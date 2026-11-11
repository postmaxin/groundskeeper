"""Which way each message goes to the robot: its own broker on the home network (local) or Yarbo's cloud.

Local first, the cloud as a fallback, so either path alone is enough:
- the robot drops to 4G (unreachable on the home network) when its HaLow link is weak, and the cloud still reaches it;
- a firmware update could close the local broker (it disconnects), or keep it open but ignore commands, and the cloud
  still works;
- the cloud can be down or signed out, and the local broker still works.

Reads go local while the local broker is connected and answering; after LOCAL_MISSES unanswered cheap reads in a row
they go through the cloud for BACKOFF_S, then local is tried again. Run commands go local while it is connected,
unless LOCAL_MISSES local commands in a row were not seen in the robot's state (a broker that accepts but ignores
them): then they go through the cloud for BACKOFF_S. Controller decides when a command may be repeated by the other
route (only pause, stop and return to dock). Map and settings writes keep one route for the whole write and, until a
local write is proved (LOCAL_WRITES_PROVED), go only through the cloud.

Entry points: `command()` takes only the run commands (control.CATALOGUE) and only Controller calls it;
`write_command()` takes only the captured writes (mapwrite.WRITE_COMMANDS) and only MapWriter calls it. Nothing else
reaches LocalTransport.command or CloudTransport.publish (tests/test_guards.py). cmd_vel is refused on every route.
Every send is recorded through `mark_sent`, so the robot echoing it back is never taken for someone else's command.
"""
import time

from .transport_local import check_command, check_read_only

ROUTE_MODES = ("auto", "local", "cloud")
# Map, settings, plan and backup writes on the local broker are not proved yet (run commands and a setting are,
# 2026-10-21). Until they are, writes go only through the cloud.
LOCAL_WRITES_PROVED = False
# The only observed path for smart_vision_control is the cloud broker (docs/dev/protocol.md, Robot video). Until a send on
# the local broker is observed, it goes only through the cloud.
LOCAL_VISION_PROVED = False
# The keep-awake (set_working_state) is the same: only the cloud broker is observed, so it goes only through the cloud.
LOCAL_KEEPAWAKE_PROVED = False
LOCAL_MISSES = 2              # unanswered local reads (or unseen local commands) in a row before switching
BACKOFF_S = 300               # how long reads or commands stay on the cloud before local is tried again
CHEAP_READS = ("get_device_msg", "read_gps_ref", "get_plan_feedback")   # quick replies: a miss means something
FAILING_NOTE_S = 3600         # "your network still not answering" at most this often


class NoRoute(ConnectionError):
    """Neither route can reach the robot (or the route mode rules out the one that could)."""


