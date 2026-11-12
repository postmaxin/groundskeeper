"""The Cameras tile's keep-awake (§13): POST /api/video/keepalive, Controller.keep_awake (through send() and check()),
the time limit, the link, and telling the panel's own beats from the Yarbo app's. The robot streams its cameras only
while someone sends `set_working_state {"state":1}`; the panel sends one per accepted request. Driven through the
real app's middleware with fakes: no robot, no cloud, no network."""
import asyncio
import random
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, control, main, mcp_server, ops as ops_mod, recorder
from app.link import NoRoute, RobotLink
from app.recorder import Recorder
from app.commands import ReadOnlyViolation, check_command
from app.store import Store
from tests.fakelink import FakeLink
from tests.test_link import FakeCloud as LinkCloud, FakeRec as LinkRec
from tests.test_phase2 import FakeCloud, good_ctx

KA = control.KEEP_AWAKE


class FakeRec:
    def __init__(self, store):
        self.store = store


@pytest.fixture
def t(tmp_path, monkeypatch):
    monkeypatch.delenv("PANEL_AUTH", raising=False)
    monkeypatch.setattr(control, "KEEP_AWAKE_JITTER_S", 0)       # see test_each_beat_waits_a_random_moment_first
    store = Store(str(tmp_path))
    gate = auth.Auth(store)
    gate.announce()
    cloud = FakeCloud()
    clock = [10_000.0]
    monkeypatch.setattr(control.time, "monotonic", lambda: clock[0])
    ctx = good_ctx()

    async def context(args):
        return dict(ctx)

    def expect(cmd):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result({"topic": cmd, "state": 0, "msg": "ok"})
        return fut

    controller = control.Controller(store, FakeLink(cloud), context, expect)
    names = ("auth", "rec", "cfg", "ops")
    old = {k: getattr(main.app.state, k, None) for k in names}
    main.app.state.auth, main.app.state.rec = gate, FakeRec(store)
    main.app.state.cfg = SimpleNamespace(robot_service_url="https://robot-service.invalid")
    main.app.state.ops = SimpleNamespace(cloud=cloud, controller=controller)
    c = TestClient(main.app, base_url="http://panel.test")
    assert c.post("/api/auth/setup", json={"code": gate.setup_code, "password": "correct horse"}).status_code == 200
    yield SimpleNamespace(c=c, store=store, cloud=cloud, controller=controller, clock=clock, ctx=ctx)
    for k, v in old.items():
        setattr(main.app.state, k, v)


def beat(t, client=None, body=None):
    return (client or t.c).post("/api/video/keepalive", json=body if body is not None else {})


def play(t, client=None):
    """A person pressed Open (or a camera, or Try again): the tile's first beat opens the window."""
    return beat(t, client, {"start": True})


def ready(t):
    assert t.c.put("/api/settings/robot_video_enabled", json=True).status_code == 200
    t.store.set_setting("control_enabled", True)


def events(t, kind):
    return [e for e in t.store.events(100) if e["kind"] == kind]


def rows(t):
    return [r for r in t.store.control_log(500) if r["cmd"] == KA]


# -- the route's guards ------------------------------------------------------------------------------------------
def test_with_robot_video_off_it_is_refused_and_nothing_is_sent(t):
    t.store.set_setting("control_enabled", True)
    r = play(t)
    assert r.status_code == 403 and "off" in r.json()["detail"]
    assert t.cloud.sent == []


def test_with_control_off_it_says_so_and_sends_nothing(t):
    assert t.c.put("/api/settings/robot_video_enabled", json=True).status_code == 200
    r = play(t)
    assert r.status_code == 200 and r.json()["sent"] is False and r.json()["control_off"] is True
    assert "ended" not in r.json()                                   # Control off does not end the window
    assert t.cloud.sent == [] and events(t, "keep_awake_started") == []


def test_a_person_who_pressed_play_sends_one_fixed_beat(t):
    ready(t)
    r = play(t)
    assert r.status_code == 200 and r.json() == {"sent": True}
    assert r.headers["cache-control"] == "no-store"
    assert t.cloud.sent == [(KA, {"state": 1})]


def test_nothing_else_in_the_request_reaches_the_robot_and_state_zero_never_goes(t):
    ready(t)
    assert play(t).json()["sent"] is True
    bodies = [{"state": 0}, {"cmd": "cmd_vel", "vel": 1}, {"payload": {"state": 0}}, [0], "state 0", {"state": False},
              {"start": "yes"}]
    for body in bodies:
        t.clock[0] += 5
        assert beat(t, body=body).status_code == 200
    assert len(t.cloud.sent) == len(bodies) + 1
    assert all(c == KA and p == {"state": 1} for c, p in t.cloud.sent)


