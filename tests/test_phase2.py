import asyncio
from datetime import datetime

import pytest

from app import control, maintenance, scheduler
from tests.fakelink import FakeLink
from app import geo
from app.ops import point_in_ring
from app.store import Store


class FakeCloud:
    def __init__(self, state="connected"):
        self.state, self.sent = state, []

    def publish(self, cmd, payload):
        self.sent.append((cmd, payload))


def good_ctx(**over):
    ctx = {"snapshot_age_s": 5, "route_state": "connected", "plans": [{"id": 1, "name": "Mow All"}], "error_code": 0,
           "battery": 90, "running": False, "yard": {"ok": True, "detail": "clear", "events": []},
           "phone_app_active": False, "conditions": "Dry", "presence": True, "writing": False}
    ctx.update(over)
    return ctx


def make_controller(tmp_path, ctx=None, enabled=False, reply=None):
    store = Store(str(tmp_path))
    if enabled:
        store.set_setting("control_enabled", True)
    cloud = FakeCloud((ctx or good_ctx())["route_state"])

    async def context(args):
        return ctx or good_ctx()

    def expect(cmd):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(reply if reply is not None else {"topic": cmd, "state": 0, "msg": "ok"})
        return fut

    return store, cloud, control.Controller(store, FakeLink(cloud), context, expect)


def run(coro):
    return asyncio.run(coro)


# -- control: master switch, catalogue, audit ------------------------------------------------------------------------
def test_control_is_off_by_default_and_refusals_are_audited(tmp_path):
    store, cloud, c = make_controller(tmp_path)
    r = run(c.send("cmd_recharge"))
    assert not r["sent"] and "switched off" in r["refused"]
    assert cloud.sent == []
    log = store.control_log()
    assert log[0]["cmd"] == "cmd_recharge" and log[0]["sent"] == 0


@pytest.mark.parametrize("cmd", ["cmd_vel", "get_controller", "save_global_params", "light_ctrl", "set_working_state"])
def test_only_catalogue_commands_ever_go_out(tmp_path, cmd):
    store, cloud, c = make_controller(tmp_path, enabled=True)
    r = run(c.send(cmd, {"vel": 1.0}))
    assert not r["sent"] and cloud.sent == []


def test_the_cameras_go_on_with_state_1_and_nothing_else(tmp_path):
    store, cloud, c = make_controller(tmp_path, enabled=True)
    r = run(c.send("smart_vision_control"))
    assert r["sent"] and r["payload"] == {"state": 1}
    assert cloud.sent == [("smart_vision_control", {"state": 1})]
    r = run(c.send("smart_vision_control", {"state": 1}))
    assert r["sent"] and cloud.sent[-1] == ("smart_vision_control", {"state": 1})


@pytest.mark.parametrize("state", [0, 2, -1, None, "1", "0", True, False, 1.0, [1]])
def test_switching_the_cameras_off_is_refused(tmp_path, state):
    store, cloud, c = make_controller(tmp_path, enabled=True)
    r = run(c.send("smart_vision_control", {"state": state}))
    assert not r["sent"] and "only switches the cameras on" in r["refused"]
    assert cloud.sent == []
    assert run(c.send("smart_vision_control", {"state": state}, actor="token"))["sent"] is False
    assert cloud.sent == []


def test_the_cameras_can_go_on_while_a_plan_is_running(tmp_path):
    store, cloud, c = make_controller(tmp_path, ctx=good_ctx(running=True, battery=None), enabled=True)
    assert run(c.send("smart_vision_control", actor="user"))["sent"]


@pytest.mark.parametrize("actor", ["scheduler", "guard", "mcp", "token", "", "robot"])
def test_only_a_person_switches_the_cameras_on(tmp_path, actor):
    store, cloud, c = make_controller(tmp_path, enabled=True)
    r = run(c.send("smart_vision_control", actor=actor))
    assert not r["sent"] and "People only" in str(r["checks"]) and cloud.sent == []


