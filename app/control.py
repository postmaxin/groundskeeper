"""Robot control (spec §10). The only module that can command the robot, and it is OFF until switched on.

Every request passes, in order:
1. the catalogue (five official run commands, the cameras-on switch and the keep-awake; anything else, including
   manual drive, is refused),
2. the master switch (setting `control_enabled`, default False),
3. the interlocks for that command (§10.3), enforced here and not only in the UI,
then goes out through app.link.RobotLink (the Yarbo cloud, the only route to the robot) and is written to the audit
log with its route and result, whatever happened.

The robot sends no reply to run commands (seen 2026-10-05: nothing for start_plan or pause on either broker), so a
command is confirmed by the robot's state changing the way the command should change it (CONFIRM below).
"""
import asyncio
import contextvars
import json
import logging
import random
import time

from . import why
from .ble import DeliveryUncertain

log = logging.getLogger(__name__)

CATALOGUE = {
    "start_plan": {"label": "Start plan", "args": ("plan_id",)},
    "pause": {"label": "Pause", "args": ()},
    "resume": {"label": "Resume", "args": ()},
    "stop": {"label": "Stop", "args": ()},
    "cmd_recharge": {"label": "Return to dock", "args": ()},
    # From the official app (docs/dev/protocol.md: Robot video, and the phone-app commands table). Switches the robot's
    # cameras on for the live view, nothing else. Only on: "off" also cut the official app's view on a phone, so a
    # client leaves switching off to the robot or the app.
    "smart_vision_control": {"label": "Cameras on", "args": ()},
    # The keep-awake the official app sends while it is open (docs/dev/protocol.md), so the robot streams its cameras.
    # Fixed payload {"state": 1}; there is no "state 0". People only, and only inside a window a person started from
    # the Cameras tile (Controller.keep_awake, §13), so /api/control/send alone cannot keep the robot awake.
    "set_working_state": {"label": "Keep awake", "args": ()},
}
HARD_BLOCKED = {"cmd_vel"}  # manual drive: never, from anyone
KEEP_AWAKE = "set_working_state"
KEEP_AWAKE_PAYLOAD = {"state": 1}
KEEP_AWAKE_MIN_S = 3.0        # at most one beat this often from the panel, whoever asks (a browser sends one every 4 s)
KEEP_AWAKE_GAP_S = 15.0       # no beat this long = the watching has stopped; the next beat is a new start
KEEP_AWAKE_MAX_S = 15 * 60    # one press of play keeps the robot awake at most this long; then a person presses again
KEEP_AWAKE_JITTER_S = 1.0     # each beat waits a random 0..this first, so the app's beats cannot stay in step (§13)
# A refusal on one of these ends the window: the robot went offline or out of reach, or the time is up.
KEEP_AWAKE_ENDS_ON = ("Robot online", "Route to the robot", "Within the time limit")
# Set only while Controller.keep_awake is sending (per task): a keep-awake from anywhere else (/api/control/send, say)
# has no window, so it is refused, and every beat gets the rate limit and the jitter.
_IN_BEAT = contextvars.ContextVar("keep_awake_beat", default=False)

GATE_DEFAULTS = {
    "min_battery_pct": 30,
    "require_dry": True,            # scheduler: Conditions must be Dry
    "allow_damp": False,            # scheduler: also accept Damp
    "presence_required": True,      # scheduler: someone home per Alarmo; unknown counts as a fail
    "yard_clear_minutes": 10,
    "yard_labels": ["person", "dog", "cat"],
    "phone_app_quiet_s": 60,        # no phone-app keep-awake seen in this window
    "yard_clear_required": True,    # start check: no person/animal on the yard cameras for yard_clear_minutes
    "guard_person": "pause",        # during a run: pause | dock | notify | ignore
    "guard_rain": "dock",
    "guard_everyone_left": "dock",
    "guard_after_dark": "dock",     # a plan that charged mid-run and heads back out after sunset is sent home
    "guard_resume": False,          # after a guard PAUSE, resume by itself once the cause is gone (opt-in)
    "guard_resume_after_min": 5,    # ...and has stayed gone this long
}
GUARD_ACTIONS = ("pause", "dock", "notify", "ignore")
STRICT_ACTORS = ("mcp", "token")    # an AI assistant, or a program with an API token: nobody here is watching