def test_it_is_rate_limited_for_everyone_together(t):
    ready(t)
    assert play(t).json()["sent"] is True
    t.clock[0] += control.KEEP_AWAKE_MIN_S - 0.5
    again = beat(t)
    assert again.status_code == 200 and again.json() == {"sent": False, "limited": True}
    t.clock[0] += 1.0
    assert beat(t).json()["sent"] is True
    assert len(t.cloud.sent) == 2
    assert len(rows(t)) == 2                                         # a request dropped by the limit is not audited


def test_the_rate_limit_counts_attempts_so_refusals_cannot_fill_the_audit_log(t):
    ready(t)
    for _ in range(10):                                              # no window: every beat is refused
        beat(t)
        t.clock[0] += 0.5
    assert len(rows(t)) == 2 and t.cloud.sent == []                  # one refusal per KEEP_AWAKE_MIN_S, not ten


def test_with_control_off_a_beat_is_not_audited_and_the_window_still_runs_out(t):
    ready(t)
    assert play(t).json()["sent"] is True
    t.store.set_setting("control_enabled", False)
    t.clock[0] += 4
    r = beat(t).json()
    assert r["control_off"] is True and "ended" not in r and len(rows(t)) == 1
    t.clock[0] += control.KEEP_AWAKE_MAX_S
    r = beat(t).json()
    assert r["control_off"] is True and r["ended"] is True           # a tile left open with Control off stops too
    assert len(rows(t)) == 1 and t.controller._awake_since is None


def test_an_api_token_is_refused_so_is_a_signed_out_caller(t):
    ready(t)
    made = t.c.post("/api/auth/tokens", json={"name": "script"}).json()["token"]
    bot = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {made}"})
    assert play(t, client=bot).status_code == 403
    assert ("POST", "/api/video/keepalive") not in auth.TOKEN_WRITES
    assert TestClient(main.app, base_url="http://panel.test").post("/api/video/keepalive", json={}).status_code == 401
    assert t.cloud.sent == []
    assert play(t).json()["sent"] is True                            # the person still can


def test_an_ai_assistant_cannot_reach_it(t):
    ready(t)
    assert not mcp_server.allowed("POST", "/api/video/keepalive")
    inner = TestClient(main.app, base_url="http://panel", headers={auth.INTERNAL_HEADER: auth.INTERNAL})
    assert play(t, client=inner).status_code == 403                 # no person's session
    assert t.cloud.sent == []


@pytest.mark.parametrize("actor", ["token", "mcp", "scheduler", "guard", "", "robot"])
def test_the_controller_beats_only_for_a_person(t, actor):
    ready(t)
    t.controller._awake_since = t.clock[0]                          # even inside a window a person opened
    r = asyncio.run(t.controller.keep_awake(actor=actor, start=True))
    assert r["sent"] is False and "People only" in r["refused"] and t.cloud.sent == []


def test_a_start_from_anyone_but_a_person_opens_no_window(t):
    ready(t)
    asyncio.run(t.controller.keep_awake(actor="token", start=True))
    assert t.controller._awake_since is None
    t.clock[0] += 5
    r = asyncio.run(t.controller.keep_awake(actor="user"))
    assert r["sent"] is False and "Within the time limit" in r["refused"] and t.cloud.sent == []


def test_a_missing_route_says_so_and_sends_nothing(t):
    ready(t)

    class NoCloud(FakeLink):
        def command(self, cmd, payload, route=None):
            raise NoRoute("the Yarbo cloud is signed out")

    t.controller.link = NoCloud(t.cloud)
    r = play(t)
    assert r.status_code == 200 and r.json()["sent"] is False and "signed out" in r.json()["error"]
    assert t.cloud.sent == [] and events(t, "keep_awake_started") == []


# -- point 1: it goes through check() and Controller.send like any command --------------------------------------------
def test_it_is_in_the_catalogue_with_a_fixed_payload():
    assert KA in control.CATALOGUE and control.CATALOGUE[KA]["args"] == ()
    assert control.payload_for(KA, {"state": 0, "x": 1}) == {"state": 1}
    assert KA in control.RUN_NEUTRAL and KA not in control.CONFIRM


def test_each_beat_gets_the_online_and_route_interlocks_and_an_audit_row(t):
    ready(t)
    assert play(t).json()["sent"] is True
    (row,) = rows(t)
    names = {c["name"] for c in row["result"]["checks"]}
    assert {"Robot online", "Route to the robot", "People only", "Keep awake only", "Robot video on",
            "Within the time limit"} <= names
    assert row["actor"] == "user" and row["route"] == "cloud"