def test_every_catalogue_command_is_confirmed_by_state_or_run_neutral():
    from app import recorder
    # The keep-awake is run-neutral too but deliberately not in the catalogue (tests/test_video_keepalive.py).
    assert set(control.CATALOGUE) == (set(control.CONFIRM) | control.RUN_NEUTRAL) - {control.KEEP_AWAKE}
    assert control.KEEP_AWAKE in control.RUN_NEUTRAL and control.KEEP_AWAKE not in control.CATALOGUE
    assert not set(control.CONFIRM) & control.RUN_NEUTRAL
    assert not control.RUN_NEUTRAL & set(control.SAFE_TO_REPEAT)
    assert not control.RUN_NEUTRAL & recorder.PEOPLE_COMMANDS


def test_the_cameras_do_not_hide_a_pauses_result(tmp_path):
    store, cloud, c = make_controller(tmp_path, ctx=good_ctx(running=True), enabled=True)

    async def go():
        await c.send("pause", wait="background")
        before = c.last
        await c.send("smart_vision_control", wait="background")
        assert c.last is before and c.last["cmd"] == "pause"
        await asyncio.sleep(0.1)

    run(go())


def test_switching_the_cameras_on_is_not_a_person_taking_over_a_run(tmp_path):
    store, cloud, c = make_controller(tmp_path, ctx=good_ctx(running=True), enabled=True)
    assert run(c.send("smart_vision_control"))["sent"]
    assert c.last_human_cmd_at == 0.0                  # the guards read this to see a person step in
    assert run(c.send("pause"))["sent"]
    assert c.last_human_cmd_at > 0.0                   # a real command still counts


def test_the_cameras_wait_for_control_the_robot_and_the_route(tmp_path):
    store, cloud, c = make_controller(tmp_path)
    assert "switched off" in run(c.send("smart_vision_control"))["refused"]
    store, cloud, c = make_controller(tmp_path, ctx=good_ctx(snapshot_age_s=None), enabled=True)
    assert not run(c.send("smart_vision_control"))["sent"]
    store, cloud, c = make_controller(tmp_path, ctx=good_ctx(route_state="no route"), enabled=True)
    assert not run(c.send("smart_vision_control"))["sent"] and cloud.sent == []


def test_payloads_match_the_official_sdk(tmp_path):
    store, cloud, c = make_controller(tmp_path, enabled=True)
    assert run(c.send("start_plan", {"plan_id": 1}))["sent"]
    assert run(c.send("cmd_recharge"))["sent"]
    assert run(c.send("pause"))["sent"]
    assert cloud.sent == [("start_plan", {"id": 1}), ("cmd_recharge", {"cmd": 2}), ("pause", {})]
    assert store.control_log()[0]["sent"] == 1


def test_start_plan_needs_its_argument_and_an_existing_plan(tmp_path):
    store, cloud, c = make_controller(tmp_path, enabled=True)
    assert "missing plan_id" in run(c.send("start_plan"))["refused"]
    assert "Plan exists" in run(c.send("start_plan", {"plan_id": 99}))["refused"]
    assert cloud.sent == []


@pytest.mark.parametrize("bad", [None, "", "abc", True, 0])
def test_bad_plan_ids_never_become_a_plan(tmp_path, bad):
    store, cloud, c = make_controller(tmp_path, enabled=True)
    r = run(c.send("start_plan", {"plan_id": bad}))
    assert not r["sent"] and cloud.sent == []
    assert run(c.preview("start_plan", {"plan_id": bad}))["would_send"] is False


# -- interlocks ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("over,needle", [
    ({"battery": 10}, "Battery"),
    ({"running": True}, "Not already running"),
    ({"error_code": 7}, "No active error"),
    ({"snapshot_age_s": 900}, "Robot online"),
    ({"route_state": "needs login"}, "Route to the robot"),
    ({"yard": {"ok": False, "detail": "person on back_yard", "events": [1]}}, "Yard clear"),
    ({"phone_app_active": True}, "Phone app"),
])
def test_hard_interlocks_refuse_even_with_override(tmp_path, over, needle):
    store, cloud, c = make_controller(tmp_path, good_ctx(**over), enabled=True)
    r = run(c.send("start_plan", {"plan_id": 1}, override=True))
    assert not r["sent"] and needle in r["refused"]
    assert cloud.sent == []