def _i(t: dict, k: str) -> int:
    v = t.get(k)
    return int(v) if isinstance(v, (int, float)) else 0


# How each run command shows up in the robot's state: (seconds to wait, test on the flat telemetry, what it means).
LATE_WATCH_S = 180   # an unconfirmed command is still watched this long (from the send) for the robot acting late
CONFIRM = {
    "start_plan": (60, lambda t: _i(t, "planning") > 0 and _i(t, "planning_paused") == 0, "the plan is running"),
    "pause": (30, lambda t: _i(t, "planning_paused") == 1, "the robot reports its plan paused"),
    "resume": (45, lambda t: _i(t, "planning_paused") == 0 and _i(t, "planning") > 0, "the plan is running again"),
    "stop": (45, lambda t: _i(t, "planning") == 0 and _i(t, "recharging") != 1
             and max(abs(t.get("speed_l") or 0), abs(t.get("speed_r") or 0)) < 0.05, "the robot has stopped"),
    "cmd_recharge": (45, lambda t: _i(t, "recharging") > 0 or _i(t, "charging") > 0,
                     "the robot is heading to its dock"),
}


def payload_for(cmd: str, args: dict) -> dict:
    if cmd == "start_plan":
        return {"id": int(args["plan_id"])}
    if cmd == "cmd_recharge":
        return {"cmd": 2}  # the firmware requires cmd=2 (official SDK)
    if cmd in ("smart_vision_control", KEEP_AWAKE):
        return {"state": 1}  # a constant: there is no payload a caller can change into "off"
    return {}


class Refused(Exception):
    def __init__(self, reason: str, checks: list | None = None):
        super().__init__(reason)
        self.reason, self.checks = reason, checks or []


def gates(store) -> dict:
    return {**GATE_DEFAULTS, **(store.setting("gates") or {})}