def test_only_the_tile_keeps_it_awake_not_the_general_command_route(t):
    ready(t)
    send = lambda args: t.c.post("/api/control/send", json={"cmd": KA, "args": args, "confirm": True}).json()  # noqa
    assert send({})["sent"] is False                                   # nobody pressed play
    assert play(t).json()["sent"] is True
    for args in ({}, {"state": 1}, {"state": 0}):                      # even inside a window a person opened
        t.clock[0] += 5
        r = send(args)
        assert r["sent"] is False and "Within the time limit" in r["refused"]
    assert len(t.cloud.sent) == 1                                      # only the tile's beat: rate limit and jitter


def test_a_token_or_an_assistant_cannot_send_it_through_the_general_route(t):
    ready(t)
    assert play(t).json()["sent"] is True                              # even with a window open
    made = t.c.post("/api/auth/tokens", json={"name": "script"}).json()["token"]
    bot = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {made}"})
    t.clock[0] += 5
    r = bot.post("/api/control/send", json={"cmd": KA, "args": {}, "confirm": True})
    assert r.status_code in (200, 403) and (r.status_code == 403 or "People only" in r.json()["refused"])
    r = asyncio.run(t.controller.send(KA, {}, actor="mcp", wait=False))
    assert r["sent"] is False and "People only" in r["refused"]
    assert len(t.cloud.sent) == 1


def test_it_is_run_neutral_like_the_camera_switch(t):
    ready(t)
    human = t.controller.last_human_cmd_at
    assert play(t).json()["sent"] is True
    assert t.controller.last_human_cmd_at == human and t.controller.last is None


def test_the_command_check_has_no_special_case_for_it():
    import inspect
    from app import commands
    assert list(inspect.signature(check_command).parameters) == ["cmd"]
    assert "set_working_state" not in inspect.getsource(commands) and "KEEP_AWAKE" not in inspect.getsource(commands)
    check_command(KA)                                                  # allowed as a catalogue command
    with pytest.raises(ReadOnlyViolation):
        check_command("cmd_vel")


def test_no_side_door_is_left():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app"
    for p in root.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        assert not any(isinstance(n, (ast.Attribute, ast.FunctionDef)) and getattr(n, "attr", getattr(n, "name", ""))
                       == "keep_awake_beat" for n in ast.walk(tree)), p.name