def test_wet_lawn_is_soft_for_a_person_and_hard_for_the_scheduler(tmp_path):
    store, cloud, c = make_controller(tmp_path, good_ctx(conditions="Wet"), enabled=True)
    assert not run(c.send("start_plan", {"plan_id": 1}))["sent"]
    assert run(c.send("start_plan", {"plan_id": 1}, override=True))["sent"]
    r = run(c.send("start_plan", {"plan_id": 1}, actor="scheduler", override=True))
    assert not r["sent"] and "Lawn dry" in r["refused"]


def test_unknown_presence_blocks_only_the_scheduler(tmp_path):
    store, cloud, c = make_controller(tmp_path, good_ctx(presence=None), enabled=True)
    assert run(c.send("start_plan", {"plan_id": 1}))["sent"]
    assert not run(c.send("start_plan", {"plan_id": 1}, actor="scheduler"))["sent"]


def test_stop_and_dock_only_need_the_link(tmp_path):
    ctx = good_ctx(battery=5, running=True, conditions="Wet", yard={"ok": False, "detail": "dog", "events": [1]})
    store, cloud, c = make_controller(tmp_path, ctx, enabled=True)
    assert run(c.send("stop"))["sent"] and run(c.send("cmd_recharge"))["sent"]


def test_unknown_frigate_does_not_block(tmp_path):
    store, cloud, c = make_controller(tmp_path, good_ctx(yard={"ok": None, "detail": "Frigate not set up", "events": []}),
                                      enabled=True)
    assert run(c.send("start_plan", {"plan_id": 1}))["sent"]


# -- scheduler -------------------------------------------------------------------------------------------------------
def test_window_validation_and_occurrence():
    w = scheduler.validate({"days": [1, 4, 9], "start": "9:00", "end": "14:00", "plan_id": "1"})
    assert w["days"] == [1, 4] and w["start"] == "09:00" and w["plan_id"] == 1
    tue_10 = datetime(2026, 10, 6, 10, 0)   # a Tuesday
    assert scheduler.active_occurrence(w, tue_10) == "2026-10-06"
    assert scheduler.active_occurrence(w, tue_10.replace(hour=15)) is None
    assert scheduler.active_occurrence(w, datetime(2026, 10, 7, 10, 0)) is None  # Wednesday
    with pytest.raises(ValueError):
        scheduler.validate({"start": "14:00", "end": "09:00"})


def test_tick_raises_one_pending_then_stops(tmp_path):
    store = Store(str(tmp_path))
    s = scheduler.Scheduler(store)
    s.save_windows([{"id": "w1", "days": [1], "start": "09:00", "end": "14:00", "plan_id": 1, "head": "mower"}])
    raised, started = [], []

    async def evaluate(w):
        return True, [], "ok"

    async def raise_pending(w, checks):
        raised.append(w["id"])
        store.set_setting("schedule_pending", {"window_id": w["id"], "date": "2026-10-06", "plan_id": 1,
                                               "created": 9e12})
        store.log_schedule(w["id"], "2026-10-06", "pending", {})

    async def auto(w):
        started.append(w)

    now = datetime(2026, 10, 6, 10, 0)
    for minute in range(3):
        run(s.tick(now.replace(minute=minute), 5, evaluate, raise_pending, auto))
    assert raised == ["w1"] and started == []