def check(cmd: str, args: dict, ctx: dict, cfg: dict, actor: str) -> list[dict]:
    """Interlocks. ctx is a snapshot of everything the checks need (built by the recorder).
    Each check: {name, ok: True|False|None (unknown), hard: bool, detail}. A failed hard check refuses the command;
    a failed soft check refuses it unless the caller explicitly overrides (manual starts only)."""
    out = []

    def add(name, ok, detail, hard=True):
        out.append({"name": name, "ok": ok, "hard": hard, "detail": detail})

    add("Robot online", ctx["snapshot_age_s"] is not None and ctx["snapshot_age_s"] < 120,
        f"last snapshot {ctx['snapshot_age_s']} s ago" if ctx["snapshot_age_s"] is not None else "no data")
    add("Route to the robot", ctx["route_state"] == "connected", ctx["route_state"])
    if cmd in ("start_plan", "resume"):
        # A map, settings or Wi-Fi write in progress must finish first (#57): a Wi-Fi join holds the writer for up to
        # a minute while it checks. Unknown (no writer to ask) blocks too. Pause, stop and dock are never held back.
        w = ctx.get("writing")
        add("No write to the robot in progress", w is False,
            "a write to the robot is in progress" if w else "no write in progress" if w is False else "unknown")
    if cmd == "resume" and (actor == "guard" or actor in STRICT_ACTORS):
        # Nobody pressed this here (a guard, an AI assistant, an API token): every check is hard and unknown counts as a fail
        # (blades start spinning).
        add("No active error", not ctx["error_code"], f"error code {ctx['error_code'] or 'none'}")
        yc = ctx["yard"]
        add("Yard clear (Frigate)", yc["ok"] is True, yc["detail"])
        pa = ctx["phone_app_active"]
        add("Phone app not driving it", pa is False, "unknown" if pa is None else ("phone app active" if pa else "quiet"))
        if cfg["require_dry"]:
            ok_states = {"Dry", "Damp"} if cfg["allow_damp"] else {"Dry"}
            add("Lawn dry", ctx["conditions"] in ok_states, ctx["conditions"] or "no weather data")
        if cfg["presence_required"]:
            add("Someone home (Alarmo)", ctx["presence"] is True,
                {True: "yes", False: "Alarmo armed away", None: "unknown"}[ctx["presence"]])
        return out
    if cmd == "smart_vision_control":
        # Allowed idle or running, like pause, stop and dock: it only starts the robot's video, so the route and
        # online checks are all it needs. The one extra rule is that "on" is the only thing that can be asked for.
        # An absent state means on; any other value (0, 2, "1", true, 1.0) fails closed.
        add("People only", actor == "user",
            "a person at the panel" if actor == "user"
            else f"{actor} may not switch the cameras on: the video is for people")
        s = args.get("state", 1)
        add("Cameras on only", type(s) is int and s == 1,
            "switching on" if type(s) is int and s == 1
            else f"state {s!r} refused: the panel only switches the cameras on")
    if cmd == KEEP_AWAKE:
        # Allowed idle or running, like the cameras-on switch: a beat says nothing about a run. The window is the
        # server's own record (ctx["awake_for_s"], set by Controller.send from Controller.keep_awake), never the
        # request.
        add("People only", actor == "user", "a person at the panel" if actor == "user"
            else f"{actor} may not keep the robot awake: the video is for people")
        s = args.get("state", 1)
        add("Keep awake only", type(s) is int and s == 1,
            "keep awake" if type(s) is int and s == 1 else f"state {s!r} refused: the panel only sends state 1")
        add("Robot video on", ctx.get("robot_video") is True,
            "on" if ctx.get("robot_video") is True else "robot video is off (Settings → Robot)")
        a = ctx.get("awake_for_s")
        add("Within the time limit", a is not None and a < KEEP_AWAKE_MAX_S,
            "only the Cameras tile keeps it awake, after a press of play" if a is None else
            f"{a / 60:.0f} of {KEEP_AWAKE_MAX_S // 60} min" if a < KEEP_AWAKE_MAX_S else
            f"{KEEP_AWAKE_MAX_S // 60} min are up: press Open on the Cameras tile to keep watching")
    if cmd != "start_plan":
        return out
    plan_ids = {p.get("id") for p in ctx["plans"]}
    pid = plan_id_of(args)
    add("Plan exists", pid is not None and pid in plan_ids, f"plan {args.get('plan_id')}")
    add("No active error", not ctx["error_code"], f"error code {ctx['error_code'] or 'none'}")
    batt = ctx["battery"]
    add("Battery", batt is not None and batt >= cfg["min_battery_pct"],
        f"{batt}% (minimum {cfg['min_battery_pct']}%)" if batt is not None else "unknown")
    add("Not already running", not ctx["running"], "a run is in progress" if ctx["running"] else "idle")
    # A start from an assistant or an API token (STRICT_ACTORS) is held to the strictest reading: what is unknown counts
    # as a fail, and the lawn and presence checks are hard whenever they are switched on. A person at the panel can see
    # the unknowns.
    strict = actor in STRICT_ACTORS
    yc = ctx["yard"]
    if strict and cfg.get("yard_clear_required", True):
        add("Yard clear (Frigate)", yc["ok"] is True, yc["detail"])
    elif not cfg.get("yard_clear_required", True):
        add("Yard clear (Frigate)", None, "check switched off (Settings → Run rules)", hard=False)
    else:
        add("Yard clear (Frigate)", yc["ok"] if yc["ok"] is not None else None, yc["detail"], hard=yc["ok"] is not None)
    pa = ctx["phone_app_active"]
    # Unknown (no route that shows the app) blocks the starts nobody is watching (scheduler, assistant); a person's
    # start from the panel goes ahead with the unknown shown.
    add("Phone app not driving it", (pa is False) if strict else (None if pa is None else not pa),
        "unknown (no link to the robot)" if pa is None else ("phone app active" if pa else "quiet"),
        hard=strict or pa is not None or actor == "scheduler")
    cond = ctx["conditions"]
    ok_states = {"Dry", "Damp"} if cfg["allow_damp"] else {"Dry"}
    if strict and cfg["require_dry"]:
        add("Lawn dry", cond in ok_states, cond or "no weather data")
    elif cond is None:
        add("Lawn dry", None, "no weather data", hard=actor == "scheduler" and cfg["require_dry"])
    else:
        add("Lawn dry", cond in ok_states or not cfg["require_dry"], cond, hard=actor == "scheduler")
    pres = ctx["presence"]
    if strict and cfg["presence_required"]:
        add("Someone home (Alarmo)", pres is True,
            {True: "yes", False: "Alarmo armed away", None: "unknown (Home Assistant not connected)"}[pres])
    elif pres is None:
        add("Someone home (Alarmo)", None, "unknown (Home Assistant not connected)",
            hard=cfg["presence_required"] and actor == "scheduler")
    else:
        add("Someone home (Alarmo)", pres, "yes" if pres else "Alarmo armed away", hard=actor == "scheduler")
    return out