# -- point 2: a limit the server keeps --------------------------------------------------------------------------------
def test_one_press_keeps_it_awake_for_fifteen_minutes_then_it_stops_until_play_again(t):
    ready(t)
    assert control.KEEP_AWAKE_MAX_S == 15 * 60
    assert play(t).json()["sent"] is True
    for _ in range(int(control.KEEP_AWAKE_MAX_S // 4) - 1):
        t.clock[0] += 4
        assert beat(t).json()["sent"] is True
    t.clock[0] += 8                                                    # past 15 min
    r = beat(t).json()
    assert r["sent"] is False and r["ended"] is True and "Within the time limit" in r["refused"]
    stopped = events(t, "keep_awake_stopped")
    assert len(stopped) == 1 and "time was up" in stopped[0]["detail"]
    sent = len(t.cloud.sent)
    for _ in range(3):                                                 # a forgotten tab keeps asking: nothing goes
        t.clock[0] += 4
        assert beat(t).json()["sent"] is False
    assert len(t.cloud.sent) == sent and len(events(t, "keep_awake_stopped")) == 1
    t.clock[0] += 4
    assert play(t).json()["sent"] is True                              # a person presses play again
    assert len(events(t, "keep_awake_started")) == 2


def test_another_press_starts_the_window_again(t):
    ready(t)
    assert play(t).json()["sent"] is True
    t.clock[0] += control.KEEP_AWAKE_MAX_S - 10
    assert play(t).json()["sent"] is True                              # e.g. a camera picked near the end
    t.clock[0] += 60
    assert beat(t).json()["sent"] is True


@pytest.mark.parametrize("change", [{"snapshot_age_s": 500}, {"snapshot_age_s": None}, {"route_state": "offline"}])
def test_the_robot_going_offline_ends_the_window(t, change):
    ready(t)
    assert play(t).json()["sent"] is True
    t.ctx.update(change)
    t.clock[0] += 4
    r = beat(t).json()
    assert r["sent"] is False and r["ended"] is True
    assert "offline or out of reach" in events(t, "keep_awake_stopped")[0]["detail"]
    t.ctx.update(good_ctx())                                           # back online: the old window does not come back
    t.clock[0] += 4
    assert beat(t).json()["sent"] is False
    t.clock[0] += 4
    assert play(t).json()["sent"] is True
    assert len(t.cloud.sent) == 2


def test_a_run_does_not_end_the_window(t):
    ready(t)
    assert play(t).json()["sent"] is True
    t.ctx.update(running=True)
    t.clock[0] += 4
    assert beat(t).json()["sent"] is True                              # the robot is awake for the run anyway


def test_control_switched_off_does_not_end_the_window_but_sends_nothing(t):
    ready(t)
    assert play(t).json()["sent"] is True
    t.store.set_setting("control_enabled", False)
    t.clock[0] += 4
    r = beat(t).json()
    assert r["sent"] is False and r["control_off"] is True and "ended" not in r
    t.store.set_setting("control_enabled", True)
    t.clock[0] += 4
    assert beat(t).json()["sent"] is True


def test_video_switched_off_in_settings_stops_the_beats(t):
    ready(t)
    assert play(t).json()["sent"] is True
    t.store.set_setting("robot_video_enabled", False)
    t.clock[0] += 4
    assert beat(t).status_code == 403
    r = asyncio.run(t.controller.keep_awake(actor="user"))             # a request that got past the route earlier
    assert r["sent"] is False and "Robot video on" in r["refused"]
    assert len(t.cloud.sent) == 1


def test_each_beat_waits_a_random_moment_first(t, monkeypatch):
    ready(t)
    monkeypatch.setattr(control, "KEEP_AWAKE_JITTER_S", 1.0)
    waits, asked = [], []
    real_sleep = asyncio.sleep

    async def sleep(s):
        waits.append(s)
        await real_sleep(0)

    monkeypatch.setattr(control.asyncio, "sleep", sleep)
    monkeypatch.setattr(control.random, "uniform", lambda a, b: asked.append((a, b)) or 0.37)
    t.controller._awake_since = t.clock[0]
    assert asyncio.run(t.controller.keep_awake())["sent"] is True
    assert asked == [(0, 1.0)] and waits == [0.37]


def test_the_jitter_and_limits_are_named():
    assert control.KEEP_AWAKE_JITTER_S == 1.0 and control.KEEP_AWAKE_MIN_S == 3.0 and control.KEEP_AWAKE_MAX_S == 900


# -- audit: one event at the start, one at the stop, an audit row per beat ---------------------------------------------
def test_one_event_when_the_beats_start_and_one_when_they_stop(t):
    ready(t)
    assert play(t).json()["sent"] is True
    for _ in range(5):
        t.clock[0] += 4
        assert beat(t).json()["sent"] is True
    assert len(events(t, "keep_awake_started")) == 1 and events(t, "keep_awake_stopped") == []
    assert len(rows(t)) == 6                                           # but every beat is in the audit log
    t.clock[0] += control.KEEP_AWAKE_GAP_S - 2                        # not long enough yet
    t.controller.keep_awake_tick()
    assert events(t, "keep_awake_stopped") == []
    t.clock[0] += 4
    t.controller.keep_awake_tick()
    t.controller.keep_awake_tick()                                     # said once
    assert len(events(t, "keep_awake_stopped")) == 1
    assert play(t).json()["sent"] is True                              # a new start
    assert len(events(t, "keep_awake_started")) == 2


def test_a_stop_nobody_noticed_is_said_when_the_next_beat_arrives(t):
    ready(t)
    assert play(t).json()["sent"] is True
    t.clock[0] += 120                                                  # the tick loop never ran
    assert beat(t).json()["sent"] is True
    ordered = sorted(t.store.events(100), key=lambda e: e["id"])
    kinds = [e["kind"] for e in ordered if e["kind"].startswith("keep_awake")]
    assert kinds == ["keep_awake_started", "keep_awake_stopped", "keep_awake_started"]


# -- the link --------------------------------------------------------------------------------------------------------
def _link(tmp_path):
    store = Store(str(tmp_path))
    rec, cl, marked = LinkRec(), LinkCloud(), []
    return RobotLink(rec, cl, store, mark_sent=marked.append), rec, cl, marked


def test_the_keep_awake_goes_through_the_cloud_and_is_marked_as_ours(tmp_path):
    lk, rec, cl, marked = _link(tmp_path)
    assert lk.command(KA, {"state": 1}) == "cloud"
    assert cl.sent == [KA] and marked == [KA]


def test_no_keep_awake_while_the_cloud_is_down(tmp_path):
    lk, rec, cl, _ = _link(tmp_path)
    cl.state, cl.connected = "signed out", False
    with pytest.raises(NoRoute):
        lk.command(KA, {"state": 1})
    assert cl.sent == []


@pytest.mark.parametrize("payload", [{"state": 0}, {}, {"state": 1, "x": 1}, {"state": True}, {"state": 1.0},
                                     {"state": "1"}])
def test_the_link_refuses_any_keep_awake_payload_but_state_1(tmp_path, payload):
    lk, rec, cl, _ = _link(tmp_path)
    with pytest.raises(NoRoute):
        lk.command(KA, payload)
    assert cl.sent == []


def test_a_beat_does_not_replace_the_links_last_command(tmp_path):
    lk, rec, cl, _ = _link(tmp_path)
    lk.command("pause", {})
    lk.command(KA, {"state": 1})
    assert lk.last_command["cmd"] == "pause"


# -- point 4: the panel's own beats are not the phone app, and the matching fails closed ---------------------------
def _recorder(tmp_path, trusted=True):
    r = Recorder.__new__(Recorder)
    r.store, r.session, r._sent, r.foreign_cmd_at, r.foreign_cmds = Store(str(tmp_path)), None, {}, 0.0, {}
    r._wifi_foreign_at, r._robot_at, r.loop = 0.0, 0.0, None
    if trusted:                                 # most tests are about the matching; see the trust tests below
        st = r._own()
        st["hist"].extend([True] * recorder.OWN_TRUST_WINDOW)
        st["trusted"] = True
        st["last"] = float("inf")               # mid-session: the next send is not a fresh start
    return r


def deliver(o, r, topic, data, ts):
    """A cloud message the way the app handles it: ops decides once and hands its verdict to the recorder's queue."""
    o._from_cloud(topic, data, ts)
    queued = r._queued.pop()
    r._handle(*queued)


def _ops(rec, cloud_state="connected"):
    rec._queued = []
    rec._from_thread = lambda topic, data, ts, ours=None: rec._queued.append((topic, data, ts, ours))
    o = ops_mod.Ops.__new__(ops_mod.Ops)
    o.rec, o.phone_app_at = rec, 0.0
    o.cloud = SimpleNamespace(state=cloud_state, connected_since=1.0)
    return o


def seen(r, name):
    return [e for e in r.store.events(50) if e["kind"] == "new_command_seen" and name in e["detail"]]


@pytest.mark.parametrize("name", [KA, "smart_vision_control"])
def test_the_panels_own_echo_is_ours_in_both_the_recorder_and_the_phone_app_check(tmp_path, name):
    r = _recorder(tmp_path)
    o = _ops(r)
    r.mark_sent(name)
    now = r._sent[name]
    deliver(o, r, f"snowbot/SN/app/{name}", {"state": 1}, now + 0.1)
    assert o.phone_app_at == 0.0 and seen(r, name) == []


@pytest.mark.parametrize("name", [KA, "smart_vision_control"])
def test_the_real_apps_beat_still_counts_while_the_panel_beats_every_four_seconds(tmp_path, monkeypatch, name):
    r = _recorder(tmp_path)
    o = _ops(r)
    clock = [5000.0]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])
    for _ in range(4):                                                        # the panel: a send and its echo every 4 s
        r.mark_sent(name)
        deliver(o, r, f"snowbot/SN/app/{name}", {"state": 1}, clock[0] + 0.1)
        clock[0] += 4
    assert o.phone_app_at == 0.0 and seen(r, name) == []
    app_at = clock[0] - 4 + 1.5                                                # the app's, 1.5 s after ours
    deliver(o, r, f"snowbot/SN/app/{name}", {"state": 1}, app_at)
    assert o.phone_app_at == app_at                                            # the interlock sees the real app
    assert len(seen(r, name)) == 1                                             # and the recorder logs another client