def test_window_that_never_passes_is_logged_as_skipped(tmp_path):
    store = Store(str(tmp_path))
    s = scheduler.Scheduler(store)
    s.save_windows([{"id": "w1", "days": [1], "start": "09:00", "end": "10:00", "plan_id": 1}])

    async def evaluate(w):
        return False, [], "Lawn dry: Wet"

    async def noop(*a):
        raise AssertionError("must not run")

    run(s.tick(datetime(2026, 10, 6, 9, 30), 5, evaluate, noop, noop))
    run(s.tick(datetime(2026, 10, 6, 10, 1), 5, evaluate, noop, noop))
    assert store.schedule_outcome("w1", "2026-10-06") == "skipped"
    assert store.schedule_log()[0]["detail"]["reason"] == "Lawn dry: Wet"


def test_snow_window_skips_while_the_mower_head_is_on(tmp_path):
    store = Store(str(tmp_path))
    s = scheduler.Scheduler(store)
    s.save_windows([{"id": "s1", "days": [1], "start": "05:00", "end": "07:00", "plan_id": 1, "head": "snow"}])

    async def evaluate(w):
        raise AssertionError("gates are not evaluated for the wrong head")

    run(s.tick(datetime(2026, 10, 6, 6, 0), 5, evaluate, evaluate, evaluate))
    assert "head" in s.last_eval["s1:2026-10-06"]["reason"]


# -- maintenance and geometry ------------------------------------------------------------------------------------------
def test_maintenance_progress():
    items = [{"id": "a", "name": "Blades", "unit": "blade_hours", "interval": 10, "baseline": 2, "done_at": 0},
             {"id": "b", "name": "Lens", "unit": "days", "interval": None, "baseline": 0, "done_at": 0},
             {"id": "c", "name": "Season", "unit": "head_change", "interval": None, "baseline": 0, "done_at": 0}]
    st = {i["id"]: i for i in maintenance.status(items, {"blade_hours": 11, "days": 5, "head_change": 1})}
    assert st["a"]["pct"] == 90 and st["a"]["due"] == "due soon"
    assert st["b"]["due"] == "set an interval"
    assert st["c"]["due"] == "due"


def test_blade_hours_count_only_spinning_samples(tmp_path):
    store = Store(str(tmp_path))
    run_id = store.start_run(0, 5, None)
    for i in range(3600):
        store.add_telemetry({"ts": float(i), "run_id": run_id, "blade_rpm_l": 3000 if i < 1800 else 0, "blade_rpm_r": 0})
    c = maintenance.counters_from(store, 0)
    assert c["blade_hours"] == pytest.approx(0.5, abs=0.01)
    assert c["mows"] == 1


def test_point_in_ring():
    ring = [[0, 0], [10, 0], [10, 10], [0, 10]]
    assert point_in_ring(5, 5, ring) and not point_in_ring(15, 5, ring)


def test_ring_area_and_double_click_cleanup():
    # ~10 m x ~10 m square at mid latitudes
    sq = [[-76.5, 39.0], [-76.5 + 10 / 86512, 39.0], [-76.5 + 10 / 86512, 39.0 + 10 / 111111],
          [-76.5, 39.0 + 10 / 111111]]
    assert geo.ring_area_m2(sq) == pytest.approx(100, rel=0.02)
    # what a double-click used to save: two corners and a repeat of the second
    flat = [[-76.5, 39.0], [-76.5001, 39.00005], [-76.5001, 39.00005]]
    cleaned = geo.clean_ring(flat)
    assert len(cleaned) == 2 and geo.ring_area_m2(cleaned) == 0
    assert geo.clean_ring(sq + [sq[0]]) == sq


# -- #57: no start or resume while a write to the robot is in progress -------------------------------------------
@pytest.mark.parametrize("cmd, args", [("start_plan", {"plan_id": 1}), ("resume", {})])
@pytest.mark.parametrize("writing", [True, None])                    # busy, or unknown: both block
def test_a_start_or_resume_waits_for_a_write_in_progress(cmd, args, writing):
    for actor in ("user", "scheduler", "guard", "mcp"):
        checks = control.check(cmd, args, good_ctx(writing=writing), control.GATE_DEFAULTS, actor)
        ok, reason = control.verdict(checks, override=True)
        assert not ok and "write to the robot" in reason


