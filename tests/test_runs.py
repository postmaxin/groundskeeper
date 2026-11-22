"""Plan-aware runs, command confirmation and the during-run guards (2026-10-05)."""
import asyncio
import calendar
import math
import time

import pytest

from app import conditions, control, ops as ops_mod
from tests.fakelink import FakeLink
from app.ops import Ops
from app.recorder import Recorder
from app.runs import (PAUSE_CAP_S, PLAN_BLIP_S, RunDetector, RunStats, activity, end_reason, parse_plan_feedback,
                      plan_flags_active)
from app.store import Store

# Flag sets seen on 2026-10-05 (StateMSG via telemetry.extract)
DOCKED = {"planning": 0, "planning_paused": 0, "recharging": 4, "charging": 2, "speed_l": 0, "speed_r": 0}
MOWING = {"planning": 1, "planning_paused": 0, "recharging": 0, "charging": 0, "speed_l": 0.4, "speed_r": 0.4}
TRANSIT = {**MOWING, "planning": 3}
PAUSED = {"planning": 0, "planning_paused": 1, "recharging": 0, "charging": 0, "car_controller": 0,
          "speed_l": 0, "speed_r": 0, "working_state": 1}
PAUSED_ASLEEP = {**PAUSED, "working_state": 0}
TO_CHARGE = {"planning": 0, "planning_paused": 2, "recharging": 1, "charging": 0, "speed_l": 0.3, "speed_r": 0.3}
CHARGE_BREAK = {"planning": 0, "planning_paused": 2, "recharging": 4, "charging": 2, "speed_l": 0, "speed_r": 0}
RETURNING = {"planning": 0, "planning_paused": 0, "recharging": 1, "charging": 0, "speed_l": 0.3, "speed_r": 0.3}
DOCKING = {**RETURNING, "recharging": 3, "speed_l": 0, "speed_r": 0}

FEEDBACK = {"state": 0, "msg": "", "topic": "get_plan_feedback", "data": {
    "actualCleanArea": 1146.4, "areaIds": [11, 34], "cleanAreaId": 11, "duration": 14206, "finishCleanArea": 1130.0,
    "finishIds": [{"clean_times": 1, "id": 11, "move_type": 1}], "leftTime": 6339.0, "planId": 1,
    "startTime": 1791211895, "state": 1, "totalCleanArea": 1749.2, "totalTime": 18395.0,
    "cleanPathProgress": [{"id": 11, "clean_index": 428, "path": [{"x": 0, "y": 0}] * 400}]}}


@pytest.mark.parametrize("t,expected", [
    (DOCKED, "docked"), (MOWING, "mowing"), (TRANSIT, "transit"), (PAUSED, "paused"), (PAUSED_ASLEEP, "paused"),
    (TO_CHARGE, "charging_break"), (CHARGE_BREAK, "charging_break"), (RETURNING, "returning"),
    (DOCKING, "docking"), ({**MOWING, "planning": 2, "speed_l": 0, "speed_r": 0}, "planning"),
    ({"speed_l": 0.5}, "moving"), ({}, "idle")])
def test_activity_from_the_robots_flags(t, expected):
    assert activity(t) == expected


def test_plan_flags():
    assert plan_flags_active(PAUSED_ASLEEP) is True and plan_flags_active(CHARGE_BREAK) is True
    assert plan_flags_active(DOCKED) is False and plan_flags_active({"speed_l": 1}) is None


def test_one_plan_is_one_run_through_pauses_and_a_charging_break():
    """The 2026-10-05 day, condensed: start, app pause, guard pause (robot dozes off), resume, charging break of
    45 min, finish. The old detector made three runs of it."""
    d = RunDetector(idle_end_s=300)
    seq = [(0, DOCKED), (60, TRANSIT), (120, MOWING), (1500, PAUSED), (1560, MOWING), (5000, PAUSED),
           (5400, PAUSED_ASLEEP), (6200, PAUSED_ASLEEP), (6300, MOWING), (17000, TO_CHARGE), (17100, CHARGE_BREAK),
           (19800, CHARGE_BREAK), (19900, TRANSIT), (22000, MOWING), (25000, RETURNING), (25100, DOCKING),
           (25150, DOCKED), (25190, DOCKED)]
    changes = [(ts, d.update(ts, t, plan_flags_active(t))) for ts, t in seq]
    assert [c for c in changes if c[1]] == [(60, "start"), (25150, "end")]


def test_a_pause_never_resumed_closes_after_the_cap():
    d = RunDetector(idle_end_s=300)
    d.update(0, MOWING, True)
    assert d.update(100, PAUSED, True) is None
    assert d.update(100 + PAUSE_CAP_S - 1, PAUSED, True) is None
    assert d.update(100 + PAUSE_CAP_S, PAUSED, True) == "end" and d.paused_out
    assert end_reason(PAUSED, RunStats(), None, d.paused_out) == "paused, not resumed"


def test_without_plan_flags_the_old_movement_rule_applies():
    d = RunDetector(idle_end_s=300)
    assert d.update(10, {"speed_l": 0.4}, None) == "start"
    assert d.update(200, {"speed_l": 0}, None) is None
    assert d.update(311, {"speed_l": 0}, None) == "end"


def test_plan_feedback_parsing():
    p = parse_plan_feedback(FEEDBACK)
    assert p.active and p.session == "1:1791211895" and p.plan_id == 1 and p.current_area == 11
    assert p.finished_areas == [11] and p.progress_pct == pytest.approx(64.6, abs=0.1) and not p.complete
    assert "path" not in str(p.view())
    done = parse_plan_feedback({**FEEDBACK, "data": {**FEEDBACK["data"], "finishIds": [{"id": 11}, {"id": 34}],
                                                     "finishCleanArea": 1745.7}})          # as run 4 ended: 99.8 %
    assert done.complete and end_reason(DOCKED, RunStats(), done) == "plan finished"
    idle = parse_plan_feedback({"data": "", "msg": "no plan working", "state": -1})
    assert not idle.active and idle.msg == "no plan working"
    assert parse_plan_feedback({"state": 3, "data": None}) is None and parse_plan_feedback(None) is None


# -- joining runs of one plan session --------------------------------------------------------------------------
def _stored_run(store, started, ended, session, points, distance):
    rid = store.start_run(started, 5, None)
    for i in range(points):
        store.add_telemetry({"ts": started + i, "run_id": rid, "x": float(i), "y": 0.0, "rtk_status": 4})
    stats = RunStats(points=points, fixed=points, distance_m=distance, battery_start=90, battery_end=60)
    store.end_run(rid, ended, "stopped", stats)
    store.set_run_plan(rid, session, 30.0)
    return rid


def test_merge_runs_moves_telemetry_and_adds_up(tmp_path):
    st = Store(str(tmp_path))
    a = _stored_run(st, 1000, 2000, "1:5", 10, 100.0)
    b = _stored_run(st, 3000, 4000, "1:5", 30, 300.0)
    with pytest.raises(ValueError):
        st.merge_runs(b, a)
    m = st.merge_runs(a, b)
    # totals come from the rows (9 m + 29 m; the 9 m hop back to x=0 between the pieces is a jump, not driving)
    assert m["points"] == 40 and m["distance_m"] == 38.0 and m["ended"] == 4000 and m["started"] == 1000
    assert st.run(b) is None and len(st.run_points(a)) == 40
    assert st.previous_run(a) is None