def test_one_send_accounts_for_one_echo_only(tmp_path):
    r = _recorder(tmp_path)
    r.mark_sent(KA)
    t = r._sent[KA]
    assert r.own_echo(KA, t + 0.1, {"state": 1}) is True       # the first of two with the same timestamp
    assert r.own_echo(KA, t + 0.1, {"state": 1}) is False      # equal timestamps, two messages: one credit
    assert r.own_echo(KA, t + 0.9, {"state": 1}) is False      # a later message is somebody else's


def test_the_phone_app_interlock_reads_the_real_apps_beat_not_the_panels(tmp_path):
    r = _recorder(tmp_path)
    r.app_stream_at = 0.0
    o = _ops(r)
    r.mark_sent(KA)
    now = r._sent[KA]
    o._from_cloud(f"snowbot/SN/app/{KA}", {"state": 1}, now + 0.1)
    assert o.phone_app_active(60) is False                                     # only the panel's own beat: app closed
    o._from_cloud(f"snowbot/SN/app/{KA}", {"state": 1}, now + 2.0)
    assert o.phone_app_active(60) is True                                      # a beat we did not send: the app is open


def test_other_commands_keep_the_old_five_second_window(tmp_path):
    r = _recorder(tmp_path)
    r.mark_sent("pause")
    t = r._sent["pause"]
    assert r.own_echo("pause", t + 1, {"state": 1}) is True and r.own_echo("pause", t + 1.5, {"state": 1}) is True
    assert r.own_echo("pause", t + 6, {"state": 1}) is False
    assert recorder.COUNTED_OWN == {KA, "smart_vision_control"}