# -- API tokens: nobody is watching, so the strict reading, the same as an AI assistant ------------------------------
@pytest.mark.parametrize("cmd, args", [("start_plan", {"plan_id": 1}), ("resume", {})])
@pytest.mark.parametrize("over, name", [({"yard": {"ok": None, "detail": "Frigate not set up", "events": []}}, "Yard clear"),
                                        ({"phone_app_active": None}, "Phone app"),
                                        ({"conditions": None}, "Lawn dry"),
                                        ({"conditions": "Wet"}, "Lawn dry"),
                                        ({"presence": None}, "Someone home")])
def test_a_token_start_or_resume_fails_on_anything_unknown_and_cannot_override(cmd, args, over, name):
    checks = control.check(cmd, args, good_ctx(**over), control.GATE_DEFAULTS, "token")
    ok, reason = control.verdict(checks, override=True)
    assert not ok and name in reason


def test_a_token_resume_faces_an_active_error():
    checks = control.check("resume", {}, good_ctx(error_code=1), control.GATE_DEFAULTS, "token")
    assert not control.verdict(checks, override=False)[0]


def test_a_token_start_goes_ahead_when_every_check_passes():
    checks = control.check("start_plan", {"plan_id": 1}, good_ctx(), control.GATE_DEFAULTS, "token")
    assert control.verdict(checks, override=False)[0]


def test_a_token_send_ignores_override_and_is_audited_as_a_token(tmp_path):
    store, cloud, c = make_controller(tmp_path, good_ctx(presence=None), enabled=True)
    r = run(c.send("start_plan", {"plan_id": 1}, actor="token", override=True, wait=False))
    assert not r["sent"] and "Someone home" in r["refused"] and cloud.sent == []
    assert store.control_log()[0]["actor"] == "token"


def test_a_token_cannot_command_while_control_is_off(tmp_path):
    _, cloud, c = make_controller(tmp_path)
    r = run(c.send("pause", actor="token"))
    assert not r["sent"] and "switched off" in r["refused"] and cloud.sent == []


@pytest.mark.parametrize("cmd", ["pause", "stop", "cmd_recharge"])
def test_stopping_commands_are_never_held_back_by_a_write(cmd):
    checks = control.check(cmd, {}, good_ctx(writing=True), control.GATE_DEFAULTS, "user")
    assert control.verdict(checks, False)[0]


# -- #64: the robot's own schedules are read and shown, never written ----------------------------------------------
def test_the_robots_own_schedules_are_read_from_either_reply_shape():
    rec = {"id": 1, "name": "Schedule 1", "enable": True, "plan_id": 2, "week_day": 42,
           "start_time": "13:49:00", "end_time": "16:00:00"}
    for reply in ({"state": 0, "data": [rec]}, {"state": 0, "data": {"data": [rec]}}):
        out = scheduler.robot_schedules(reply)
        assert out == [{"id": 1, "name": "Schedule 1", "enabled": True, "plan_id": 2, "days": ["Mon", "Wed", "Fri"],
                        "start_utc": "13:49:00", "end_utc": "16:00:00"}]
    assert scheduler.robot_schedules({"state": 0, "data": []}) == []
    assert scheduler.robot_schedules({"state": -1, "msg": "failed"}) is None
    assert scheduler.robot_schedules({"state": 0, "data": "nope"}) is None


def test_the_panel_never_writes_robot_schedules():
    from app import mapwrite
    from app.transport_local import READ_ONLY_COMMANDS
    assert "read_schedules" in READ_ONLY_COMMANDS
    assert not {"save_schedule", "enable_schedule", "del_schedule"} & (set(mapwrite.WRITE_COMMANDS) | set(control.CATALOGUE))


# -- #69: no write to the robot in the moments after a start --------------------------------------------------------
def started_controller(tmp_path, cmd, args, state):
    store, cloud, c = make_controller(tmp_path, enabled=True)
    c._state = lambda: state
    r = run(c.send(cmd, args, wait=False))
    assert r["sent"]
    return c