def _bare_recorder(store):
    r = Recorder.__new__(Recorder)
    r.store, r.plan, r.plan_at, r.run_plan, r.t, r.activity_at = store, None, 0.0, None, {}, 0.0
    r.wifi_survey = None
    r._stall_anchor, r.stalled_s, r._stall_seen, r._stall_waiting, r._fix_back_at = None, 0.0, 0.0, False, None
    r.firmware, r._fw_pending, r.snapshot, r.run_id = {}, {}, {}, None
    return r


def test_a_resume_rejoins_the_previous_run_of_the_same_plan_session(tmp_path):
    st = Store(str(tmp_path))
    first = _stored_run(st, 1791211895, 1791218054, "1:1791211895", 50, 1800.0)
    r = _bare_recorder(st)
    r.run_id, r.run_started = st.start_run(1791218882, 5, None), 1791218882
    r.run_stats = RunStats()
    for i, row in enumerate(({"x": 0.0, "y": 0.0, "rtk_status": 4, "battery": 70},
                             {"x": 3.0, "y": 0.0, "rtk_status": 4, "battery": 69})):
        r.run_stats.add(row)
        st.add_telemetry({**row, "ts": 1791218883 + i, "run_id": r.run_id})     # rows are stored as they come
    r._on_plan(parse_plan_feedback(FEEDBACK), 1791218890)
    assert r.run_id == first and r.run_started == 1791211895
    run = st.run(first)
    assert run["ended"] is None and run["end_reason"] is None and run["points"] == 52
    # counted from the rows: 49 m, the hop back to x=0 left out as a jump, then 3 m
    assert r.run_stats.distance_m == pytest.approx(52.0) and len(st.runs()) == 1


def test_a_resume_the_next_morning_still_joins_its_plan_session(tmp_path):
    """Run 6 (2026-10-17) paused at 16:40 and was closed by the pause cap; the plan was resumed at 08:14 the next
    day (run 7, same plan session, 15.4 h after run 6's last movement) and stayed a run of its own."""
    st = Store(str(tmp_path))
    first = _stored_run(st, 1791211895, 1791218054, "1:1791211895", 50, 1800.0)
    r = _bare_recorder(st)
    r.run_started = 1791218054 + 55570
    r.run_id, r.run_stats = st.start_run(r.run_started, 5, None), RunStats()
    r._on_plan(parse_plan_feedback(FEEDBACK), r.run_started + 5)
    assert r.run_id == first and len(st.runs()) == 1


def test_a_stale_or_finished_reply_of_an_old_plan_never_joins(tmp_path):
    """Review 2026-10-19: a new run's first reply can be the last plan's (run 6's was the week before's, finished)."""
    for gap, data in ((2 * 86400, FEEDBACK["data"]),
                      (3600, {**FEEDBACK["data"], "finishCleanArea": 1745.7, "state": 5})):
        st = Store(str(tmp_path / str(gap)))
        first = _stored_run(st, 1791211895, 1791218054, "1:1791211895", 50, 1800.0)
        r = _bare_recorder(st)
        r.run_started = 1791218054 + gap
        r.run_id, r.run_stats = st.start_run(r.run_started, 5, None), RunStats()
        own = r.run_id
        r._on_plan(parse_plan_feedback({**FEEDBACK, "data": data}), r.run_started + 5)
        assert r.run_id == own != first and len(st.runs()) == 2


def test_a_different_plan_session_starts_its_own_run(tmp_path):
    st = Store(str(tmp_path))
    _stored_run(st, 1000, 2000, "1:999", 5, 10.0)
    r = _bare_recorder(st)
    r.run_id, r.run_started, r.run_stats = st.start_run(2100, 5, None), 2100, RunStats()
    own = r.run_id
    r._on_plan(parse_plan_feedback(FEEDBACK), 2110)
    assert r.run_id == own and st.run(own)["plan_session"] == "1:1791211895" and len(st.runs()) == 2


def test_a_no_plan_reply_newer_than_the_flags_ends_the_plan(tmp_path):
    r = _bare_recorder(Store(str(tmp_path)))
    r.t, r.activity_at = PAUSED_ASLEEP, 100.0
    assert r.plan_active(200) is True
    r._on_plan(parse_plan_feedback({"state": -1, "msg": "no plan working", "data": ""}), 150.0)
    assert r.plan_active(200) is False


# -- command confirmation --------------------------------------------------------------------------------------
class FakeCloud:
    state = "connected"

    def __init__(self):
        self.sent = []

    def publish(self, cmd, payload):
        self.sent.append(cmd)


def _controller(tmp_path, telemetry_now, reply=None):
    store = Store(str(tmp_path))
    store.set_setting("control_enabled", True)

    async def context(args):
        return {"snapshot_age_s": 1, "route_state": "connected", "writing": False}

    def expect(cmd):
        fut = asyncio.get_running_loop().create_future()
        if reply is not None:
            fut.set_result(reply)
        return fut

    c = control.Controller(store, FakeLink(FakeCloud()), context, expect, telemetry_now)
    c.poll_s = 0.01
    return store, c


def test_a_pause_is_confirmed_by_the_robots_state(tmp_path):
    t = dict(MOWING)
    _, c = _controller(tmp_path, lambda: t)

    async def go():
        task = asyncio.create_task(c.send("pause"))
        await asyncio.sleep(0.05)
        t.update(PAUSED)
        return await task

    r = asyncio.run(go())
    assert r["sent"] and r["confirmed"] is True and "paused" in r["confirm"]


def test_no_state_change_means_not_confirmed(tmp_path, monkeypatch):
    monkeypatch.setitem(control.CONFIRM, "resume", (0.1, control.CONFIRM["resume"][1], "running again"))
    store, c = _controller(tmp_path, lambda: PAUSED)
    r = asyncio.run(c.send("resume"))
    assert r["sent"] and r["confirmed"] is False and "no change" in r["confirm"]
    assert store.control_log()[0]["cmd"] == "resume"


def test_a_refusal_in_a_reply_is_reported(tmp_path):
    _, c = _controller(tmp_path, lambda: MOWING, reply={"topic": "pause", "state": 5, "msg": "busy"})
    r = asyncio.run(c.send("pause"))
    assert r["confirmed"] is False and "busy" in r["confirm"]


def test_yard_clear_start_check_can_be_switched_off():
    ctx = {"snapshot_age_s": 1, "route_state": "connected", "plans": [{"id": 1}], "error_code": 0, "battery": 90,
           "running": False, "yard": {"ok": False, "detail": "person on back", "events": []},
           "phone_app_active": False, "conditions": "Dry", "presence": True, "writing": False}
    on = control.check("start_plan", {"plan_id": 1}, ctx, control.GATE_DEFAULTS, "user")
    assert not control.verdict(on, False)[0]
    off = control.check("start_plan", {"plan_id": 1}, ctx, {**control.GATE_DEFAULTS, "yard_clear_required": False},
                        "user")
    assert control.verdict(off, False)[0]


