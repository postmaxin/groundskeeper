"""An optional Bluetooth (BLE) transport behind app.link.RobotLink (ADR 0012, fork issue #73, upstream #150).

Off by default, and not a doorway of its own: RobotLink checks every command and read first (the 0002 refusal of
cmd_vel and the other motor commands, the run-command and read allowlists) and only then picks a route. This module
sends bytes it is handed and refuses anything RobotLink would never hand it (`allowed`), as a second lock, not the
first. Map writes are not carried (ADR 0004: their BLE encodings are not captured), nor the cloud-video keep-alive
commands (NEVER_ON_BLE).

`BleTransport` has the shape of `CloudTransport` as RobotLink uses it: `publish(cmd, payload)` raising
ConnectionError when it did not go, `state`, `connected` and `takes_commands`; incoming messages are handed to the same
`on_message(topic, decoded, ts)` the cloud path uses (Ops._from_cloud), with the topic spelled as the cloud spells a
robot message, so DeviceMSG and replies are handled once, in one place.

The device I/O is a `BleAdapter`, shaped for bleak (BlueZ) but library-agnostic. This PR has none that touches
Bluetooth: tests use a fake. The bleak adapter is a later PR and needs a robot and a capture (ADR 0012). Framing is
app/ble_packet.py's; nothing here re-implements it.
"""
import abc
import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from collections.abc import Callable

from . import ble_packet

log = logging.getLogger(__name__)

# Commands that are never carried on BLE even when RobotLink would allow them on the cloud:
#  * the camera-on and keep-awake commands belong to the cloud video path, and the own-echo accounting for them
#    (recorder.COUNTED_OWN) expects the cloud's echo, which a BLE send would never produce;
#  * preview_snowbot_area_path is protobuf over BLE in the official app (ADR 0012 / 0004): not captured, so cloud only.
NEVER_ON_BLE = frozenset({"smart_vision_control", "set_working_state", "preview_snowbot_area_path"})
SEND_TIMEOUT_S = 5.0     # for all of one message's writes together: the caller waits this long at most


class DeliveryUncertain(ConnectionError):
    """A write timed out: the message may still reach the robot, so RobotLink keeps its own-send mark."""


class BleAdapter(abc.ABC):
    """The device I/O a BleTransport needs: one GATT central link to one robot. Async, as bleak is. There is no real
    implementation in this PR; the bleak one is a later PR (ADR 0012), written against this and the tests' fake.

    `mtu` is the agreed ATT MTU in bytes (what `ble_packet.encode_message` is given). `is_connected` is True only while
    a link is up. `write` is one GATT write-without-response of one whole Packet and raises on failure. The callback set
    by `set_notify_callback` receives the raw bytes of each notification (one Packet) and must be safe to call from
    the adapter's own thread or loop."""

    @property
    @abc.abstractmethod
    def mtu(self) -> int: ...

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    @abc.abstractmethod
    async def connect(self, name_prefix: str) -> None:
        """Find a robot by advertised name prefix and connect, subscribing to notifications."""

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @abc.abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    def set_notify_callback(self, callback: Callable[[bytes], None]) -> None: ...


def allowed(cmd: str) -> bool:
    """What this transport will carry, as a second lock behind RobotLink: a documented read or a run command, never a
    hard-blocked command, a map write or a NEVER_ON_BLE one."""
    from . import control, mapwrite
    from .commands import READ_ONLY_COMMANDS
    if cmd in control.HARD_BLOCKED or cmd in mapwrite.WRITE_COMMANDS or cmd in NEVER_ON_BLE:
        return False
    return cmd in READ_ONLY_COMMANDS or cmd in control.CATALOGUE