@pytest.mark.parametrize("cmd, args", [("start_plan", {"plan_id": 1}), ("resume", {})])
def test_a_start_holds_writes_back_until_the_robot_shows_the_plan(tmp_path, cmd, args):
    state = {"planning": 0, "planning_paused": 0}                 # the robot has not reported the plan yet
    c = started_controller(tmp_path, cmd, args, state)
    why = c.start_settling()
    assert why and "try again in a moment" in why
    state.update(planning=1)                                      # it shows the plan: the plan checks take over
    assert c.start_settling() is None
    state.update(planning_paused=1)                               # paused again: the hold stays ended (latched)
    assert c.start_settling() is None and c.start_out is None


def test_a_start_the_robot_never_acts_on_holds_for_the_late_watch(tmp_path, monkeypatch):
    c = started_controller(tmp_path, "start_plan", {"plan_id": 1}, {"planning": 0})
    sent = c.start_out["at"]
    monkeypatch.setattr(control.time, "time", lambda: sent + control.LATE_WATCH_S - 1)
    assert c.start_settling()
    monkeypatch.setattr(control.time, "time", lambda: sent + control.LATE_WATCH_S)
    assert c.start_settling() is None


@pytest.mark.parametrize("cmd", ["pause", "stop", "cmd_recharge"])
def test_stopping_commands_do_not_hold_writes_back(tmp_path, cmd):
    c = started_controller(tmp_path, cmd, {}, {"planning": 0})
    assert c.start_settling() is None


def test_a_send_that_fails_half_way_still_holds_writes(tmp_path):
    store, cloud, c = make_controller(tmp_path, enabled=True)
    c._state = lambda: {"planning": 0}

    def broken(cmd, payload):
        raise ConnectionError("dropped mid-publish")
    c.link.command = broken
    r = run(c.send("start_plan", {"plan_id": 1}, wait=False))
    assert not r["sent"] and c.start_settling()


def test_a_start_is_refused_if_a_write_takes_the_lock_while_its_checks_run(tmp_path):
    from app import mapwrite
    store = Store(str(tmp_path))
    store.set_setting("control_enabled", True)
    cloud = FakeCloud()
    w = mapwrite.MapWriter(store, lambda cmd, payload: None, None, None, None, lambda: "")
    held = asyncio.Event()

    async def context(args):                    # a write takes the lock while the start's checks are running
        held.set()
        await asyncio.sleep(0.05)
        return good_ctx(writing=False)          # read before the write took it

    def expect(cmd):
        return asyncio.get_running_loop().create_future()

    c = control.Controller(store, FakeLink(cloud), context, expect)
    c.writing = w.lock.locked

    async def go():
        async def writer():
            await held.wait()
            async with w.lock:
                await asyncio.sleep(0.2)
        task = asyncio.create_task(writer())
        r = await c.send("start_plan", {"plan_id": 1}, wait=False)
        await task
        return r
    r = run(go())
    assert not r["sent"] and "write to the robot" in r["refused"]
    assert cloud.sent == [] and c.start_out is None


class _Robot:
    def __init__(self):
        self.sent = []

    def publish(self, cmd, payload):
        self.sent.append(cmd)

    def expect(self, cmd):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result({"state": 0, "msg": "ok"})
        return fut


def test_the_writer_refuses_every_write_at_its_first_send(tmp_path):
    from app import mapwrite
    bot = _Robot()
    w = mapwrite.MapWriter(Store(str(tmp_path)), bot.publish, bot.expect, None, None, lambda: "")
    w.start_block = lambda cmd: "Start plan was sent 2 s ago"

    async def go():
        for cmd in sorted(mapwrite.WRITE_COMMANDS - {"get_all_map_backup"}):
            async with w.lock:
                with pytest.raises(mapwrite.WriteRefused, match="sent 2 s ago"):
                    await w._send(cmd, {})
        async with w.lock:
            assert (await w._send("get_all_map_backup", {}))["ok"]                     # a read still goes
            assert (await w._send("save_mower_pathway_params", {}, rollback=True))["ok"]   # so does putting back
    run(go())
    assert bot.sent == ["get_all_map_backup", "save_mower_pathway_params"]