# -- guards ----------------------------------------------------------------------------------------------------
class FakeRec:
    def __init__(self, clock):
        self.clock = clock
        self.run_id, self.activity, self.activity_at = 7, "mowing", 0.0
        self.t, self.conditions = dict(MOWING), {"raining": False}
        self.head_type, self.head_at = None, 0.0
        self.plan = parse_plan_feedback(FEEDBACK)
        self.foreign_cmd_at, self.app_stream_at = 0.0, 0.0
        self.feedback = FEEDBACK
        self.in_plan = True

    def set(self, t, act):
        self.t, self.activity, self.activity_at = dict(t), act, self.clock[0]

    def plan_active(self, ts=None):
        return self.in_plan

    def status(self):
        return {"state": self.activity}

    async def request(self, cmd, timeout=15, payload=None, replies=()):
        return self.feedback


class FakeController:
    """Sends go 'out'; the robot reacts unless told to ignore or the send is refused."""
    def __init__(self, rec):
        self.rec, self.sent, self.refuse, self.deaf = rec, [], 0, False
        self.last_human_cmd_at, self.checks_pass = 0.0, True

    def enabled(self):
        return True

    async def preview(self, cmd, args, actor="user"):
        return {"would_send": self.checks_pass, "reason": "ok" if self.checks_pass else "Yard clear (Frigate): person"}

    def human(self, cmd):
        """A person presses a button in this panel."""
        self.last_human_cmd_at = self.rec.clock[0]
        self.rec.set(*{"pause": (PAUSED, "paused"), "resume": (MOWING, "mowing")}[cmd])

    async def send(self, cmd, args=None, actor="user", override=False, wait=True):
        if self.refuse:
            self.refuse -= 1
            return {"sent": False, "refused": "Cloud connected: reconnecting"}
        self.sent.append(cmd)
        if not self.deaf:
            self.rec.set(*{"pause": (PAUSED, "paused"), "resume": (MOWING, "mowing"),
                           "cmd_recharge": (RETURNING, "returning")}[cmd])
        return {"sent": True, "confirmed": None}


class FakeNotifier:
    def __init__(self):
        self.msgs = []

    async def send(self, kind, title, body, **kw):
        self.msgs.append((title, body))


def _ops(tmp_path, monkeypatch, gates):
    clock = [1000.0]
    monkeypatch.setattr(ops_mod.time, "time", lambda: clock[0])
    o = Ops.__new__(Ops)
    o.store = Store(str(tmp_path))
    o.store.set_setting("gates", gates)
    o.rec = FakeRec(clock)
    o.controller, o.notifier = FakeController(o.rec), FakeNotifier()
    o.guards, o._guard_cmd_at, o._resume, o.phone_app_at, o._guard_saved, o._guard_boot = {}, 0.0, None, 0.0, {}, None
    o._charged_at, o._charged_saved, o._left_at, o._night_out, o._dark_kept = 0.0, 0.0, None, None, None
    o.yard = {"person": False}

    async def yard_status(minutes, labels):
        live = o.yard["person"]
        ev = [{"label": "person", "camera": "back", "in_progress": True}] if live else []
        return {"ok": not live, "detail": "x", "events": ev}

    async def presence():
        return True

    o.yard_status, o.presence = yard_status, presence
    return o, clock


def _tick(o, clock, secs=5, n=1):
    for _ in range(n):
        clock[0] += secs
        asyncio.run(o._guard_tick())


RESUME_ON = {"guard_resume": True, "guard_resume_after_min": 5}


def test_person_guard_pauses_then_resumes_after_the_cooldown_and_rearms(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, RESUME_ON)
    o.yard["person"] = True
    _tick(o, clock, n=3)
    assert o.controller.sent == ["pause"] and o.guards["person"]["acted"] == "pause"
    o.yard["person"] = False
    _tick(o, clock, secs=60, n=5)           # gone 5 min, counted from the end of the 30 s hysteresis
    assert o.controller.sent == ["pause"]
    _tick(o, clock, secs=60, n=2)
    assert o.controller.sent == ["pause", "resume"]
    _tick(o, clock)                          # confirmation
    assert o.guards["person"]["acted"] is None and o._resume is None
    o.yard["person"] = True                  # back 30 s later: paused again at once (no rate limit on pauses)
    _tick(o, clock, secs=30)
    assert o.controller.sent == ["pause", "resume", "pause"]


def test_a_refused_pause_is_retried_while_the_person_stays(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {})
    o.controller.refuse = 2
    o.yard["person"] = True
    _tick(o, clock, n=20)
    assert o.controller.sent == ["pause"] and o.rec.activity == "paused"
    assert any("NOT sent" in m[1] for m in o.notifier.msgs)


def test_an_unconfirmed_pause_is_sent_again(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {})
    o.controller.deaf = True
    o.yard["person"] = True
    _tick(o, clock, n=20)                    # 100 s: the 30 s confirmation window lapses, then it is resent
    assert o.controller.sent.count("pause") >= 2 and o.guards["person"]["acted"] is None


def test_a_rain_pause_does_not_resume_without_the_full_checks(tmp_path, monkeypatch):
    """Review finding: a rain-held resume used to skip the yard. Now the guard-resume interlocks (in control.check)
    gate every resume; here the controller refuses and nothing goes out."""
    ctx = {"snapshot_age_s": 1, "route_state": "connected", "error_code": 0, "phone_app_active": False,
           "yard": {"ok": None, "detail": "Frigate unreachable", "events": []}, "conditions": "Dry", "presence": None, "writing": False}
    checks = control.check("resume", {}, ctx, control.GATE_DEFAULTS, "guard")
    ok, reason = control.verdict(checks, override=True)
    assert not ok and "Yard clear" in reason and "Someone home" in reason
    assert control.verdict(control.check("resume", {}, ctx, control.GATE_DEFAULTS, "user"), False)[0]
    good = {**ctx, "yard": {"ok": True, "detail": "nothing", "events": []}, "presence": True}
    assert control.verdict(control.check("resume", {}, good, control.GATE_DEFAULTS, "guard"), False)[0]
    assert not control.verdict(control.check("resume", {}, {**good, "conditions": "Wet"}, control.GATE_DEFAULTS,
                                              "guard"), False)[0]


# -- ADR 0009: the rain guard only notifies while the snow blower is attached ----------------------------------------
def _rain(tmp_path, monkeypatch, action, head, fresh=True):
    o, clock = _ops(tmp_path, monkeypatch, {"guard_rain": action, "guard_person": "pause"})
    o.rec.head_type = head
    o.rec.conditions = {"raining": True}
    for _ in range(4):
        clock[0] += 5
        o.rec.head_at = clock[0] - (1 if fresh else control.ONLINE_S)
        asyncio.run(o._guard_tick())
    return o


@pytest.mark.parametrize("action", ["pause", "dock"])
def test_rain_does_not_stop_the_snow_blower_but_says_so(tmp_path, monkeypatch, action):
    o = _rain(tmp_path, monkeypatch, action, head=1)
    assert o.controller.sent == []
    assert len(o.notifier.msgs) == 1
    title, body = o.notifier.msgs[0]
    assert "snow blower" in title and body.startswith("notify only")


def test_rain_set_to_ignore_stays_silent_for_the_snow_blower(tmp_path, monkeypatch):
    o = _rain(tmp_path, monkeypatch, "ignore", head=1)
    assert o.controller.sent == [] and o.notifier.msgs == [] and "rain" not in o.guards