def test_a_send_recorded_after_a_message_arrived_is_not_what_it_echoes(tmp_path, monkeypatch):
    r = _recorder(tmp_path)
    monkeypatch.setattr(recorder.time, "time", lambda: 1000.5)
    r.mark_sent(KA)
    assert r.own_echo(KA, 1000.0, {"state": 1}) is False  # somebody else's, from before
    assert r.own_echo(KA, 1000.6, {"state": 1}) is True   # the real echo still finds its send


def test_the_apps_camera_off_before_our_camera_on_echo_is_not_ours_and_leaves_our_send_pending(tmp_path, monkeypatch):
    r = _recorder(tmp_path)
    o = _ops(r)
    monkeypatch.setattr(recorder.time, "time", lambda: 1000.0)
    r.mark_sent("smart_vision_control")
    name = "smart_vision_control"
    deliver(o, r, f"snowbot/SN/app/{name}", {"state": 0}, 1000.1)
    assert o.phone_app_at == 1000.1 and len(seen(r, name)) == 1             # seen as the app's, not hidden
    assert r.own_echo(name, 1000.2, {"state": 1}) is True                    # our send was still waiting for its echo
    for odd in ({"state": 2}, {"state": True}, {"state": "1"}, {}):
        r.mark_sent(name)
        assert r.own_echo(name, 1000.1, odd) is False


def test_a_malformed_payload_takes_no_pending_send(tmp_path, monkeypatch):
    r = _recorder(tmp_path)
    monkeypatch.setattr(recorder.time, "time", lambda: 1000.0)
    r.mark_sent(KA)
    for junk in (None, "oops", [1], b"junk", 7):
        assert r.own_echo(KA, 1000.1, junk) is False
    assert r.own_echo(KA, 1000.2, {"state": 1}) is True       # the real echo still finds its send


def test_a_person_check_wants_a_session_cookie_not_a_bearer_token(t, monkeypatch):
    monkeypatch.delenv("PANEL_AUTH", raising=False)
    from fastapi import HTTPException
    bearer = SimpleNamespace(cookies={}, headers={"authorization": "Bearer gk_x"}, state=SimpleNamespace(who="token"),
                             app=main.app)
    with pytest.raises(HTTPException) as e:
        main._person(bearer)
    assert e.value.status_code == 403
    assert asyncio.run(t.controller.keep_awake(actor=main._actor(bearer), start=True))["sent"] is False


def test_the_two_commands_are_decided_once_per_message(tmp_path, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])
    r = _recorder(tmp_path)
    o = _ops(r)
    r.mark_sent(KA)
    deliver(o, r, f"snowbot/SN/app/{KA}", {"state": 1}, 5000.1)       # ours
    assert o.phone_app_at == 0.0
    deliver(o, r, f"snowbot/SN/app/{KA}", {"state": 1}, 5000.1)       # the app, the very same timestamp
    assert o.phone_app_at == 5000.1
    assert len([e for e in r.store.events(20) if e["kind"] == "new_command_seen"]) == 1


def test_a_local_message_without_a_verdict_is_decided_once_by_the_recorder(tmp_path, monkeypatch):
    r = _recorder(tmp_path)
    monkeypatch.setattr(recorder.time, "time", lambda: 1000.0)
    r.mark_sent("smart_vision_control")
    r._handle("snowbot/SN/app/smart_vision_control", {"state": 1}, 1000.1)         # no ops in front of it
    assert [e for e in r.store.events(20) if e["kind"] == "new_command_seen"] == []


# The trust rules (point 4 of the review): trusted only while EVERY one of the last OWN_TRUST_WINDOW credits was
# matched within OWN_TRUST_LATENCY_S; one miss withdraws it; the app's beats cannot stand in for echoes.
def _round(o, r, clock, echo_after=None, app_after=None, period=4.0):
    """One beat of the panel, with its echo `echo_after` s later (None = the cloud does not echo) and the Yarbo app's
    beat `app_after` s after ours (None = the app is closed). Returns whether the app's beat counted as the app's."""
    r.mark_sent(KA)
    if echo_after is not None:
        deliver(o, r, f"snowbot/SN/app/{KA}", {"state": 1}, clock[0] + echo_after)
    counted = None
    if app_after is not None:
        before = o.phone_app_at
        deliver(o, r, f"snowbot/SN/app/{KA}", {"state": 1}, clock[0] + app_after)
        counted = o.phone_app_at != before
    clock[0] += period
    return counted


