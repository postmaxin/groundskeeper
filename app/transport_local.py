"""Client for the robot's on-board MQTT broker on the home network.

`request()` publishes only the read requests in READ_ONLY_COMMANDS; anything else raises ReadOnlyViolation before the
network is touched. `command()` publishes only what the panel may send to the robot: the run commands in
control.CATALOGUE and the captured writes in mapwrite.WRITE_COMMANDS, never cmd_vel. It is called only by
app.link.RobotLink, which only Controller and MapWriter call, after their own checks (tests/test_guards.py).
Since 2026-10-21 the robot is known to accept action and run commands here (docs/dev/protocol.md → Access control).
"""
import logging
import secrets
import threading
import time
from collections.abc import Callable

import paho.mqtt.client as mqtt

from .codec import decode, encode

log = logging.getLogger(__name__)

READ_ONLY_COMMANDS = frozenset({
    "get_device_msg",
    "get_map",
    "read_all_plan",
    "get_plan_feedback",   # the running plan's progress, or "no plan working"
    "read_all_clean_area", # every area's full record (what an in-place area save starts from)
    "read_mower_area_params",  # one area's mow settings, by id
    "read_gps_ref",
    "read_low_cfd_config",
    "read_tow_params",
    "get_all_map_backup",        # the robot's list of its own map backups (id, name, timestamp)
    "get_map_buckup_from_id",    # one backup's map, by id (sic: the robot's spelling)
    "read_mower_pathway_params", # one pathway's mowing settings, by id
    "read_deadend_params",       # one dead end's settings, by id (id 0 in the reply = the robot's defaults)
    "get_wifi_list",             # the Wi-Fi networks the robot hears (a ~4 s scan): the survey and Health → Network
    "get_connect_wifi_name",
    "read_schedules",            # the robot's own schedules (made in the app): the Schedule page warns about them     # the Wi-Fi network it is on (no answer when none): Health → Network
}) | {
    "preview_snowbot_area_path", # COMPUTE_ONLY: the planned mowing route for an area outline (the app's preview)
}
# Requests that are not named get_/read_ but only make the robot compute and answer (nothing on it changes). Each one is
# listed here by name, so the read-only test still catches anything else.
COMPUTE_ONLY = frozenset({"preview_snowbot_area_path"})


class ReadOnlyViolation(Exception):
    """Raised for any attempt to publish something other than a read request."""


def check_read_only(cmd: str) -> None:
    if cmd not in READ_ONLY_COMMANDS:
        raise ReadOnlyViolation(f"refusing to publish {cmd!r}: only read requests are allowed")


def check_command(cmd: str, payload: dict | None = None) -> None:
    """A command the panel may send: a run command or a captured write. cmd_vel never. The keep-awake is allowed only
    with exactly {"state": 1} (pass the payload to check it)."""
    from . import control, mapwrite          # imported here: both modules sit above this one
    if cmd == control.KEEP_AWAKE:
        if not (payload == control.KEEP_AWAKE_PAYLOAD and type(payload["state"]) is int):
            raise ReadOnlyViolation(f"refusing to publish {cmd!r}: only {control.KEEP_AWAKE_PAYLOAD}")
        return
    if cmd in control.HARD_BLOCKED or cmd not in (set(control.CATALOGUE) | set(mapwrite.WRITE_COMMANDS)):
        raise ReadOnlyViolation(f"refusing to publish {cmd!r}: not a run command or a captured write")


def _client(prefix: str) -> mqtt.Client:
    return mqtt.Client(client_id=f"{prefix}-{secrets.token_hex(3)}",
                       callback_api_version=mqtt.CallbackAPIVersion.VERSION2)


def _ok(rc) -> bool:
    return not rc.is_failure if hasattr(rc, "is_failure") else rc == 0


def discover_sn(host: str, port: int, timeout: float = 30) -> str:
    """Find the robot's serial from its heart_beat topic (published every ~1.5 s)."""
    found = threading.Event()
    result: dict[str, str] = {}
    client = _client("yarbo-panel-discover")

    def on_connect(c, userdata, flags, rc, props=None):
        if _ok(rc):
            c.subscribe("snowbot/+/device/heart_beat")

    def on_message(c, userdata, msg):
        result["sn"] = msg.topic.split("/")[1]
        found.set()

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=30)
    client.loop_start()
    try:
        if not found.wait(timeout):
            raise RuntimeError(f"no heart_beat on {host}:{port} within {timeout:.0f} s; is the robot awake? "
                               "You can also enter the serial in Settings → Robot")
        return result["sn"]
    finally:
        client.loop_stop()
        client.disconnect()


class LocalTransport:
    """Subscribes to everything under snowbot/{sn}/; publishes read requests, and commands only for RobotLink."""

    def __init__(self, host: str, port: int, sn: str, on_message: Callable[[str, object, float], None]):
        self.host, self.port, self.sn = host, port, sn
        self._on_message = on_message
        self.connected = False
        self.connected_since = 0.0
        self.last_message_at = 0.0
        self.published = 0
        self.commands_sent = 0
        self.received = 0
        self.last_error = ""
        self._client = _client("yarbo-panel-ro")
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message
        self._client.reconnect_delay_set(min_delay=2, max_delay=60)

    def start(self) -> None:
        self._client.connect_async(self.host, self.port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def _handle_connect(self, client, userdata, flags, rc, props=None):
        if _ok(rc):
            self.connected = True
            self.connected_since = time.time()
            self.last_error = ""
            client.subscribe([(f"snowbot/{self.sn}/device/#", 0), (f"snowbot/{self.sn}/app/#", 0)])
            log.info("local broker %s:%s connected; listening to snowbot/%s/#", self.host, self.port, self.sn)
        else:
            self.last_error = f"CONNACK {rc}"
            log.warning("local broker refused the connection: %s", rc)

    def _handle_disconnect(self, client, userdata, flags, rc, props=None):
        self.connected = False
        self.last_error = f"disconnected: {rc}"
        log.warning("local broker disconnected: %s", rc)

    def _handle_message(self, client, userdata, msg):
        self.received += 1
        self.last_message_at = time.time()
        try:
            self._on_message(msg.topic, decode(msg.payload), self.last_message_at)
        except Exception:
            log.exception("handler failed for %s", msg.topic)

    def request(self, cmd: str, payload: dict | None = None) -> None:
        check_read_only(cmd)
        if not self.connected:
            raise ConnectionError("local broker is not connected")
        self._client.publish(f"snowbot/{self.sn}/app/{cmd}", encode(payload or {}))
        self.published += 1

    def command(self, cmd: str, payload: dict) -> None:
        """Only app.link.RobotLink calls this (the checks are Controller's and MapWriter's)."""
        check_command(cmd, payload)
        if not self.connected:
            raise ConnectionError("local broker is not connected")
        self._client.publish(f"snowbot/{self.sn}/app/{cmd}", encode(payload))
        self.commands_sent += 1