@pytest.mark.parametrize("head", [None, 0, 2, 3, 4, 5, "1", True, 1.0])
def test_rain_still_sends_every_other_head_home(tmp_path, monkeypatch, head):
    o = _rain(tmp_path, monkeypatch, "dock", head=head)
    assert o.controller.sent == ["cmd_recharge"]


def test_rain_sends_the_robot_home_when_nothing_has_named_the_snow_blower_lately(tmp_path, monkeypatch):
    o = _rain(tmp_path, monkeypatch, "dock", head=1, fresh=False)
    assert o.controller.sent == ["cmd_recharge"]


def test_the_person_guard_still_pauses_the_snow_blower(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {"guard_rain": "dock"})
    o.rec.head_type, o.rec.head_at, o.yard["person"] = 1, clock[0], True
    _tick(o, clock, n=2)
    assert o.controller.sent == ["pause"]


def test_no_auto_resume_unless_switched_on(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {"guard_resume": False})
    o.yard["person"] = True
    _tick(o, clock, n=2)
    o.yard["person"] = False
    _tick(o, clock, secs=60, n=30)
    assert o.controller.sent == ["pause"]


def test_someone_elses_resume_is_not_fought_once_the_yard_is_clear(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, RESUME_ON)
    o.yard["person"] = True
    _tick(o, clock, n=2)
    o.yard["person"] = False
    _tick(o, clock, secs=60)
    o.rec.set(MOWING, "mowing")              # resumed from the phone app
    o.rec.set(PAUSED, "paused")              # ...and paused again by hand
    _tick(o, clock, secs=60, n=10)
    assert o.controller.sent == ["pause"] and o.guards["person"]["acted"] is None


def test_a_command_from_someone_else_hands_the_pause_back(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, RESUME_ON)
    o.yard["person"] = True
    _tick(o, clock, n=2)
    o.yard["person"] = False
    o.rec.foreign_cmd_at = clock[0] + 1      # e.g. the phone app sent pause/stop, state flags unchanged
    _tick(o, clock, secs=60, n=10)
    assert o.controller.sent == ["pause"]


def test_a_different_plan_session_is_never_resumed(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, RESUME_ON)
    o.yard["person"] = True
    _tick(o, clock, n=2)
    o.yard["person"] = False
    o.rec.feedback = {"state": -1, "msg": "no plan working", "data": ""}
    _tick(o, clock, secs=60, n=10)
    assert o.controller.sent == ["pause"]


def test_a_pause_held_too_long_is_left_to_a_person(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, RESUME_ON)
    o.yard["person"] = True
    _tick(o, clock, n=2)
    _tick(o, clock, secs=600, n=7)           # person stays 70 min
    o.yard["person"] = False
    _tick(o, clock, secs=60, n=10)
    assert o.controller.sent == ["pause"]


def test_ignore_switches_a_guard_off(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {"guard_person": "ignore"})
    o.yard["person"] = True
    _tick(o, clock)
    assert o.controller.sent == [] and o.notifier.msgs == [] and "person" not in o.guards


def test_nothing_is_paused_while_the_robot_is_already_heading_home(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {})
    o.rec.set(RETURNING, "returning")
    o.yard["person"] = True
    _tick(o, clock, n=3)
    assert o.controller.sent == [] and len(o.notifier.msgs) == 1 and "nothing sent" in o.notifier.msgs[0][1]


def test_a_flapping_detection_notifies_once(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {"guard_person": "notify"})
    for live in (True, False, True, False, True):
        o.yard["person"] = live
        _tick(o, clock, secs=10)
    assert len(o.notifier.msgs) == 1


def test_a_person_pressing_pause_in_the_panel_keeps_it_paused(tmp_path, monkeypatch):
    """Second review: the panel's own commands were treated as 'ours' and the guard kept ownership."""
    o, clock = _ops(tmp_path, monkeypatch, RESUME_ON)
    o.yard["person"] = True
    _tick(o, clock, n=2)
    o.yard["person"] = False
    _tick(o, clock, secs=60)
    o.controller.last_human_cmd_at = clock[0]          # pressed Pause on an already paused robot: no state change
    _tick(o, clock, secs=60, n=10)
    assert o.controller.sent == ["pause"]


def test_a_human_resume_during_a_detection_snoozes_the_guard_for_that_detection(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {})
    o.yard["person"] = True                             # e.g. a lawn ornament Frigate keeps seeing
    _tick(o, clock, n=2)
    o.controller.human("resume")
    _tick(o, clock, n=20)
    assert o.controller.sent == ["pause"] and o.guards["person"]["snoozed"]
    assert any("overridden" in m[1] for m in o.notifier.msgs)
    o.yard["person"] = False
    _tick(o, clock, secs=60)
    o.yard["person"] = True                             # a new detection: the guard applies again
    _tick(o, clock)
    assert o.controller.sent == ["pause", "pause"]


def test_a_resume_snoozes_the_guard_even_when_its_pause_was_handed_back_to_people(tmp_path, monkeypatch):
    """#53, seen on a real mow: the phone app was open, so the guard's confirmed pause was not "ours" and was handed
    back to people in the same tick. That cleared what the guard had done, so a person's resume did not count as an
    override and the guard paused the robot again within seconds, over and over."""
    o, clock = _ops(tmp_path, monkeypatch, {})
    o.yard["person"] = True                             # the owner, out at the robot
    _tick(o, clock)                                     # the guard sends its pause
    o.rec.app_stream_at = clock[0] + 1                  # the phone app is open: the pause is not the guard's to own
    _tick(o, clock)                                     # confirmed, and handed back to people at once
    assert o.controller.sent == ["pause"] and o.guards["person"]["owner"] is None
    o.controller.human("resume")
    _tick(o, clock, n=20)
    assert o.controller.sent == ["pause"] and o.guards["person"]["snoozed"]


def test_a_command_from_before_the_guard_acted_is_not_an_override(tmp_path, monkeypatch):
    """Review of #53: once the guard has held the robot, only a person's command after that overrides it."""
    o, clock = _ops(tmp_path, monkeypatch, {})
    o.controller.deaf = True                            # the robot takes a moment to act on the pause
    o.yard["person"] = True
    _tick(o, clock)                                     # the guard's pause goes out
    o.controller.last_human_cmd_at = clock[0] + 1       # someone sends something unrelated meanwhile
    o.rec.app_stream_at = clock[0] + 1                  # with the phone app open: the pause won't be "ours"
    clock[0] += 2
    o.rec.set(PAUSED, "paused")                         # the pause lands
    _tick(o, clock)                                     # confirmed, and handed back to people at once
    assert o.guards["person"]["held"] and o.guards["person"]["acted"] is None
    o.controller.deaf = False
    o.rec.set(MOWING, "mowing")                         # it carries on mowing, with no new command from a person
    _tick(o, clock, secs=25)
    assert o.controller.sent == ["pause", "pause"] and not o.guards["person"]["snoozed"]


def test_driving_by_hand_without_a_plan_is_not_paused(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {})
    o.rec.in_plan = False
    o.rec.set({"speed_l": 0.5, "speed_r": 0.5}, "moving")
    o.yard["person"] = True
    _tick(o, clock, n=10)
    assert o.controller.sent == []