@pytest.fixture
def clocked(tmp_path, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])
    return clock


def test_a_fresh_start_is_untrusted_so_every_beat_counts_as_the_apps(tmp_path, clocked):
    r = _recorder(tmp_path, trusted=False)
    o = _ops(r)
    r.app_stream_at = 0.0
    # The app lands as quickly as an echo, every time: until a full window of such matches, each counts as the app's.
    # (Held in step for a whole window it would pass for ours; the panel's jitter is what stops that, tested below.)
    counted = [_round(o, r, clocked, echo_after=None, app_after=0.1) for _ in range(recorder.OWN_TRUST_WINDOW - 1)]
    assert all(counted) and o.phone_app_active(60) is True


def test_trust_takes_a_full_window_of_quick_echoes_and_until_then_they_count_as_the_apps(tmp_path, clocked):
    r = _recorder(tmp_path, trusted=False)
    o = _ops(r)
    stamps = []
    for _ in range(recorder.OWN_TRUST_WINDOW):
        _round(o, r, clocked, echo_after=0.05)
        stamps.append(o.phone_app_at)
    assert r._own()["trusted"] is True
    assert stamps[-2] == clocked[0] - 8 + 0.05                       # the one before trust: counted as the app's
    last = o.phone_app_at
    _round(o, r, clocked, echo_after=0.05)
    assert o.phone_app_at == last                                    # trusted: ours, not the app's


def test_one_missing_echo_withdraws_trust_and_says_so_once(tmp_path, clocked):
    r = _recorder(tmp_path)
    o = _ops(r)
    _round(o, r, clocked, echo_after=None)                           # the cloud drops one echo
    r.mark_sent(KA)                                                  # the next send ages the dropped credit out
    assert r._own()["trusted"] is False
    clocked[0] += 4
    for _ in range(3):
        _round(o, r, clocked, echo_after=None)
    missing = [e for e in r.store.events(20) if e["kind"] == "cloud_echo_missing"]
    assert len(missing) == 1 and "count as the phone app" in missing[0]["detail"]


def test_trust_comes_back_after_a_full_window_and_says_so(tmp_path, clocked):
    r = _recorder(tmp_path)
    o = _ops(r)
    _round(o, r, clocked, echo_after=None)
    for _ in range(recorder.OWN_TRUST_WINDOW + 1):
        _round(o, r, clocked, echo_after=0.05)
    assert r._own()["trusted"] is True
    assert len([e for e in r.store.events(20) if e["kind"] == "cloud_echo_seen"]) == 1


def test_an_app_beat_slower_than_an_echo_never_matches(tmp_path, clocked):
    r = _recorder(tmp_path, trusted=False)
    o = _ops(r)
    for _ in range(recorder.OWN_TRUST_WINDOW * 2):
        _round(o, r, clocked, app_after=recorder.OWN_TRUST_LATENCY_S + 0.05)
    assert r._own()["trusted"] is False


def test_the_review_case_echoes_stop_and_the_apps_beats_land_just_after_ours(tmp_path, clocked):
    """Jeff's case: trust earned, then the cloud stops echoing and the app's 4 s beats land within the old 0.5 s of the
    panel's. With the panel's beats jittered they cannot stay there: trust goes and the app is seen."""
    rng = random.Random(1)
    r = _recorder(tmp_path)
    o = _ops(r)
    r.app_stream_at = 0.0
    app_next = clocked[0] + 0.1                                     # the app starts 0.1 s after ours, every 4.0 s
    for _ in range(40):
        jitter = rng.uniform(0, control.KEEP_AWAKE_JITTER_S)        # the panel's beat: its 4 s tick plus the jitter
        t0 = clocked[0]
        clocked[0] = t0 + jitter
        r.mark_sent(KA)
        while app_next < clocked[0] + 3.9:
            if app_next >= t0:
                deliver(o, r, f"snowbot/SN/app/{KA}", {"state": 1}, app_next)
            app_next += 4.0
        clocked[0] = t0 + 4.0
    assert r._own()["trusted"] is False
    assert o.phone_app_active(60) is True


@pytest.mark.parametrize("seed", range(50))
def test_without_echoes_the_app_cannot_hold_trust_for_long_whatever_its_phase(tmp_path, clocked, seed):
    rng = random.Random(seed)
    r = _recorder(tmp_path)
    o = _ops(r)
    phase = rng.uniform(0, 4.0)
    app_next = clocked[0] + phase
    lost_at = None
    for i in range(30):
        t0 = clocked[0]
        clocked[0] = t0 + rng.uniform(0, control.KEEP_AWAKE_JITTER_S)
        r.mark_sent(KA)
        while app_next < t0 + 4.0:
            if app_next >= clocked[0] - 1.0:
                deliver(o, r, f"snowbot/SN/app/{KA}", {"state": 1}, app_next)
            app_next += 4.0
        clocked[0] = t0 + 4.0
        if lost_at is None and not r._own()["trusted"]:
            lost_at = i
    assert lost_at is not None and lost_at <= 12