def plan_id_of(args: dict) -> int | None:
    """A plan id is an integer (or an integer string). null, booleans and junk are never coerced into a plan."""
    v = args.get("plan_id")
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def verdict(checks: list[dict], override: bool) -> tuple[bool, str]:
    hard_fail = [c for c in checks if c["hard"] and c["ok"] is not True]
    soft_fail = [c for c in checks if not c["hard"] and c["ok"] is False]
    if hard_fail:
        return False, "; ".join(f"{c['name']}: {c['detail']}" for c in hard_fail)
    if soft_fail and not override:
        return False, "; ".join(f"{c['name']}: {c['detail']}" for c in soft_fail) + " (can be overridden)"
    return True, "ok"


# Commands that say nothing about the run: they never count as a person stepping in (last_human_cmd_at) and never
# replace the status stream's `last` (a Pause's result stays visible).
RUN_NEUTRAL = frozenset({"smart_vision_control", KEEP_AWAKE})


class Controller:
    def __init__(self, store, link, context_fn, expect_reply_fn, state_fn=None):
        """link: app.link.RobotLink (command, state).
        context_fn(args) -> ctx dict (async). expect_reply_fn(cmd) -> Future resolved by the matching data_feedback; registered BEFORE
        publishing so a fast reply cannot be missed. state_fn() -> the robot's latest flat telemetry, used to confirm
        a command took effect (None = no confirmation)."""
        self.store = store
        self.link = link
        self._context, self._expect, self._state = context_fn, expect_reply_fn, state_fn
        self._lock = asyncio.Lock()
        self.poll_s = 0.5
        self.last_human_cmd_at = 0.0      # last command sent from this panel by a person or the scheduler
        self.last: dict | None = None     # the last background-confirmed command, for the status stream
        self.start_out: dict | None = None  # the last start or resume this panel sent: {"cmd", "at"} (#69)
        self.writing = lambda: False      # a write to the robot holds the writer's lock; set by Ops
        self._tasks: set = set()
        self._beat_at = None              # monotonic time of the last keep-awake attempt (the rate limit), any viewer
        self._beat_ok_at = None           # monotonic time of the last one that went out; None = none since it stopped
        self._awake_since = None          # monotonic start of the keep-awake window a person opened; None = none
        # () -> {"snapshot_age_s", "route_state"}: all a beat's checks need, without the yard or presence lookups (set
        # by Ops; None = the full context). A beat every 4 s must not hold the lock across Frigate's timeouts.
        self.light_context = None

    async def _watch(self, cmd: str, fut: asyncio.Future, last: dict, rowid: int, logged: dict) -> None:
        """Confirm a background command; the audit row gets the outcome, the status stream gets `last`."""
        try:
            try:
                res = await self._confirm(cmd, fut)
            except Exception as exc:
                res = {"confirmed": None, "confirm": f"could not check ({exc})", "reply": None}
            last.update(confirmed=res["confirmed"], confirm=res["confirm"], done=True)
            reply = res.get("reply") or {}
            self.store.update_control_result(rowid, {**logged, "confirmed": res["confirmed"], "confirm": res["confirm"],
                                                     "state": reply.get("state"), "msg": reply.get("msg") or res["confirm"]})
            if res["confirmed"] is not None:
                self.store.event("command_confirmed" if res["confirmed"] else "command_unconfirmed",
                                 f"{cmd}: {res['confirm']}")
            if res["confirmed"] is False and not res.get("reply"):
                await self._watch_late(cmd, rowid, logged, res, last)
        except Exception:
            log.exception("confirming %s failed", cmd)
            last["done"] = True

    async def _watch_late(self, cmd: str, rowid: int, logged: dict, res: dict, last: dict) -> None:
        """After "no change seen": keep looking, quietly, up to LATE_WATCH_S from the send. In the weak patch the robot
        can act a minute late (run 4: Return to dock at 15:26:14 marked unconfirmed at 45 s, heading home ~15:27:20).
        The first verdict stands for anything that acted on it (the guards retry on it); this only sets the record
        straight. Nothing is sent."""
        timeout, test, meaning = CONFIRM.get(cmd, (15, None, ""))
        if test is None or self._state is None:
            return
        start = time.time() - timeout
        while time.time() - start < LATE_WATCH_S:
            await asyncio.sleep(self.poll_s)
            if test(self._state() or {}):
                took = time.time() - start
                note = f"{meaning}, late ({took:.0f} s)"
                last.update(confirmed=True, confirm=note)
                self.store.update_control_result(rowid, {**logged, "confirmed": True, "confirm": note, "late": True})
                self.store.event("command_confirmed_late", f"{cmd}: {note}")
                return

    async def _confirm(self, cmd: str, fut: asyncio.Future) -> dict:
        """Wait for the robot to show the command took effect (or to reject it in a reply). The robot normally sends
        no reply to run commands, so the state change is the confirmation."""
        timeout, test, meaning = CONFIRM.get(cmd, (15, None, ""))
        if self._state is None:
            test = None
        start = time.time()
        reply = None
        while time.time() - start < timeout:
            if fut.done() and reply is None:
                reply = fut.result()
                if reply.get("state") not in (0, None):
                    return {"confirmed": False, "reply": reply,
                            "confirm": f"the robot refused: {reply.get('msg') or reply.get('state')}"}
                if test is None:
                    return {"confirmed": True, "reply": reply, "confirm": "the robot replied OK"}
            if test is not None and test(self._state() or {}):
                return {"confirmed": True, "reply": reply,
                        "confirm": f"{meaning} ({time.time() - start:.0f} s)"}
            await asyncio.sleep(self.poll_s)
        if not fut.done():
            fut.cancel()
        return {"confirmed": False if test is not None else None, "reply": reply,
                "confirm": f"no change seen in {timeout} s" if test is not None else "no reply"}

    def enabled(self) -> bool:
        return bool(self.store.setting("control_enabled", False))

    async def keep_awake(self, actor: str = "user", start: bool = False) -> dict:
        """One keep-awake beat for the Cameras tile (§13), sent through send() and check() like any command.

        The server never repeats a beat: the open tile asks for one every 4 s, so closing it stops them. `start` (a
        person pressed Open, a camera, or Try again) opens a window of KEEP_AWAKE_MAX_S; check() refuses beats outside
        one, and a refusal for the time limit, the robot going offline or losing its route ends the window, so the
        person presses play again. At most one beat every KEEP_AWAKE_MIN_S, whoever asks: a request inside that is
        dropped quietly before send() (several open tabs would otherwise fill the audit log with refusals). Each beat
        waits a random 0..KEEP_AWAKE_JITTER_S first, so the phone app's own beats cannot stay in step with the panel's
        (recorder.own_echo). The start and the stop of the beats are each one event."""
        now = time.monotonic()
        if start and actor == "user":
            self._awake_since = now
        if self._beat_at is not None and now - self._beat_at < KEEP_AWAKE_MIN_S:
            return {"sent": False, "limited": True}
        self._beat_at = now
        if not self.enabled():
            # Nothing would go out: answered here, without an audit row per beat, as before send(). The window still
            # runs out, so a tile left open with Control off stops asking too.
            out = {"sent": False, "control_off": True, "refused": "control is switched off (Settings → Safety)"}
            if self._awake_since is None or now - self._awake_since >= KEEP_AWAKE_MAX_S:
                self._awake_since, out["ended"] = None, True
            return out
        await asyncio.sleep(random.uniform(0, KEEP_AWAKE_JITTER_S))
        token = _IN_BEAT.set(True)
        try:
            out = await self.send(KEEP_AWAKE, {}, actor=actor, wait=False)
        finally:
            _IN_BEAT.reset(token)
        failed = {c["name"] for c in out.get("checks") or [] if c["hard"] and c["ok"] is not True}
        # No route at all (the cloud signed out, say) ends it too: nothing can go out until a person tries again.
        if not out.get("sent") and (failed & set(KEEP_AWAKE_ENDS_ON) or out.get("error")):
            capped = "Within the time limit" in failed and self._awake_since is not None
            self._awake_since = None
            out["ended"] = True
            if self._beat_ok_at is not None:
                self._beat_ok_at = None
                self.store.event("keep_awake_stopped", "the panel stopped keeping the robot awake: "
                                 + ("its time was up" if capped else "the robot is offline or out of reach"))
        if not out.get("sent") and not self.enabled():
            out["control_off"] = True
        if out.get("sent"):
            if self._beat_ok_at is not None and now - self._beat_ok_at > KEEP_AWAKE_GAP_S:
                self._beat_stopped(self._beat_ok_at, end_window=False)   # noticed late: say it now (this press stands)
            if self._beat_ok_at is None:
                self.store.event("keep_awake_started", "the robot is being kept awake for the Cameras tile")
            self._beat_ok_at = time.monotonic()
        return out

    def _beat_stopped(self, last_at: float, end_window: bool = True) -> None:
        self._beat_ok_at = None
        if end_window:
            self._awake_since = None      # the window ends with the beats: a later beat needs a new press
        self.store.event("keep_awake_stopped", f"the Cameras tile stopped keeping the robot awake "
                                               f"(last beat {time.monotonic() - last_at:.0f} s ago)")

    def keep_awake_tick(self) -> None:
        """Called from an existing loop: the beats have stopped once none came for KEEP_AWAKE_GAP_S."""
        if self._beat_ok_at is not None and time.monotonic() - self._beat_ok_at > KEEP_AWAKE_GAP_S:
            self._beat_stopped(self._beat_ok_at)

    async def preview(self, cmd: str, args: dict, actor: str = "user") -> dict:
        ctx = await self._context(args)
        checks = check(cmd, args, ctx, gates(self.store), actor)
        ok, reason = verdict(checks, override=False)
        return {"cmd": cmd, "enabled": self.enabled(), "would_send": ok and self.enabled(), "reason": reason,
                "checks": checks,
                "why": why.from_checks(checks, self.enabled(), ctx.get("plans") if cmd == "start_plan" else None)}

    def start_settling(self) -> str | None:
        """Why a write to the robot must wait: a start or resume this panel sent has not shown in the robot's state
        yet (#69). The robot can take a minute to act, so this holds for LATE_WATCH_S from the send. Once the robot
        shows the plan the hold ends for good (a later pause does not bring it back) and the plan checks take over,
        the writer's own included. None = no start in the way."""
        s = self.start_out
        if not s:
            return None
        age = time.time() - s["at"]
        test = CONFIRM[s["cmd"]][1]
        if age >= LATE_WATCH_S or (self._state is not None and test(self._state() or {})):
            self.start_out = None
            return None
        return (f"{CATALOGUE[s['cmd']]['label']} was sent {age:.0f} s ago and the robot does not show the plan running "
                f"yet; try again in a moment (writes wait up to {LATE_WATCH_S // 60} min after a start)")

    async def send(self, cmd: str, args: dict | None = None, actor: str = "user", override: bool = False,
                   wait: bool | str = True) -> dict:
        """wait=True waits for the robot's state to confirm it. wait=False returns as soon as it is out (the caller
        watches the state itself, see Ops guards). wait="background" returns at once and confirms in the background,
        publishing the outcome as `last` (the panel's buttons: a browser request is not held open for a minute)."""
        args = args or {}
        started = time.time()
        async with self._lock:
            try:
                if cmd in HARD_BLOCKED:
                    raise Refused(f"{cmd} is permanently blocked")
                if cmd not in CATALOGUE:
                    raise Refused(f"{cmd} is not in the control catalogue")
                missing = [a for a in CATALOGUE[cmd]["args"] if args.get(a) is None]
                if missing:
                    raise Refused(f"missing {', '.join(missing)}")
                if not self.enabled():
                    raise Refused("control is switched off (Settings → Safety)")
                if cmd == KEEP_AWAKE and self.light_context is not None:
                    ctx = dict(self.light_context())
                else:
                    ctx = await self._context(args)
                if cmd == KEEP_AWAKE:           # the window is the server's own record, never the request's
                    since = self._awake_since if _IN_BEAT.get() else None
                    ctx["awake_for_s"] = None if since is None else time.monotonic() - since
                    ctx["robot_video"] = bool(self.store.setting("robot_video_enabled", False))
                checks = check(cmd, args, ctx, gates(self.store), actor)
                ok, reason = verdict(checks, override=override and actor == "user")
                if not ok:
                    raise Refused(reason, checks)
                if cmd in ("start_plan", "resume") and self.writing():
                    # Asked again now, with no await before the send: the context above was read before a write
                    # could have taken the writer's lock (#69).
                    raise Refused("a write to the robot is in progress")
                payload = payload_for(cmd, args)
                fut = self._expect(cmd)
                if cmd in ("start_plan", "resume"):
                    self.start_out = {"cmd": cmd, "at": time.time()}   # before the send: a send that fails half-way holds too
                route = self.link.command(cmd, payload)
                # A run-neutral command (RUN_NEUTRAL) must not look like a person taking over (the guards,
                # ops._people_cmd_at, would stop retrying a pause or dock that didn't take).
                if actor != "guard" and cmd not in RUN_NEUTRAL:
                    self.last_human_cmd_at = time.time()
                result = {"sent": True, "cmd": cmd, "payload": payload, "checks": checks, "route": route}
            except Refused as r:
                result = {"sent": False, "cmd": cmd, "refused": r.reason, "checks": r.checks,
                          "why": ([why.control_off()] if not self.enabled() else [])
                          + (why.from_checks(r.checks, override=override and actor == "user") if r.checks else
                             [] if not self.enabled() and r.reason.startswith("control is switched off") else
                             [why.reason("refused", r.reason, "", source="control")])}
            except DeliveryUncertain as exc:
                # BLE only: it may still arrive. A person's run-affecting command then counts as taken over, as if sent.
                if actor != "guard" and cmd not in RUN_NEUTRAL:
                    self.last_human_cmd_at = time.time()
                result = {"sent": False, "cmd": cmd, "error": str(exc), "uncertain": True}
            except Exception as exc:
                result = {"sent": False, "cmd": cmd, "error": str(exc)}
        # Confirmation runs outside the lock: a Pause pressed while a start is still being confirmed goes out now.
        background = result["sent"] and wait == "background"
        if background:
            last = {"cmd": cmd, "at": started, "actor": actor, "confirmed": None, "confirm": "checking", "done": False}
            if cmd not in RUN_NEUTRAL:
                self.last = last        # else a camera-on would hide a Pause's result from the panel's toast
            result.update(confirmed=None, confirm="checking", at=started)
        elif result["sent"] and not wait:
            fut.cancel()
            result.update(confirmed=None, confirm="not waited for")
        elif result["sent"]:
            result.update(await self._confirm(cmd, fut))
            reply = result.get("reply") or {}
            result.update(state=reply.get("state"), msg=reply.get("msg") or result["confirm"])
        result["latency_ms"] = round((time.time() - started) * 1000)
        rowid = self.store.log_control(actor, result.get("route") or "none", cmd, json.dumps(args), result)
        if background:
            task = asyncio.get_running_loop().create_task(self._watch(cmd, fut, last, rowid, dict(result)))
        elif result.get("confirmed") is False and not result.get("reply"):
            task = asyncio.get_running_loop().create_task(self._watch_late(cmd, rowid, dict(result), result, {}))
        else:
            task = None
        if task is not None:
            self._tasks.add(task)                       # keep a reference until it finishes
            task.add_done_callback(self._tasks.discard)
        return result