def test_a_guard_dock_the_robot_leaves_by_itself_is_rechecked(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {"guard_person": "dock"})
    o.yard["person"] = True
    _tick(o, clock, n=2)
    assert o.controller.sent == ["cmd_recharge"] and o.guards["person"]["acted"] == "dock"
    o.yard["person"] = False
    _tick(o, clock, secs=60)
    o.controller.checks_pass = False
    o.rec.set(TRANSIT, "transit")                       # charging break over, heading back out on its own
    _tick(o, clock, n=2)
    assert o.controller.sent == ["cmd_recharge", "cmd_recharge"]
    o.controller.checks_pass = True
    o.rec.set(TRANSIT, "transit")
    _tick(o, clock, secs=30, n=2)
    assert o.controller.sent == ["cmd_recharge", "cmd_recharge"] and o.guards["person"]["acted"] is None


def test_a_resume_that_would_fail_its_checks_is_not_sent(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, RESUME_ON)
    o.yard["person"] = True
    _tick(o, clock, n=2)
    o.yard["person"] = False
    o.controller.checks_pass = False
    _tick(o, clock, secs=60, n=15)
    assert o.controller.sent == ["pause"]
    assert sum("not resuming yet" in m[0] for m in o.notifier.msgs) == 1


# -- stuck detection and the live track ------------------------------------------------------------------------
def test_working_without_getting_anywhere_counts_as_stalled(tmp_path):
    """2026-10-05 10:07-10:19: 'mowing', wheels at 0, position frozen, stuck flag 0."""
    r = _bare_recorder(Store(str(tmp_path)))
    r._stall_anchor, r.stalled_s, r._stall_seen = None, 0.0, 0.0
    for i in range(0, 200, 10):                       # mowing in a straight line: never stalled
        r._track_stall(i, {**MOWING, "x": i * 0.4, "y": 0.0}, "mowing")
        assert r.stalled_s == 0
    for i in range(200, 420, 10):                     # stopped, rocking 80 cm back and forth
        r._track_stall(i, {**MOWING, "x": 80.0 + 0.8 * ((i // 10) % 2), "y": 0.3, "speed_l": 0}, "mowing")
    assert r.stalled_s >= 180
    r._track_stall(430, {**PAUSED, "x": 80.0, "y": 0.3}, "paused")   # paused on purpose: not stuck
    assert r.stalled_s == 0


def test_track_rows_and_run_points_are_incremental(tmp_path):
    st = Store(str(tmp_path))
    rid = st.start_run(100, 5, None)
    for ts in (100, 101, 102):
        st.add_telemetry({"ts": ts, "run_id": rid, "x": 1.0, "y": 2.0, "lon": -76.5, "lat": 39.0, "rtk_status": 4})
    st.add_telemetry({"ts": 103, "run_id": rid, "x": 1.0, "y": 2.0, "lon": None, "lat": None})
    assert [r[0] for r in st.track_rows(100.5)] == [101, 102]
    assert [p["ts"] for p in st.run_points(rid, since=101)] == [102, 103]


def test_no_stall_is_measured_across_a_gap_in_the_data(tmp_path):
    r = _bare_recorder(Store(str(tmp_path)))
    r._stall_anchor, r.stalled_s, r._stall_seen = None, 0.0, 0.0
    r._track_stall(100, {**MOWING, "x": 0.0, "y": 0.0}, "mowing")
    r._track_stall(460, {**MOWING, "x": 0.5, "y": 0.0}, "mowing")   # 6 min silence, back near the same spot
    assert r.stalled_s == 0


FLOAT_STOPPED = {**MOWING, "speed_l": 0, "speed_r": 0, "rtk_status": 5, "blade_rpm_l": 0, "blade_rpm_r": 0}


def test_standing_still_on_float_with_the_blades_stopped_is_waiting_for_a_fix(tmp_path):
    """2026-10-18 08:54-09:05: mowing, RTK float, blades 0 rpm, HaLow fine; it drove on when the fix came back."""
    r = _bare_recorder(Store(str(tmp_path)))
    for i in range(0, 400, 10):
        r._track_stall(i, {**FLOAT_STOPPED, "x": 5.0, "y": 5.0}, "mowing")
    r.t, r.update_at = {**FLOAT_STOPPED, "x": 5.0, "y": 5.0}, 390
    tr = r.trouble(400)
    assert tr["rtk_wait"] and not tr["stalled"] and tr["stalled_s"] >= 180
    fixed = {**FLOAT_STOPPED, "x": 5.0, "y": 5.0, "rtk_status": 4}
    r._track_stall(400, fixed, "mowing")                  # a fix for a moment: still waiting, never "stuck"
    r._track_stall(405, {**FLOAT_STOPPED, "x": 5.0, "y": 5.0}, "mowing")
    r._track_stall(406, fixed, "mowing")
    r.t, r.update_at = fixed, 406
    assert r.trouble(406)["rtk_wait"] and not r.trouble(406)["stalled"]
    r._track_stall(416, fixed, "mowing")                  # the fix held: the robot gets a fresh 3 min to move
    r.update_at = 416
    assert r.trouble(416)["stalled_s"] == 0 and not r.trouble(416)["rtk_wait"]
    for i in range(420, 620, 10):                         # ...and if it still does not, it is stuck after all
        r._track_stall(i, fixed, "mowing")
    r.update_at = 610
    assert r.trouble(610)["stalled"]


def test_blades_turning_without_a_fix_is_still_stuck(tmp_path):
    r = _bare_recorder(Store(str(tmp_path)))
    spinning = {**FLOAT_STOPPED, "x": 5.0, "y": 5.0, "blade_rpm_l": -2800, "blade_rpm_r": -2800}
    for i in range(0, 400, 10):
        r._track_stall(i, spinning, "mowing")
    r.t, r.update_at = spinning, 390
    assert r.trouble(400)["stalled"] and not r.trouble(400)["rtk_wait"]


def test_a_firmware_version_counts_only_once_it_persists(tmp_path):
    """2026-10-18 11:54: chassis 2.1.46 -> 74.10.180 -> 2.1.46 within 15 s was two firmware_changed events."""
    from app import telemetry
    st = Store(str(tmp_path))
    r = _bare_recorder(st)
    path = next(iter(telemetry.FIRMWARE.items()))
    name, keys = path[0], path[1].split(".")

    def report(version, ts):
        snap = r.snapshot = {}
        for k in keys[:-1]:
            snap = snap.setdefault(k, {})
        snap[keys[-1]] = version
        r._track_firmware(ts)
    report("2.1.46", 0)
    assert r.firmware[name] == "2.1.46"                    # first sighting: taken at once
    for ts in range(10, 25, 1):
        report("74.10.180", ts)
    report("2.1.46", 26)
    assert r.firmware[name] == "2.1.46" and not [e for e in st.events(50) if e["kind"] == "firmware_changed"]
    for ts in range(30, 100, 5):
        report("2.1.47", ts)
    assert r.firmware[name] == "2.1.47"
    assert [e["detail"] for e in st.events(50) if e["kind"] == "firmware_changed"] == [f"{name}: 2.1.46 -> 2.1.47"]


def test_coverage_of_a_resumed_run_counts_its_earlier_plan_session_runs(tmp_path):
    st = Store(str(tmp_path))
    early = _stored_run(st, 1000, 2000, "1:5", 10, 9.0)
    later = _stored_run(st, 60000, 61000, "1:5", 10, 9.0)
    other = _stored_run(st, 70000, 71000, "1:6", 10, 9.0)
    assert [x["id"] for x in st.earlier_session_runs(later)] == [early]
    assert st.earlier_session_runs(other) == [] and st.earlier_session_runs(early) == []
    r = _bare_recorder(st)
    r.gps_ref, r._analysing, r.map_raw = (43.0, -88.0), set(), None
    seen = {}

    def analyze(points, areas, nogos, deck, before=None):
        seen["before"] = before
        return {"v": __import__("app.coverage", fromlist=["VERSION"]).VERSION, "areas": []}
    import app.coverage as cov
    real, cov.analyze = cov.analyze, analyze
    try:
        res = asyncio.run(r.analyze_run(later))
    finally:
        cov.analyze = real
    assert res["earlier_runs"] == [early] and len(seen["before"]) == 1 and len(seen["before"][0]) == 10
    st.analysis_set(other, {"areas": []})                  # an analysis from before VERSION is worked out again
    cov.analyze = analyze
    try:
        assert asyncio.run(r.analyze_run(other))["earlier_runs"] == []
    finally:
        cov.analyze = real


class AlertRec:
    def __init__(self):
        self.t, self.activity, self.run_id = dict(MOWING, x=0.0, y=0.0), "mowing", 7
        self.tr = {"stalled": False, "rtk_wait": False, "stalled_s": 0, "lost_contact": False, "data_age_s": 1}
        self.update_at = 1000.0

    def trouble(self, now=None):
        return self.tr

    def status(self):
        return {"state": self.activity}


def _alert_ops(tmp_path):
    from types import SimpleNamespace
    o = Ops.__new__(Ops)
    o.rec, o.notifier, o.cfg = AlertRec(), FakeNotifier(), SimpleNamespace(panel_url="")
    o._stuck_at, o._lost_since, o._codes, o._pause_code, o._rtk_wait = None, None, {}, None, False
    return o


def test_waiting_for_a_fix_says_so_instead_of_may_be_stuck(tmp_path):
    o = _alert_ops(tmp_path)
    o.rec.t.update(rtk_status=5)
    o.rec.tr.update(rtk_wait=True, stalled_s=200)
    for _ in range(3):
        asyncio.run(o._check_robot_alerts())
    assert [m[0] for m in o.notifier.msgs] == ["Yarbo is waiting for an RTK fix"]
    o.rec.tr.update(rtk_wait=False, stalled_s=0, lost_contact=True, data_age_s=200)
    asyncio.run(o._check_robot_alerts())                # no data: nothing to say about the fix
    assert [m[0] for m in o.notifier.msgs] == ["Yarbo is waiting for an RTK fix", "No data from Yarbo"]
    o.rec.t.update(rtk_status=4)
    o.rec.tr.update(lost_contact=False, data_age_s=1)
    asyncio.run(o._check_robot_alerts())
    assert o.notifier.msgs[-2][0] == "Yarbo is no longer waiting for an RTK fix" and "fixed" in o.notifier.msgs[-2][1].lower()
    assert not any("stuck" in m[0] for m in o.notifier.msgs)


def test_stuck_alert_fires_once_and_clears_only_after_real_progress(tmp_path):
    o = _alert_ops(tmp_path)
    o.rec.tr.update(stalled=True, stalled_s=200)
    for _ in range(3):
        asyncio.run(o._check_robot_alerts())
    assert [m[0] for m in o.notifier.msgs] == ["Yarbo may be stuck"]
    o.rec.tr.update(stalled=False, stalled_s=0)
    o.rec.t.update(x=1.5)                             # rocked 1.5 m: anchor reset, but not unstuck
    asyncio.run(o._check_robot_alerts())
    assert len(o.notifier.msgs) == 1
    o.rec.t.update(x=4.0)
    asyncio.run(o._check_robot_alerts())
    assert o.notifier.msgs[-1][0] == "Yarbo is moving again"


def test_pausing_a_stuck_robot_says_the_alert_cleared(tmp_path):
    o = _alert_ops(tmp_path)
    o.rec.tr.update(stalled=True, stalled_s=200)
    asyncio.run(o._check_robot_alerts())
    o.rec.tr.update(stalled=False, stalled_s=0)
    o.rec.activity = "paused"
    asyncio.run(o._check_robot_alerts())
    assert o.notifier.msgs[-1] == ("Stuck alert cleared", "the robot is now paused")


def test_lost_contact_alerts_once_and_reports_the_gap(tmp_path):
    o = _alert_ops(tmp_path)
    o.rec.tr.update(lost_contact=True, data_age_s=300)
    asyncio.run(o._check_robot_alerts())
    asyncio.run(o._check_robot_alerts())
    o.rec.tr.update(lost_contact=False)
    o.rec.update_at = 1000.0 + 420
    asyncio.run(o._check_robot_alerts())
    assert [m[0] for m in o.notifier.msgs] == ["No data from Yarbo", "Yarbo is reporting again"]
    assert "7 min" in o.notifier.msgs[-1][1] and o.notifier.msgs[-1][1]


def test_a_failed_resume_check_backs_off_for_five_minutes(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, RESUME_ON)
    calls = []
    real = o.controller.preview

    async def counting(*a, **k):
        calls.append(clock[0])
        return await real(*a, **k)
    o.controller.preview = counting
    o.yard["person"] = True
    _tick(o, clock, n=2)
    o.yard["person"] = False
    o.controller.checks_pass = False
    _tick(o, clock, secs=60, n=16)                   # 16 min
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert len(calls) >= 3 and all(g >= 300 for g in gaps)


def test_every_pause_reason_reads_as_paused_and_keeps_the_plan_open():
    from app import telemetry
    for pp in (1, 3, 4, 5, 6, 7, 9):
        t = {**PAUSED, "planning_paused": pp}
        assert activity(t) == "paused" and plan_flags_active(t) is True
    assert activity({**RETURNING, "recharging": 2}) == "returning"
    assert telemetry.code_label(telemetry.PLAN_ERRORS, -12) == "WP005 Failed to calculate route"
    assert telemetry.code_label(telemetry.PLAN_ERRORS, -99) == "error -99"
    assert telemetry.code_label(telemetry.PLAN_ERRORS, 1) is None


def test_the_robots_own_stuck_pause_and_plan_errors_alert(tmp_path):
    o = _alert_ops(tmp_path)
    o.rec.t["planning_paused"] = 6
    o.rec.activity = "paused"
    asyncio.run(o._check_robot_alerts())
    assert o.notifier.msgs[-1][0] == "Yarbo may be stuck" and "reports it is stuck" in o.notifier.msgs[-1][1]
    o.rec.status = lambda: {"state": "idle", "plan_error": "WP005 Failed to calculate route"}
    asyncio.run(o._check_robot_alerts())
    asyncio.run(o._check_robot_alerts())
    assert sum(1 for m in o.notifier.msgs if "Plan error" in m[0]) == 1


def test_an_estop_or_stuck_pause_on_top_of_a_guard_pause_is_never_resumed(tmp_path, monkeypatch):
    """Review: codes 3-9 all read as "paused", so ownership must also require the guard's own pause code (1)."""
    for code in (4, 6, 9):
        o, clock = _ops(tmp_path / str(code), monkeypatch, RESUME_ON)
        o.yard["person"] = True
        _tick(o, clock, n=2)
        o.yard["person"] = False
        o.rec.t["planning_paused"] = code            # same activity string, different reason
        _tick(o, clock, secs=60, n=12)
        assert o.controller.sent == ["pause"], code


def test_a_lingering_reason_code_does_not_hide_a_mowing_robot():
    assert activity({**MOWING, "planning_paused": 9}) == "mowing"
    assert activity({**PAUSED, "planning_paused": 9}) == "paused"
    assert activity({**MOWING, "planning_paused": 1}) == "paused"      # 1 is the plan's own pause flag


def test_robot_error_codes_are_not_re_announced_after_a_restart(tmp_path):
    o = _alert_ops(tmp_path)
    o.rec.status = lambda: {"state": "idle", "plan_error": "WP005 Failed to calculate route"}
    asyncio.run(o._check_robot_alerts())
    assert not any("Plan error" in m[0] for m in o.notifier.msgs)
    o.rec.status = lambda: {"state": "idle", "plan_error": "WP006 Outside mapped area"}
    asyncio.run(o._check_robot_alerts())
    assert sum(1 for m in o.notifier.msgs if "Plan error" in m[0]) == 1


# Follow-cam: which camera, and whether any camera's coverage holds the robot (Live's camera layout falls back to the
# map when none does). Two square coverages side by side, robot positions set directly on the fake recorder.
SQ = lambda x0: [[x0, 0], [x0 + 1, 0], [x0 + 1, 1], [x0, 1], [x0, 0]]


def _follow(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {})
    o.store.set_setting("camera_polygons", {"east": SQ(0), "west": SQ(2)})
    o.store.set_setting("yard_cameras", ["east", "west"])
    o._follow, o._follow_next = None, (None, 0.0)
    pos = {"lon": 0.5, "lat": 0.5}
    o.rec.status = lambda: {"robot": {"position": pos}}
    return o, clock, pos


def test_follow_camera_reports_whether_a_coverage_holds_the_robot(tmp_path, monkeypatch):
    o, clock, pos = _follow(tmp_path, monkeypatch)
    assert o.follow_camera()["camera"] == "east" and o.follow_camera()["covered"] is True
    o._follow = None
    pos.update(lon=1.5)                      # between the two coverages
    d = o.follow_camera()
    assert d["covered"] is False and d["camera"] == "east"   # first yard camera, flagged as not seeing the robot
    o._follow = None
    pos.update(lon=None, lat=None)           # no position at all
    assert o.follow_camera()["covered"] is False


def test_follow_camera_holds_a_coverage_change_on_the_same_camera(tmp_path, monkeypatch):
    o, clock, pos = _follow(tmp_path, monkeypatch)
    assert o.follow_camera()["covered"] is True
    pos.update(lon=1.5)                      # leaves east's coverage; the fallback is east too
    assert o.follow_camera()["covered"] is True      # held: no flip to the map on one stray fix
    clock[0] += o.FOLLOW_HOLD_S - 1
    assert o.follow_camera()["covered"] is True
    clock[0] += 2
    assert o.follow_camera()["covered"] is False     # out of view for the whole hold


def test_follow_camera_switches_camera_after_the_hold(tmp_path, monkeypatch):
    o, clock, pos = _follow(tmp_path, monkeypatch)
    assert o.follow_camera()["camera"] == "east"
    pos.update(lon=2.5)
    assert o.follow_camera()["camera"] == "east"
    clock[0] += o.FOLLOW_HOLD_S + 1
    d = o.follow_camera()
    assert d["camera"] == "west" and d["covered"] is True


def test_an_area_listed_finished_too_early_does_not_finish_the_plan():
    from app.runs import PlanStatus
    flicker = PlanStatus(active=True, area_ids=[11], current_area=11, finished_areas=[11], done_m2=600, total_m2=1760)
    assert not flicker.complete                                          # run 4, 15:00: listed finished at ~34 %
    assert PlanStatus(active=True, area_ids=[11], finished_areas=[11], done_m2=1700, total_m2=1760).complete
    assert PlanStatus(active=True, area_ids=[11, 34], finished_areas=[11, 34]).complete       # no progress given
    assert not PlanStatus(active=True, area_ids=[11, 34], finished_areas=[11]).complete


def test_a_command_the_robot_acts_on_late_is_logged_as_confirmed_late(tmp_path, monkeypatch):
    monkeypatch.setitem(control.CONFIRM, "resume", (0.1, control.CONFIRM["resume"][1], "running again"))
    monkeypatch.setattr(control, "LATE_WATCH_S", 2)
    t = dict(PAUSED)
    store, c = _controller(tmp_path, lambda: t)

    async def go():
        r = await c.send("resume")
        assert r["confirmed"] is False                    # the first verdict stands (the guards act on it)
        t.update(MOWING)                                  # the robot gets to it a little later
        await asyncio.sleep(0.3)
        return r

    asyncio.run(go())
    assert any(e["kind"] == "command_confirmed_late" for e in store.events(10))
    assert store.control_log()[0]["result"]["confirmed"] is True and store.control_log()[0]["result"]["late"]
    assert c.link.sent == ["resume"]                      # nothing sent again


def test_the_plan_blip_when_leaving_a_charging_break_does_not_end_the_run():
    d = RunDetector(idle_end_s=300)
    assert d.update(0, MOWING, True) == "start"
    assert d.update(10, CHARGE_BREAK, True) is None
    assert d.update(6000, CHARGE_BREAK, True) is None             # 1.7 h on the charger mid-plan
    assert d.update(6001, DOCKED, False) is None                  # run 4, 20:36: "no plan" for a moment...
    assert d.update(6006, DOCKED, False) is None
    assert d.update(6007, MOWING, True) is None                   # ...then it drives out: the same run


def test_a_plan_that_really_ends_during_a_hold_still_ends_the_run():
    d = RunDetector(idle_end_s=300)
    d.update(0, MOWING, True)
    d.update(10, CHARGE_BREAK, True)
    assert d.update(100, DOCKED, False) is None
    assert d.update(100 + PLAN_BLIP_S + 1, DOCKED, False) == "end"


def test_a_guard_notice_is_not_sent_again_after_a_restart(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {"guard_person": "notify"})
    o.yard["person"] = True
    _tick(o, clock, n=3)
    assert len(o.notifier.msgs) == 1
    o2, clock2 = _ops(tmp_path, monkeypatch, {"guard_person": "notify"})    # the panel restarted: same database
    clock2[0] = clock[0] + 20
    o2.yard["person"] = True
    _tick(o2, clock2, n=3)
    assert o2.notifier.msgs == []                                          # the same detection, already told
    o2.rec.run_id = 8                                                      # a different run: a new detection
    o2.guards.clear()
    _tick(o2, clock2)
    assert len(o2.notifier.msgs) == 1


def test_a_restart_still_sends_the_protective_pause(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {})
    o.yard["person"] = True
    _tick(o, clock, n=3)
    o2, clock2 = _ops(tmp_path, monkeypatch, {})
    clock2[0] = clock[0] + 20
    o2.yard["person"] = True
    _tick(o2, clock2, n=3)
    assert o2.controller.sent == ["pause"]                                 # only the notice is quiet


def test_a_person_who_comes_back_later_in_the_same_run_is_notified_again(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {"guard_person": "notify"})
    o.yard["person"] = True
    _tick(o, clock, n=3)
    o.yard["person"] = False
    _tick(o, clock, secs=40, n=2)                     # cleared (past the 30 s hysteresis)
    o.yard["person"] = True
    _tick(o, clock, n=2)                              # back two minutes later, no restart
    assert len(o.notifier.msgs) == 2


# -- the after-dark guard (#20) ----------------------------------------------------------------------------------
ANNAPOLIS = (38.97, -76.50)                       # a public landmark, not a yard
NIGHT = calendar.timegm((2026, 10, 20, 23, 30, 0))  # about an hour after sunset there
DAY = calendar.timegm((2026, 10, 20, 17, 0, 0))
SUNSET = calendar.timegm((2026, 10, 20, 22, 18, 0))  # about two minutes before sunset there


def _dark_ops(tmp_path, monkeypatch, when, gates=None):
    o, clock = _ops(tmp_path, monkeypatch, gates or {})
    clock[0] = when
    o.rec.gps_ref = ANNAPOLIS
    o.rec.set(RETURNING, "charging_break")         # charging mid-plan
    _tick(o, clock)
    return o, clock


def _heads_out(o, clock, n=2):
    o.rec.set(TRANSIT, "transit")                  # full again: heading back out by itself
    _tick(o, clock, n=n)


def test_leaving_the_charger_after_dark_is_sent_home(tmp_path, monkeypatch):
    o, clock = _dark_ops(tmp_path, monkeypatch, NIGHT)
    _heads_out(o, clock)
    assert o.controller.sent == ["cmd_recharge"]
    assert any("after dark" in t for t, _ in o.notifier.msgs)
    assert any("next schedule window" in b for _, b in o.notifier.msgs)


def test_an_idle_moment_on_the_way_out_still_counts(tmp_path, monkeypatch):
    o, clock = _dark_ops(tmp_path, monkeypatch, NIGHT)
    o.rec.set(PAUSED, "idle")
    _tick(o, clock, secs=20)
    _heads_out(o, clock)
    assert o.controller.sent == ["cmd_recharge"]


def test_a_second_departure_the_same_night_is_sent_home_again(tmp_path, monkeypatch):
    o, clock = _dark_ops(tmp_path, monkeypatch, NIGHT)
    _heads_out(o, clock)
    o.rec.set(RETURNING, "charging_break")
    _tick(o, clock, secs=600, n=2)
    _heads_out(o, clock, n=3)
    assert o.controller.sent.count("cmd_recharge") >= 2


def test_leaving_the_charger_in_daylight_is_left_alone(tmp_path, monkeypatch):
    o, clock = _dark_ops(tmp_path, monkeypatch, DAY)
    _heads_out(o, clock, n=3)
    assert o.controller.sent == []


def test_leaving_just_before_sunset_is_left_alone_after_it(tmp_path, monkeypatch):
    o, clock = _dark_ops(tmp_path, monkeypatch, SUNSET)
    _heads_out(o, clock, n=1)
    _tick(o, clock, secs=60, n=5)                  # sunset passes while it mows
    assert o.controller.sent == []


def test_a_mow_that_runs_into_dusk_without_charging_is_left_alone(tmp_path, monkeypatch):
    o, clock = _ops(tmp_path, monkeypatch, {})
    clock[0] = NIGHT
    o.rec.gps_ref = ANNAPOLIS
    o.rec.set(MOWING, "mowing")
    _tick(o, clock, n=3)
    assert o.controller.sent == []


def test_a_person_who_resumes_it_after_dark_is_not_overruled(tmp_path, monkeypatch):
    o, clock = _dark_ops(tmp_path, monkeypatch, NIGHT)
    o.controller.human("resume")                   # the person's own resume from the charging break
    _tick(o, clock, n=3)
    assert o.controller.sent == []


def test_the_after_dark_guard_can_only_notify(tmp_path, monkeypatch):
    o, clock = _dark_ops(tmp_path, monkeypatch, NIGHT, {"guard_after_dark": "notify"})
    _heads_out(o, clock)
    assert o.controller.sent == [] and any("after dark" in t for t, _ in o.notifier.msgs)


def test_a_stored_pause_acts_as_dock_after_dark(tmp_path, monkeypatch):
    o, clock = _dark_ops(tmp_path, monkeypatch, NIGHT, {"guard_after_dark": "pause"})
    _heads_out(o, clock)
    assert o.controller.sent == ["cmd_recharge"]


def test_a_restart_after_it_left_keeps_the_guard(tmp_path, monkeypatch):
    o, clock = _dark_ops(tmp_path, monkeypatch, NIGHT)
    o.controller.refuse = 5                        # the dock does not get through before the restart
    _heads_out(o, clock)
    o.guards, o._charged_at, o._left_at, o._night_out, o._dark_kept = {}, 0.0, None, None, None   # a new process
    o.controller.refuse = 0
    _tick(o, clock, n=2)
    assert o.controller.sent == ["cmd_recharge"]


def test_without_the_yards_location_the_after_dark_guard_stays_quiet(tmp_path, monkeypatch):
    o, clock = _dark_ops(tmp_path, monkeypatch, NIGHT)
    o.rec.gps_ref = None
    _heads_out(o, clock)
    assert o.controller.sent == []


def _elevation(ts, lat, lon):
    """The sun's elevation in degrees (NOAA's general solar position formulas): an independent check on is_dark."""
    t = time.gmtime(ts)
    g = 2 * math.pi / 365 * (t.tm_yday - 1 + (t.tm_hour - 12 + t.tm_min / 60) / 24)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g)
                    - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    tst = t.tm_hour * 60 + t.tm_min + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    return math.degrees(math.asin(math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)))


@pytest.mark.parametrize("place", [ANNAPOLIS, (41.88, -87.63), (39.74, -104.99), (47.61, -122.33), (51.5, -0.13),
                                   (-33.87, 151.21)])
def test_is_dark_agrees_with_the_suns_elevation_all_year(place):
    """Every 20 minutes for a year; samples within 10 minutes of sunrise or sunset are left out (the two methods
    differ by a minute or two). Before the unwrap fix, US places had one whole night a year read as light."""
    start, wrong = calendar.timegm((2026, 1, 1, 0, 0, 0)), []
    for k in range(0, 365 * 72):
        ts = start + k * 1200
        el = _elevation(ts, *place)
        if abs(el + 0.833) < 2.0:
            continue
        if conditions.is_dark(ts, *place) != (el < -0.833):
            wrong.append(time.strftime("%m-%d %H:%M", time.gmtime(ts)))
    assert not wrong, wrong[:5]


def test_the_late_summer_midnight_ut_night_is_dark():
    assert conditions.is_dark(calendar.timegm((2026, 8, 16, 1, 0, 0)), *ANNAPOLIS) is True