def test_the_stats_record_what_was_seen(tmp_path, clocked):
    r = _recorder(tmp_path, trusted=False)
    o = _ops(r)
    for _ in range(3):
        _round(o, r, clocked, echo_after=0.08)
    s = r.own_echo_stats()
    assert s["latency_ms"]["n"] == 3 and s["latency_ms"]["median"] == 80 and s["recent"] == [True, True, True]


def test_the_matching_constants_err_towards_the_app():
    assert recorder.OWN_TRUST_LATENCY_S <= 0.3 and recorder.OWN_TRUST_WINDOW >= 10
    assert recorder.OWN_ECHO_COUNTED_S < recorder.OWN_ECHO_S


# -- the independent review's findings ------------------------------------------------------------------------------
def test_a_beat_reads_the_light_context_and_never_waits_on_frigate_or_home_assistant(t):
    ready(t)

    async def slow_context(args):
        raise AssertionError("a beat must not build the full context (Frigate, Home Assistant)")

    t.controller._context = slow_context
    t.controller.light_context = lambda: {"snapshot_age_s": 3, "route_state": "connected"}
    assert play(t).json()["sent"] is True
    t.controller.light_context = lambda: {"snapshot_age_s": 600, "route_state": "connected"}
    t.clock[0] += 4
    assert beat(t).json()["ended"] is True                             # its checks still see the robot go offline


def test_the_ops_beat_context_has_what_the_checks_read_and_no_awaits():
    import inspect
    assert not inspect.iscoroutinefunction(ops_mod.Ops.beat_context)
    o = ops_mod.Ops.__new__(ops_mod.Ops)
    o.rec = SimpleNamespace(snapshot_at=None)
    o.link = SimpleNamespace(state="connected")
    assert o.beat_context() == {"snapshot_age_s": None, "route_state": "connected"}


def test_the_events_log_leaves_out_the_beats_that_went_but_keeps_refusals(t):
    ready(t)
    assert play(t).json()["sent"] is True
    for _ in range(5):
        t.clock[0] += 4
        beat(t)
    t.ctx.update(snapshot_age_s=600)
    t.clock[0] += 4
    beat(t)                                                            # refused: robot offline
    asyncio.run(t.controller.send("pause", {}, actor="user", wait=False))
    shown = t.c.get("/api/control/log?limit=300").json()["log"]
    assert [r["cmd"] for r in shown].count(KA) == 1 and any(r["cmd"] == "pause" for r in shown)
    assert len([r for r in t.c.get("/api/control/log?beats=true").json()["log"] if r["cmd"] == KA]) == 7


def test_the_window_ends_when_the_beats_stop(t):
    ready(t)
    assert play(t).json()["sent"] is True
    t.clock[0] += control.KEEP_AWAKE_GAP_S + 1
    t.controller.keep_awake_tick()
    assert t.controller._awake_since is None
    t.clock[0] += 1
    assert beat(t).json()["sent"] is False                             # a tab that comes back needs a press


def test_a_failed_route_ends_the_window(t):
    ready(t)
    assert play(t).json()["sent"] is True

    class NoCloud(FakeLink):
        def command(self, cmd, payload, route=None):
            raise NoRoute("the Yarbo cloud is signed out")

    t.controller.link = NoCloud(t.cloud)
    t.clock[0] += 4
    r = beat(t).json()
    assert r["sent"] is False and r["ended"] is True


def test_a_slow_echo_is_kept_for_measuring_and_the_stats_say_so(tmp_path, clocked):
    r = _recorder(tmp_path, trusted=False)
    o = _ops(r)
    _round(o, r, clocked, echo_after=0.6)
    s = r.own_echo_stats()
    assert s["latency_ms"] is None and s["slow_ms"]["median"] == 600


def test_trust_is_earned_again_after_a_gap_in_the_panels_sends(tmp_path, clocked):
    r = _recorder(tmp_path)
    o = _ops(r)
    r._own()["last"] = clocked[0]
    _round(o, r, clocked, echo_after=0.05)
    assert r._own()["trusted"] is True
    clocked[0] += recorder.OWN_FRESH_S + 1                             # the tile was closed for a while
    r.mark_sent(KA)
    assert r._own()["trusted"] is False and r._own()["hist"] == __import__("collections").deque()
    assert [e for e in r.store.events(20) if e["kind"] == "cloud_echo_missing"] == []   # not a cloud failure