class BleTransport:
    takes_commands = True

    def __init__(self, adapter: BleAdapter, on_message: Callable[[str, object, float], None],
                 sn: Callable[[], str | None], compress: bool = False):
        self.adapter, self._on_message, self._sn = adapter, on_message, sn
        self.compress = compress         # whether the module takes zlib: unconfirmed until captured, so off
        self.published = 0
        self.error = ""
        self._bound_sn: str | None = None    # the robot this link was made for; a different selected serial is no link
        self._uncertain = False              # a write timed out: it may still be on the air, so no send until reconnect
        self._reasm = ble_packet.Reassembler()
        self._rlock = threading.Lock()   # the Reassembler is fed from the adapter's thread
        self._wlock = threading.Lock()   # one message's chunks are written without another's between them
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="yarbo-ble", daemon=True)
        self._thread.start()
        adapter.set_notify_callback(self._notified)

    # -- state, as RobotLink reads it from the cloud transport ----------------------------------------------------
    @property
    def connected(self) -> bool:
        """Up only for the robot it was connected for: if the selected serial changed since, this is no link to it."""
        return bool(self.adapter.is_connected and self._bound_sn and self._bound_sn == self._sn()
                    and not self._uncertain)

    @property
    def state(self) -> str:
        return "connected" if self.connected else "disconnected"

    def _run(self, coro, timeout: float):
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError("BleTransport called from its own loop")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()                 # asks the adapter to stop; whether the bytes went is then unknown
            raise

    def connect(self, name_prefix: str) -> None:
        """Connect for the robot selected now (Ops's serial); a later change of serial drops the link to it."""
        sn = self._sn()
        if not sn:
            raise ConnectionError("no robot serial to bind the BLE link to")
        try:
            self._run(self.adapter.connect(name_prefix), 30.0)
        except Exception as exc:
            self.error = str(exc)
            raise
        with self._rlock:
            self._reasm = ble_packet.Reassembler()       # nothing half-received from an earlier link counts
        self._bound_sn, self._uncertain, self.error = sn, False, ""

    def close(self) -> None:
        try:
            if self.adapter.is_connected:
                self._run(self.adapter.disconnect(), 10.0)
        except Exception:
            log.warning("BLE disconnect failed")
        self._bound_sn = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(2.0)

    # -- out ---------------------------------------------------------------------------------------------------------
    def publish(self, cmd: str, payload: dict) -> None:
        """Only app.link.RobotLink calls this, after its checks. Raises ConnectionError when the message did not all go;
        it is never sent again (the link does not retry or fall back to the cloud)."""
        if not allowed(cmd):
            raise ConnectionError(f"BLE does not carry {cmd}")
        if not self.connected:
            raise ConnectionError(f"BLE is {self.state}")
        body = json.dumps(payload, separators=(",", ":")).encode()
        try:
            chunks = ble_packet.encode_message(cmd, body, self.adapter.mtu, self.compress)
        except ValueError as exc:
            raise ConnectionError(f"BLE could not frame {cmd}: {exc}") from exc
        with self._wlock:
            deadline = time.monotonic() + SEND_TIMEOUT_S
            try:
                for chunk in chunks:
                    self._run(self.adapter.write(chunk), max(0.1, deadline - time.monotonic()))
            except concurrent.futures.TimeoutError as exc:
                self._uncertain = True   # it may still go out: no other message follows it on this link
                raise DeliveryUncertain("BLE write timed out; the message may or may not have been delivered") from exc
            except Exception as exc:
                raise ConnectionError(f"BLE write failed: {exc}") from exc
        self.published += 1

    # -- in ----------------------------------------------------------------------------------------------------------
    def _notified(self, raw: bytes) -> None:
        with self._rlock:
            msg = self._reasm.feed(raw)
        if msg is None:
            return
        sn = self._sn()
        if not sn or sn != self._bound_sn or not msg.topic or "/" in msg.topic:
            log.warning("BLE message dropped: not the robot this link was made for, or an odd topic")
            return
        try:
            data = json.loads(msg.body)     # already inflated (and bounded) by the Reassembler: never inflated twice
        except ValueError:
            log.warning("BLE %s: body is not JSON (or is a compressed body over the size limit), dropped", msg.topic)
            return
        try:
            self._on_message(f"snowbot/{sn}/device/{msg.topic}", data, time.time())
        except Exception:
            log.exception("BLE message handler failed")

    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "commands_sent": self.published}