def test_a_write_is_never_stopped_half_way(tmp_path):
    """Asked once per hold of the lock: a hold that comes back after the first send does not cut a write short."""
    from app import mapwrite
    bot = _Robot()
    w = mapwrite.MapWriter(Store(str(tmp_path)), bot.publish, bot.expect, None, None, lambda: "")
    answers = [None, "Resume was sent 1 s ago"]
    w.start_block = lambda cmd: answers.pop(0) if answers else "held"

    async def go():
        async with w.lock:
            await w._send("save_nogozone", {})
            await w._send("save_pathway", {})
            await w._send("upload_cloud_map_backup", {})
        async with w.lock:                                          # the next write asks again
            with pytest.raises(mapwrite.WriteRefused):
                await w._send("save_nogozone", {})
    run(go())
    assert bot.sent == ["save_nogozone", "save_pathway", "upload_cloud_map_backup"]


def _ops(tmp_path, monkeypatch):
    from app import config
    from app.imagery import Imagery
    from app.main import Runtime
    from app.ops import Ops
    from app.recorder import Recorder

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    env = config.load()
    store = Store(env.data_dir)
    cfg = Runtime(env, store)
    return Ops(cfg, Recorder(cfg, store, Imagery(env.data_dir), None))


def test_ops_wires_the_start_and_the_writer_to_each_other(tmp_path, monkeypatch):
    import time
    o = _ops(tmp_path, monkeypatch)
    o.rec.plan_active = lambda ts=None: False
    assert o.map_writer.start_block("save_nogozone") is None
    o.controller.start_out = {"cmd": "start_plan", "at": time.time()}
    assert "Start plan was sent" in o.map_writer.start_block("save_nogozone")
    from app import mapwrite
    assert "Start plan was sent" in o.map_writer.start_block(mapwrite.BACKUP_CMD)   # even bookkeeping waits on a start

    async def held():
        async with o.map_writer.lock:
            return o.controller.writing()
    assert run(held()) is True and o.controller.writing() is False


def test_the_writer_asks_the_plan_state_again_before_it_sends(tmp_path, monkeypatch):
    """Map Studio apply and backup restore read for many seconds after their gates: a plan shown by then stops them."""
    from app import mapwrite
    o = _ops(tmp_path, monkeypatch)
    for active, word in ((True, "running"), (None, "unknown")):
        o.rec.plan_active = lambda ts=None, a=active: a
        assert word in o.write_hold("map_recovery") and word in o.write_hold("change_to_wifi")
        assert o.write_hold(mapwrite.BACKUP_CMD) is None                             # bookkeeping during a mow is fine
    o.rec.plan_active = lambda ts=None: False
    o.rec.t = {"planning": 1, "planning_paused": 0}           # the flags say planning though a reply says idle
    assert "running" in o.write_hold("save_nogozone")
    o.rec.t = {"planning": 0, "planning_paused": 0}
    assert o.write_hold("save_nogozone") is None
    o.rec.run_id = 7
    assert "running" in o.write_hold("save_nogozone")


def test_a_saved_serial_reaches_the_cloud_client_with_no_robot_address(tmp_path, monkeypatch):
    """Cloud-only: no local broker address, so apply_robot must still hand the configured serial to Ops."""
    o = _ops(tmp_path, monkeypatch)
    o.cfg.set("robot", {"sn": "YB0000DEMO0001"})
    assert not o.cfg.yarbo_host and o.cloud.sn is None
    run(o.rec.apply_robot())
    assert o.rec.sn == "YB0000DEMO0001" and o.cloud.sn == "YB0000DEMO0001"
