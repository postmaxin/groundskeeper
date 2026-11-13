"""The one way messages go to the robot: Yarbo's cloud broker (app/cloud.py), as the official app's do.

The robot's own broker on the home network was a second route until firmware V3.14.4x closed it (#91); the panel
now has no local route at all. RobotLink stays the single doorway so every send is checked, marked and counted in one
place:

Entry points: `read()` takes only the read requests (commands.READ_ONLY_COMMANDS); `command()` takes only the run
commands (control.CATALOGUE) and only Controller calls it; `write_command()` takes only the captured writes
(mapwrite.WRITE_COMMANDS) and only MapWriter calls it. Nothing else reaches CloudTransport.publish
(tests/test_guards.py). cmd_vel is refused everywhere. Every send is recorded through `mark_sent` before it goes, so
the robot echoing it back (which can be handled before the send call returns) is never taken for someone else's
command; a send that fails takes its mark back.

An optional BLE route (app/ble.py, ADR 0012) sits behind the same doorway, off by default: it needs the `ble_enabled`
setting and a transport, and is used only for a run command or a read the cloud can't carry now (the cloud, when it
carries commands, always wins). The cmd_vel and allowlist checks run before any route is chosen. Map writes never go
by BLE (ADR 0004: not captured), nor the cloud-video commands (ble.NEVER_ON_BLE). One send takes one route and is
never repeated on the other, even when it fails; own-echo marks are made per send whatever the route.
"""
import time

from .ble import NEVER_ON_BLE
from .commands import check_command, check_read_only

ROUTE = "cloud"
ROUTE_BLE = "ble"
QUIET_COMMANDS = frozenset({"set_working_state"})   # not shown as the link's last command
ON_ONLY = frozenset({"smart_vision_control", "set_working_state"})   # sent only as {"state": 1}


class NoRoute(ConnectionError):
    """The Yarbo cloud can't carry the message (signed out, connecting, or the demo robot that takes no commands)."""


class RobotLink:
    def __init__(self, rec, cloud, store, mark_sent=None, ble=None, ble_enabled=None):
        self.rec, self.cloud, self.store = rec, cloud, store
        self.ble = ble                                       # app.ble.BleTransport, or None: no BLE route at all
        self.ble_enabled = ble_enabled or (lambda: False)    # the ble_enabled setting, read live; off unless given
        self.mark_sent = mark_sent or (lambda cmd: None)   # (cmd) -> undo or None: own-send record
        self.last_command: dict | None = None

    def cloud_up(self) -> bool:
        c = self.cloud
        return bool(c and c.state == "connected" and getattr(c, "connected", True))

    def ble_up(self) -> bool:
        """BLE is a route only with a transport, the setting on, and a connected link: all three."""
        b = self.ble
        return bool(b is not None and self.ble_enabled() is True and b.state == "connected" and b.connected)

    def _cloud_command_route(self) -> str | None:
        return ROUTE if self.cloud_up() and getattr(self.cloud, "takes_commands", True) else None

    def read_route(self) -> str | None:
        return ROUTE if self.cloud_up() else (ROUTE_BLE if self.ble_up() else None)

    def command_route(self) -> str | None:
        """None for the demo robot, which answers reads and takes no commands."""
        return self._cloud_command_route() or (ROUTE_BLE if self.ble_up() else None)

    def write_route(self) -> str | None:
        return self._cloud_command_route()       # map writes are cloud only (ADR 0004)

    def _pick(self, cmd: str, reading: bool = False, writing: bool = False):
        """(route, transport) for one send, chosen once: the cloud when it can carry it, else BLE when it may. None
        when neither can. Called only after the entry point's checks, so the cmd_vel refusal never depends on it."""
        if writing:
            return (ROUTE, self.cloud) if self._cloud_command_route() else (None, None)
        if reading and self.cloud_up():
            return ROUTE, self.cloud
        if not reading and self._cloud_command_route():
            return ROUTE, self.cloud
        if cmd not in NEVER_ON_BLE and self.ble_up():
            return ROUTE_BLE, self.ble
        return None, None

    @property
    def state(self) -> str:
        """For the interlocks: "connected" while the cloud carries commands."""
        return "connected" if self.command_route() else "no route to the robot"

    # -- sending ---------------------------------------------------------------------------------------------------
    def read(self, cmd: str, payload: dict | None = None) -> str:
        check_read_only(cmd)
        route, transport = self._pick(cmd, reading=True)
        if route is None:
            raise NoRoute(self._why_none())
        undo = self.mark_sent(cmd)       # before it goes, as for commands: its echo may be handled first
        try:
            transport.publish(cmd, payload or {})
        except BaseException:
            if callable(undo):
                undo()
            raise
        return route

    def command(self, cmd: str, payload: dict) -> str:
        """A run command (Controller only). Returns the route it went."""
        check_command(cmd)
        from . import control
        if cmd not in control.CATALOGUE:
            raise NoRoute(f"{cmd} is not a run command")
        # on only, checked again here as cmd_vel is blocked at every layer (True and 1.0 are not 1)
        on = list(payload) == ["state"] and type(payload["state"]) is int and payload["state"] == 1
        if cmd in ON_ONLY and not on:
            raise NoRoute(f"{cmd} is only {{\"state\": 1}}")
        return self._send(cmd, payload, writing=False)

    def write_command(self, cmd: str, payload: dict, route: str | None = None) -> str:
        """A captured write (MapWriter only). Returns the route it went. `route` is the write's route so far (MapWriter
        keeps one for the whole write); there is only the cloud."""
        check_command(cmd)
        from . import mapwrite
        if cmd not in mapwrite.WRITE_COMMANDS:
            raise NoRoute(f"{cmd} is not a map or settings write")
        if route not in (None, ROUTE):
            raise NoRoute(f"no {route} route to the robot")
        return self._send(cmd, payload, writing=True)

    def _send(self, cmd: str, payload: dict, writing: bool = False) -> str:
        route, transport = self._pick(cmd, writing=writing)
        if route is None:
            raise NoRoute(self._why_none())
        undo = self.mark_sent(cmd)       # before it goes: the echo can be handled before the send call returns
        try:
            transport.publish(cmd, payload)
        except BaseException:
            if callable(undo):
                undo()                   # it never went, so it is not ours
            raise
        if cmd not in QUIET_COMMANDS:    # a keep-awake beat every 4 s is not "the last command" (status shows that)
            self.last_command = {"cmd": cmd, "route": route, "at": time.time()}
        return route

    def why_no_writes(self) -> str | None:
        """Why a map or settings write can't go out now (None when it can): the checks before a write ask this."""
        return None if self.write_route() else self._why_none()

    def _why_none(self) -> str:
        if self.cloud_up():
            return "the demo robot takes no commands"
        return f"the Yarbo cloud is {getattr(self.cloud, 'state', 'not set up')} (Settings → Robot)"

    def status(self) -> dict:
        out = {"cloud": self.cloud_up(), "reads": self.read_route(), "commands": self.command_route(),
               "writes": self.write_route(), "last_command": self.last_command}
        if self.ble is not None:
            out["ble"] = self.ble_up()
        return out
