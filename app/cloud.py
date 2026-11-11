"""Yarbo cloud transport (spec §5.3), designed against the bug that broke the official HA integration in June.

* The panel never stores the password. login() hands it to the official SDK, which RSA-encrypts it for Yarbo's API;
  only the session tokens are kept, in DATA_DIR/cloud_session.json (chmod 600).
* The rotated refresh token is persisted after EVERY refresh.
* A failed refresh means "needs login": reconnecting stops, and the panel says so. It never connects with an
  expired JWT (the SDK swallows that failure; this module does not).
* Reconnects back off from 5 s to 5 min.

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

log = logging.getLogger(__name__)


class CloudTransport:
    def __init__(self, data_dir: str, sn: str, on_message: Callable[[str, object, float], None]):
        self.path = Path(data_dir) / "cloud_session.json"
        self.sn = sn
        self._on_message = on_message
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
        self._refresh_lock = threading.Lock()   # one refresh (and its save) at a time: the worker and video reads share it

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
        self._client = None
        c = self._sdk()
        c.login(email, password)
        self.username = email.lower()
        self._save()
        self.error = ""
        self.start()

    def logout(self) -> None:
        self.stop()
        self._client = None
        self.username = None
        self.state, self.error = "signed out", ""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    # -- connection ------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = threading.Event()        # each run has its own: a worker still winding down keeps the old one
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name="yarbo-cloud", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_client()

    def restart(self) -> None:
        """Drop the connection and connect again (after the robot's serial changed). Blocks while the old worker
        finishes, so call it off the event loop."""
        self.stop()
        t = self._thread
        if t and t.is_alive():
            t.join(25)
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

    def _refresh(self, stale_jwt: str | None = None) -> bool:
        """Refresh the session. If `stale_jwt` is given and the session no longer holds it, another caller has
        already refreshed: use that and do nothing."""
        from yarbo_robot_sdk.exceptions import TokenExpiredError
        with self._refresh_lock:
            if stale_jwt is not None and self.session_jwt() not in (None, stale_jwt):
                return True
            return self._refresh_locked(TokenExpiredError)

    def _refresh_locked(self, TokenExpiredError) -> bool:
        try:
            self._sdk()._auth.refresh()
            self._save()
            self._last_refresh = time.time()
            return True
        except TokenExpiredError as exc:
            self.state, self.error = "needs login", f"session expired: {exc}"
            log.warning("Yarbo cloud session expired; sign in again in Settings")
            return False
        except Exception as exc:
            self.error = f"refresh failed: {exc}"
            return True  # network trouble: keep the session and retry later

    def _run(self, stop: threading.Event) -> None:
        delay = 5
        while not stop.is_set():
            self.state = "connecting"
            if not self.sn:
                self.error = "waiting for the robot's serial (Settings → Robot)"
                stop.wait(10)
                continue
            if not self._refresh():
                return
            try:
                self._connect_once(stop)
                delay = 5
                # Stay connected; refresh the token twice a day so the refresh token keeps rotating.
                while not stop.wait(60):
                    if not self.connected:
                        break
                    if time.time() - self._last_refresh > 12 * 3600:
                        if not self._refresh():
                            self._close_client()
                            return
            except Exception as exc:
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
        with self._lock:
            self._mqtt = client
        self.connected, self.state, self.error = True, "connected", ""
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
        info = client.publish(f"snowbot/{self.sn}/app/{cmd}", encode(payload))
        rc = getattr(info, "rc", 0)                  # paho: was the message taken (not: did it arrive)
        if rc != 0:                                  # MQTT_ERR_SUCCESS
            raise ConnectionError(f"the cloud did not take the message (MQTT error {rc})")
        self.published += 1

    def status(self) -> dict:
        return {"state": self.state, "username": self.username, "broker": self.broker, "error": self.error,
                "commands_sent": self.published}