class RobotLink:
    def __init__(self, rec, cloud, store, mark_sent=None):
        self.rec, self.cloud, self.store = rec, cloud, store
        self.mark_sent = mark_sent or (lambda cmd: None)   # (cmd) -> None: the recorder's own-send record
        self._misses = {"reads": 0, "commands": 0}
        self._off_until = {"reads": 0.0, "commands": 0.0}
        self.reads_route: str | None = None
        self.commands_route: str | None = None
        self.last_command: dict | None = None
        self._failing_noted = 0.0
        self._changes: dict = {}
        self.on_change = None            # (what, before, after) -> None, for an event when a route changes

    # -- which way -------------------------------------------------------------------------------------------------
    def mode(self) -> str:
        m = self.store.setting("robot_route") or "auto"
        return m if m in ROUTE_MODES else "auto"

    def local_up(self) -> bool:
        """The local broker is connected (enough for reads; the demo's stand-in counts too)."""
        t = getattr(self.rec, "transport", None)
        return bool(t and t.connected)

    def local_commands_up(self) -> bool:
        t = getattr(self.rec, "transport", None)
        return bool(t and t.connected and hasattr(t, "command"))

    def cloud_up(self) -> bool:
        c = self.cloud
        return bool(c and c.state == "connected" and getattr(c, "connected", True))

    def _pick(self, what: str, local_ok: bool) -> str | None:
        m, now = self.mode(), time.time()
        if m == "local":
            return "local" if local_ok else None
        if m == "cloud":
            return "cloud" if self.cloud_up() else None
        if local_ok and (now >= self._off_until[what] or not self.cloud_up()):
            return "local"
        return "cloud" if self.cloud_up() else None

    def read_route(self) -> str | None:
        return self._pick("reads", self.local_up())

    def command_route(self) -> str | None:
        return self._pick("commands", self.local_commands_up())

    def write_route(self) -> str | None:
        """The route a map or settings write takes (it keeps it for the whole write)."""
        if not LOCAL_WRITES_PROVED:
            return "cloud" if self.mode() != "local" and self.cloud_up() else None
        return self.command_route()

    def other_route(self, route: str) -> str | None:
        """The fallback for a command that went `route` and was not seen (Automatic only)."""
        if self.mode() != "auto":
            return None
        if route == "local":
            return "cloud" if self.cloud_up() else None
        return "local" if self.local_commands_up() else None

    def _allowed(self, route: str) -> bool:
        m = self.mode()
        return m == "auto" or m == route

    @property
    def state(self) -> str:
        """For the interlocks: "connected" while some route carries commands."""
        return "connected" if self.command_route() else "no route to the robot"

    # -- sending ---------------------------------------------------------------------------------------------------
    def read(self, cmd: str, payload: dict | None = None) -> str:
        check_read_only(cmd)
        route = self.read_route()
        if route is None:
            raise NoRoute(self._why_none())
        if route == "local":
            self.rec.transport.request(cmd, payload)
        else:
            self.cloud.publish(cmd, payload or {})
        self.mark_sent(cmd)
        self._note("reads", route)
        return route

    def note_read(self, cmd: str, route: str | None, answered: bool) -> None:
        """A read's outcome. Only quick reads count: a slow map or backup read timing out says little."""
        if route == "local" and cmd in CHEAP_READS:
            self._outcome("reads", answered)

    def note_command(self, route: str | None, seen: bool) -> None:
        """A run command's outcome (seen in the robot's state or not), from Controller."""
        if route == "local":
            self._outcome("commands", seen)

    def _outcome(self, what: str, ok: bool) -> None:
        if ok:
            self._misses[what] = 0
            return
        self._misses[what] += 1
        if self._misses[what] >= LOCAL_MISSES and self.cloud_up() and self.mode() == "auto":
            self._off_until[what] = time.time() + BACKOFF_S
            self._misses[what] = 0
            now = time.time()
            if now - self._failing_noted >= FAILING_NOTE_S:
                self._failing_noted = now
                self.store.event("route_local_failing", f"{what}: the robot did not answer on your network "
                                 f"{LOCAL_MISSES} times in a row; using the Yarbo cloud for {BACKOFF_S // 60} min")

    def command(self, cmd: str, payload: dict, route: str | None = None) -> str:
        """A run command (Controller only). Returns the route it went."""
        check_command(cmd)
        from . import control
        if cmd not in control.CATALOGUE:
            raise NoRoute(f"{cmd} is not a run command")
        if cmd == "smart_vision_control":
            # on only, checked again here as cmd_vel is blocked at every layer (True and 1.0 are not 1)
            if not (list(payload) == ["state"] and type(payload["state"]) is int and payload["state"] == 1):
                raise NoRoute("smart_vision_control only switches the cameras on ({\"state\": 1})")
            if not LOCAL_VISION_PROVED:
                if route == "local" or self.mode() == "local":
                    raise NoRoute("the cameras are switched on through the Yarbo cloud only for now")
                route = "cloud" if self.cloud_up() else None
                if route is None:
                    raise NoRoute(f"the Yarbo cloud is {getattr(self.cloud, 'state', 'not set up')}")
        return self._send(cmd, payload, route or self.command_route(), None if route else "commands")

    def keep_awake_beat(self, payload: dict) -> str:
        """The one keep-awake beat (Controller.keep_awake only; it is not a run command). Returns the route it went."""
        from . import control
        if payload != control.KEEP_AWAKE_PAYLOAD or type(payload.get("state")) is not int:
            raise NoRoute("the keep-awake is only {\"state\": 1}")      # fixed, as cmd_vel is blocked at every layer
        route = None
        if not LOCAL_KEEPAWAKE_PROVED:
            if self.mode() == "local":
                raise NoRoute("the keep-awake goes through the Yarbo cloud only for now")
            route = "cloud" if self.cloud_up() else None
            if route is None:
                raise NoRoute(f"the Yarbo cloud is {getattr(self.cloud, 'state', 'not set up')}")
        return self._send(control.KEEP_AWAKE, payload, route or self.command_route(), None if route else "commands")

    def write_command(self, cmd: str, payload: dict, route: str | None = None) -> str:
        """A captured write (MapWriter only). Returns the route it went."""
        check_command(cmd)
        from . import mapwrite
        if cmd not in mapwrite.WRITE_COMMANDS:
            raise NoRoute(f"{cmd} is not a map or settings write")
        route = route or self.write_route()
        if route == "local" and not LOCAL_WRITES_PROVED:
            raise NoRoute("writes on your network aren't proved yet; they go through the Yarbo cloud")
        return self._send(cmd, payload, route, None)

    def _send(self, cmd: str, payload: dict, route: str | None, what: str | None) -> str:
        if route is None:
            raise NoRoute(self._why_none() if what is not None or cmd not in self._writes() else self._why_no_writes())
        if not self._allowed(route):
            raise NoRoute(f"the route is set to {self.mode()} only")
        if route == "local":
            if not self.local_commands_up():
                raise NoRoute("the robot's local broker is not connected")
            self.rec.transport.command(cmd, payload)
        else:
            if not self.cloud_up():
                raise NoRoute(f"the Yarbo cloud is {getattr(self.cloud, 'state', 'not set up')}")
            self.cloud.publish(cmd, payload)
        self.mark_sent(cmd)              # after it went: the robot's echo arrives later, on the event loop
        self.last_command = {"cmd": cmd, "route": route, "at": time.time()}
        if what:
            self._note(what, route)
        return route

    @staticmethod
    def _writes():
        from . import mapwrite
        return mapwrite.WRITE_COMMANDS

    def _why_no_writes(self) -> str:
        if self.mode() == "local":
            return ("the route is set to your network only, and writes there aren't proved yet: set the route to "
                    "Automatic or Yarbo cloud (Settings → Robot) for map and settings writes")
        return f"map and settings writes go through the Yarbo cloud for now, and it is {getattr(self.cloud, 'state', 'not set up')}"

    def _why_none(self) -> str:
        m = self.mode()
        if m == "local":
            return "the robot's local broker is not connected (route: your network only)"
        if m == "cloud":
            return f"the Yarbo cloud is {getattr(self.cloud, 'state', 'not set up')} (route: Yarbo cloud only)"
        return "neither the robot's local broker nor the Yarbo cloud is connected"

    def _note(self, what: str, route: str) -> None:
        """The automatically chosen route for reads or commands; a change is reported, each kind of change at most once
        an hour (reads go back and forth while the local broker is half there)."""
        before = getattr(self, f"{what}_route")
        setattr(self, f"{what}_route", route)
        if before and before != route and self.on_change:
            key, now = (what, before, route), time.time()
            if now - self._changes.get(key, 0) >= FAILING_NOTE_S:
                self._changes[key] = now
                self.on_change(what, before, route)

    def status(self) -> dict:
        now = time.time()
        return {"mode": self.mode(), "local": self.local_up(), "cloud": self.cloud_up(),
                "reads": self.read_route(), "commands": self.command_route(), "writes": self.write_route(),
                "fallback_ready": self.mode() == "auto" and self.local_up() and self.cloud_up(),
                "local_reads_paused_s": max(0, round(self._off_until["reads"] - now)) or None,
                "local_commands_paused_s": max(0, round(self._off_until["commands"] - now)) or None,
                "local_writes_proved": LOCAL_WRITES_PROVED, "last_command": self.last_command}
