"""Yarbo cloud transport (spec §5.3), designed against the bug that broke the official HA integration in June.

* The panel never stores the password. login() hands it to the official SDK, which RSA-encrypts it for Yarbo's API;
  only the session tokens are kept, in DATA_DIR/cloud_session.json (chmod 600).
* The rotated refresh token is persisted after EVERY refresh.
* A failed refresh means "needs login": reconnecting stops, and the panel says so. It never connects with an
  expired JWT (the SDK swallows that failure; this module does not).
* Reconnects back off from 5 s to 5 min.
* One worker at a time. Each has its own stop event; a stopped worker that is still winding down (a connect can take
  20 s) never installs its connection or writes the status, so a sign-in or restart starts the next one at once.
* With no serial known (none found before), the account's own robot
  list (the SDK's get_devices(), an account lookup that sends the robot nothing) fills it: one robot is used, several
  are offered in Settings → Robot. While it waits the status says so, and the log says so once.

The SDK is used for its config, auth and per-robot broker routing; the MQTT connection is our own paho client, so
the reconnect/refresh behaviour above is ours too.
"""
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import paho.mqtt.client as mqtt

from .codec import decode, encode
from .runtime import valid_sn

log = logging.getLogger(__name__)


class CloudTransport:
    def __init__(self, data_dir: str, sn: str, on_message: Callable[[str, object, float], None],
                 on_sn: Callable[[str], None] | None = None):
        self.path = Path(data_dir) / "cloud_session.json"
        self.sn = sn
        self._on_message = on_message
        self.on_sn = on_sn           # told (on the worker thread) when the account's only robot gave the serial
        self.devices: list[dict] = []    # the account's robots, while there are several and none is chosen
        # Set by Ops: where the kept serial came from ("account"; older installs "heart_beat" or "settings"; None to
        # skip the check, as during a run), and what to do when a serial the signed-in account lacks is dropped.
        self.sn_source: Callable[[], str | None] | None = None
        self.on_forget: Callable[[str], None] | None = None
        self._check_sn = False           # after a sign-in: is the kept serial on this account?
        self._check_tried_at = 0.0
        self._devices_at = 0.0
        self.state = "signed out"
        self.username: str | None = None
        self.broker: str | None = None
        self.error = ""
        self.connected = False
        self.published = 0
        self.connected_since = 0.0
        self._client = None          # YarboClient
        self._mqtt: mqtt.Client | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # one refresh (and its save) at a time: the worker and video reads share it
        self._refresh_lock = threading.Lock()

    # -- session ---------------------------------------------------------------------------------------------
    def _sdk(self):
        if self._client is None:
            from yarbo_robot_sdk import YarboClient  # imports fetch nothing; the constructor fetches /sdk/config
            self._client = YarboClient()
        return self._client

    def _save(self) -> None:
        c = self._sdk()
        body = json.dumps({"username": self.username, "token": c.token, "refresh_token": c.refresh_token,
                           "saved_at": time.time()})
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(body)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def restore(self) -> bool:
        if not self.path.exists():
            return False
        try:
            s = json.loads(self.path.read_text())
            self.username = s["username"]
            self._sdk().restore_session(s["username"], s["token"], s["refresh_token"])
            return True
        except Exception as exc:
            self.state, self.error = "needs login", f"saved session unreadable: {exc}"
            return False

    def login(self, email: str, password: str) -> None:
        """Blocking; call from a worker thread. The password is not kept anywhere."""
        self.stop()
        with self._refresh_lock:        # a refresh in flight must not save over (or after) the new session
            self._client = None
            c = self._sdk()
            c.login(email, password)
            self.username = email.lower()
            self._save()
        self.error, self.devices, self._devices_at = "", [], 0.0     # another account may have other robots
        self._check_sn, self._check_tried_at = True, 0.0   # a serial from another account mustn't be followed here
        self.start()

    def logout(self) -> None:
        self.stop()
        with self._refresh_lock:        # a refresh in flight must not write the file back after we remove it
            self._client = None
            self.username = None
            self.devices = []
            self.state, self.error = "signed out", ""
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    # -- connection ------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive() and not self._stop.is_set():
            return                            # already running; a stopped one winding down is simply replaced
        self._stop = threading.Event()        # each run has its own: a worker still winding down keeps the old one
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name="yarbo-cloud", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_client()

    def restart(self) -> None:
        """Drop the connection and connect again (after the robot's serial changed)."""
        self.stop()
        self.state = "reconnecting"
        self.start()

    def _close_client(self) -> None:
        with self._lock:
            client, self._mqtt = self._mqtt, None
        if client:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        self.connected = False

    def session_jwt(self) -> str | None:
        """The signed-in JWT, or None when signed out. Never builds the SDK client (that fetches config over the
        network), so it is safe to call from the event loop."""
        c = self._client
        if c is None:
            return None
        return getattr(c, "token", None) or None

    def refresh_session(self, stale_jwt: str | None = None) -> str:
        """Refresh the session and say how it went: "ok", "expired" (sign in again) or "unreachable" (network
        trouble: the session is kept). If `stale_jwt` is given and the session no longer holds it, another caller
        has already refreshed: that is "ok" and nothing is done."""
        from yarbo_robot_sdk.exceptions import TokenExpiredError
        with self._refresh_lock:
            if stale_jwt is not None and self.session_jwt() not in (None, stale_jwt):
                return "ok"
            client = self._client
            if client is None:          # no session to refresh or save
                if self.username is None:   # signed out (perhaps while we waited for the lock): say just that
                    self.state = "signed out"
                else:
                    self.state, self.error = "needs login", "no Yarbo session: sign in again in Settings → Robot"
                return "expired"
            try:
                client._auth.refresh()
                if self._client is not client:      # signed out or replaced meanwhile: do not write the file back
                    return "expired"
                self._save()
                self._last_refresh = time.time()
                return "ok"
            except TokenExpiredError as exc:
                self.state, self.error = "needs login", f"session expired: {exc}"
                log.warning("Yarbo cloud session expired; sign in again in Settings")
                return "expired"
            except Exception as exc:
                self.error = f"refresh failed: {exc}"
                return "unreachable"

    def _refresh(self) -> bool:
        return self.refresh_session() != "expired"   # network trouble: keep the session and retry later

    def _save_rotated(self) -> None:
        """Save a session the SDK rotated on its own. A failed write keeps the new tokens in memory (the worker carries
        on with them) and says so; the next refresh saves again."""
        try:
            self._save()
        except OSError as exc:
            self.error = f"could not save the Yarbo session: {exc}"
            log.warning("could not save the Yarbo session after the SDK refreshed it: %s", type(exc).__name__)

    def _lookup_due(self) -> bool:
        return self._client is not None and time.time() - self._devices_at >= 600

    def _find_sn(self, stop: threading.Event | None = None) -> str | None:
        """Ask the account which robots it has (an account lookup; nothing is sent to a robot). One robot: its
        serial. Several: none, and Settings → Robot offers them. Asked again after 10 minutes, so a robot added to
        the account turns up."""
        robots = self._list_robots(stop)
        return None if robots is None else self._pick(robots)

    def _pick(self, robots: list) -> str | None:
        if len(robots) == 1:
            self.devices = []
            return robots[0].sn
        self.devices = [{"sn": d.sn, "name": getattr(d, "name", "") or "", "model": getattr(d, "model", "") or ""}
                        for d in robots]
        if robots:
            log.info("the Yarbo account has %d robots: choose one in Settings → Robot", len(robots))
        return None

    def _list_robots(self, stop: threading.Event | None = None, force: bool = False) -> list | None:
        """The account's robots, or None when the lookup failed, was stopped or isn't due (`force` asks anyway). Runs
        under the refresh lock: on a 401 the SDK refreshes the session by itself, and a rotated refresh token must be
        saved, not lost at the next restart. A dead session means "needs login"."""
        from yarbo_robot_sdk.exceptions import TokenExpiredError
        if not (force and self._client is not None) and not self._lookup_due():
            return None
        self._devices_at = time.time()
        with self._refresh_lock:
            client = self._client
            if client is None:
                return None
            before = client.refresh_token
            try:
                found = client.get_devices()
            except TokenExpiredError as exc:
                if stop is None or not stop.is_set():
                    self.state, self.error = "needs login", f"session expired: {exc}"
                    log.warning("Yarbo cloud session expired; sign in again in Settings")
                return None
            except Exception as exc:
                self._devices_at -= 540         # a failed lookup is tried again after a minute, not ten
                log.warning("could not list the robots on the Yarbo account: %s", type(exc).__name__)
                return None
            finally:
                if self._client is client and client.refresh_token != before:
                    self._save_rotated()        # the SDK refreshed (and rotated) the session on a 401
        if stop is not None and stop.is_set():
            return None                         # stopped while asking: the next worker owns the status
        return [d for d in found if valid_sn(getattr(d, "sn", None))]   # it goes into MQTT topics: no + # /

    def _kept_sn_elsewhere(self, stop: threading.Event) -> list | None:
        """After a sign-in: when this account doesn't list the kept serial, drop it and return this account's robots
        (to choose from); the cloud can't reach a robot the account doesn't have. None when it stays, or can't be
        checked now (a run is open, the lookup failed): then the check stays pending and is tried again."""
        if not self.sn:
            self._check_sn = False
            return None
        source = self.sn_source() if self.sn_source else None
        if source is None:
            return None                         # a run is open: checked once it has ended
        if time.time() - self._check_tried_at < 600:
            return None                         # asked recently and it failed: try again later, not every minute
        self._check_tried_at = time.time()
        robots = self._list_robots(stop, force=True)
        if stop.is_set():
            return None                         # stopped meanwhile: a new sign-in owns the check now
        if robots is None:
            return None                         # the lookup failed: still pending
        if self.sn_source() is None:
            return None                         # the robot started mowing while the account answered: later
        self._check_sn = False
        if any(d.sn == self.sn for d in robots):
            return None
        log.info("the signed-in Yarbo account has no robot with the kept serial: finding the robot again")
        dropped, self.sn = self.sn, None
        if self.on_forget:
            self.on_forget(dropped)
        return robots

    def _run(self, stop: threading.Event) -> None:
        delay = 5
        waiting_said = False
        listed = None                       # this account's robots, when a check after sign-in already asked
        while not stop.is_set():
            self.state = "connecting"
            if self.sn and self._check_sn:
                if not self._refresh():
                    return
                listed = self._kept_sn_elsewhere(stop)
                if stop.is_set() or self.state == "needs login":
                    return
            if not self.sn:
                if listed is None and self._lookup_due() and not self._refresh():   # a fresh session first
                    return
                sn, listed = (self._pick(listed) if listed is not None else self._find_sn(stop)), None
                if stop.is_set() or self.state == "needs login":
                    return
                if sn and not stop.is_set() and not self.sn:     # not if one was chosen meanwhile
                    self.sn = sn
                    log.info("Yarbo cloud: using the account's only robot")
                    if self.on_sn:
                        self.on_sn(sn)
                    continue
                self.error = "waiting for the robot's serial (Settings → Robot)"
                if not waiting_said:
                    log.warning("Yarbo cloud is waiting for the robot's serial (Settings → Robot)")
                    waiting_said = True
                stop.wait(10)
                continue
            if self.error.startswith("waiting for the robot's serial"):
                self.error = ""
            if not self._refresh():
                return
            try:
                self._connect_once(stop)
                delay = 5
                # Stay connected; refresh the token twice a day so the refresh token keeps rotating.
                while not stop.wait(60):
                    if not self.connected:
                        break
                    if self._check_sn:          # still pending after a sign-in (a run was open, or the lookup failed)
                        listed = self._kept_sn_elsewhere(stop)
                        if listed is not None:  # not this account's robot: drop the connection and choose again
                            self._close_client()
                            break
                    if time.time() - self._last_refresh > 12 * 3600:
                        if not self._refresh():
                            self._close_client()
                            return
            except Exception as exc:
                if stop.is_set():               # stopped while connecting: the next worker owns the status
                    break
                self.error = str(exc)
                log.warning("Yarbo cloud connection failed: %s", exc)
            if stop.is_set():
                break
            self.state = "reconnecting"
            stop.wait(delay)
            delay = min(delay * 2, 300)

    def _connect_once(self, stop: threading.Event) -> None:
        from yarbo_robot_sdk.mqtt_client import LegacyCredentialsProvider, NewBrokerCredentialsProvider
        c = self._sdk()
        cfg = c._config
        migrated = c._migration.query(self.sn)
        # Credentials AND client id come from the SDK's own providers: the brokers only accept the SDK's
        # client-id scheme ("HA_{email}_..."); a "yarbo-panel_..." id is refused with "Not authorized".
        if migrated:
            host, port, tls = cfg.get("new_mqtt_host"), int(cfg.get("new_mqtt_port")), bool(cfg.get("new_mqtt_use_tls"))
            creds = NewBrokerCredentialsProvider(c._auth)
        else:
            host, port = cfg.get("mqtt_host"), int(cfg.get("mqtt_port"))
            tls = bool(cfg.get("mqtt_use_tls")) if cfg.get("mqtt_use_tls") is not None else True
            creds = LegacyCredentialsProvider(c._auth)
        user, pw = creds.initial_credentials()
        self.broker = f"{'new' if migrated else 'legacy'} ({host})"
        done = threading.Event()
        result = {}

        def on_connect(cl, ud, flags, rc, props=None):
            ok = not rc.is_failure if hasattr(rc, "is_failure") else rc == 0
            result["ok"], result["rc"] = ok, str(rc)
            if ok:
                cl.subscribe([(f"snowbot/{self.sn}/device/#", 0), (f"snowbot/{self.sn}/app/#", 0)])
            done.set()

        def on_disconnect(cl, ud, flags, rc, props=None):
            if cl is self._mqtt:                # not a connection a stopped worker gave up on
                self.connected = False

        def on_message(cl, ud, msg):
            try:
                self._on_message(msg.topic, decode(msg.payload), time.time())
            except Exception:
                log.exception("cloud message handler failed")

        client = mqtt.Client(client_id=creds.client_id(), callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        client.username_pw_set(user, pw)
        if tls:
            client.tls_set()
        client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
        client.reconnect_delay_set(min_delay=5, max_delay=300)
        client.connect(host, port, keepalive=60)
        client.loop_start()
        if not done.wait(20) or not result.get("ok") or stop.is_set():
            client.loop_stop()
            client.disconnect()
            if stop.is_set():
                raise ConnectionError("stopped while connecting")
            raise ConnectionError(f"cloud broker refused the connection: {result.get('rc', 'no reply')}")
        with self._lock:                        # stop() sets the event before it takes this lock to close _mqtt
            if stop.is_set():
                installed = False
            else:
                self._mqtt, installed = client, True
                self.connected, self.state, self.error = True, "connected", ""
        if not installed:
            client.loop_stop()
            client.disconnect()
            raise ConnectionError("stopped while connecting")
        self.connected_since = time.time()
        self._last_refresh = time.time()
        log.info("Yarbo cloud connected via the %s broker", self.broker.split()[0])

    _last_refresh = 0.0

    def publish(self, cmd: str, payload: dict) -> None:
        """Only app.link.RobotLink calls this (for Controller and MapWriter, after their checks, and for reads).
        Raises if not connected."""
        with self._lock:
            client = self._mqtt
        if not (client and self.connected):
            raise ConnectionError(f"cloud is {self.state}")
        # QoS 0 (paho's default) on purpose: at QoS 1 or 2 paho queues the message even when it returns NO_CONN, so
        # raising on rc != 0 below would report a failed send that is then delivered anyway.
        info = client.publish(f"snowbot/{self.sn}/app/{cmd}", encode(payload))
        rc = getattr(info, "rc", 0)      # paho: non-zero when the client did not take the message (4 = not connected)
        if rc != 0:
            raise ConnectionError(f"the cloud did not take the message (MQTT error {rc})")
        self.published += 1

    def status(self) -> dict:
        return {"state": self.state, "username": self.username, "broker": self.broker, "error": self.error,
                "commands_sent": self.published,
                "devices": list(self.devices) if not self.sn else []}
