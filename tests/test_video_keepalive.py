"""The Cameras tile's keep-awake (§13): POST /api/video/keepalive, Controller.keep_awake, the link's one door for it,
and telling the panel's own beats from the Yarbo app's. The robot streams its cameras only while someone sends
`set_working_state {"state":1}`; the panel sends exactly one per accepted request. Driven through the real app's
middleware with fakes: no robot, no cloud, no network."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, control, link as link_mod, main, mcp_server, ops as ops_mod, recorder
from app.link import NoRoute, RobotLink
from app.recorder import Recorder
from app.transport_local import ReadOnlyViolation
from app.store import Store
from tests.fakelink import FakeLink
from tests.test_link import FakeCloud as LinkCloud, FakeRec as LinkRec
from tests.test_phase2 import FakeCloud, good_ctx


class FakeRec:
    def __init__(self, store):
        self.store = store


@pytest.fixture
def t(tmp_path, monkeypatch):
    monkeypatch.delenv("PANEL_AUTH", raising=False)
    store = Store(str(tmp_path))
    gate = auth.Auth(store)
    gate.announce()
    cloud = FakeCloud()
    clock = [10_000.0]
    monkeypatch.setattr(control.time, "monotonic", lambda: clock[0])

    async def context(args):
        return good_ctx()

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
    yield SimpleNamespace(c=c, store=store, cloud=cloud, controller=controller, clock=clock)
    for k, v in old.items():
        setattr(main.app.state, k, v)


def beat(t, client=None, body=None):
    return (client or t.c).post("/api/video/keepalive", json=body if body is not None else {})


def ready(t):
    assert t.c.put("/api/settings/robot_video_enabled", json=True).status_code == 200
    t.store.set_setting("control_enabled", True)


def events(t, kind):
    return [e for e in t.store.events(100) if e["kind"] == kind]


# -- the route's guards ------------------------------------------------------------------------------------------
def test_with_robot_video_off_it_is_refused_and_nothing_is_sent(t):
    t.store.set_setting("control_enabled", True)
    r = beat(t)
    assert r.status_code == 403 and "off" in r.json()["detail"]
    assert t.cloud.sent == []


def test_with_control_off_it_says_so_and_sends_nothing(t):
    assert t.c.put("/api/settings/robot_video_enabled", json=True).status_code == 200
    r = beat(t)
    assert r.status_code == 200 and r.json()["sent"] is False and r.json()["control_off"] is True
    assert t.cloud.sent == [] and events(t, "keep_awake_started") == []


def test_a_person_with_video_and_control_on_sends_one_fixed_beat(t):
    ready(t)
    r = beat(t)
    assert r.status_code == 200 and r.json() == {"sent": True}
    assert r.headers["cache-control"] == "no-store"
    assert t.cloud.sent == [("set_working_state", {"state": 1})]


def test_nothing_in_the_request_reaches_the_robot_and_state_zero_never_goes(t):
    ready(t)
    bodies = [{"state": 0}, {"cmd": "cmd_vel", "vel": 1}, {"payload": {"state": 0}}, [0], "state 0", {"state": False}]
    for body in bodies:
        t.clock[0] += 5
        assert beat(t, body=body).status_code == 200
    assert len(t.cloud.sent) == len(bodies)
    assert all(c == "set_working_state" and p == {"state": 1} for c, p in t.cloud.sent)


def test_it_is_rate_limited_for_everyone_together(t):
    ready(t)
    assert beat(t).json()["sent"] is True
    t.clock[0] += control.KEEP_AWAKE_MIN_S - 0.5
    again = beat(t)
    assert again.status_code == 200 and again.json() == {"sent": False, "limited": True}
    t.clock[0] += 1.0
    assert beat(t).json()["sent"] is True
    assert len(t.cloud.sent) == 2


def test_a_refused_beat_does_not_start_the_rate_limit_clock(t):
    t.c.put("/api/settings/robot_video_enabled", json=True)
    assert beat(t).json()["control_off"] is True                    # control off: not sent
    t.store.set_setting("control_enabled", True)
    assert beat(t).json()["sent"] is True                           # so the next one is not "too soon"


def test_an_api_token_is_refused_so_is_a_signed_out_caller(t):
    ready(t)
    made = t.c.post("/api/auth/tokens", json={"name": "script"}).json()["token"]
    bot = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {made}"})
    assert beat(t, client=bot).status_code == 403
    assert ("POST", "/api/video/keepalive") not in auth.TOKEN_WRITES
    assert TestClient(main.app, base_url="http://panel.test").post("/api/video/keepalive", json={}).status_code == 401
    assert t.cloud.sent == []
    assert beat(t).json()["sent"] is True                           # the person still can


def test_an_ai_assistant_cannot_reach_it(t):
    ready(t)
    assert not mcp_server.allowed("POST", "/api/video/keepalive")
    inner = TestClient(main.app, base_url="http://panel", headers={auth.INTERNAL_HEADER: auth.INTERNAL})
    assert beat(t, client=inner).status_code == 403                 # no person's session
    assert t.cloud.sent == []


@pytest.mark.parametrize("actor", ["token", "mcp", "scheduler", "guard", "", "robot"])
def test_the_controller_beats_only_for_a_person(t, actor):
    ready(t)
    r = asyncio.run(t.controller.keep_awake(actor=actor))
    assert r["sent"] is False and "people" in r["refused"] and t.cloud.sent == []


def test_a_missing_route_says_so_and_sends_nothing(t):
    ready(t)

    class NoCloud(FakeLink):
        def keep_awake_beat(self, payload):
            raise NoRoute("the Yarbo cloud is signed out")

    t.controller.link = NoCloud(t.cloud)
    r = beat(t)
    assert r.status_code == 200 and r.json()["sent"] is False and "signed out" in r.json()["error"]
    assert t.cloud.sent == []


# -- it is not a command anyone else can send -----------------------------------------------------------------------
def test_the_general_command_route_cannot_send_it_for_anyone(t):
    ready(t)
    assert control.KEEP_AWAKE not in control.CATALOGUE
    for args in ({}, {"state": 1}, {"state": 0}):
        r = t.c.post("/api/control/send", json={"cmd": "set_working_state", "args": args, "confirm": True})
        assert r.status_code == 200 and r.json()["sent"] is False
        assert "not in the control catalogue" in r.json()["refused"]
    made = t.c.post("/api/auth/tokens", json={"name": "script"}).json()["token"]
    bot = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {made}"})
    r = bot.post("/api/control/send", json={"cmd": "set_working_state", "args": {}, "confirm": True})
    assert r.json()["sent"] is False
    assert t.c.post("/api/control/preview", json={"cmd": "set_working_state"}).status_code == 400
    assert t.cloud.sent == []


def test_it_is_run_neutral_like_the_camera_switch(t):
    ready(t)
    assert control.KEEP_AWAKE in control.RUN_NEUTRAL
    seq, human = t.controller._seq, t.controller.last_human_cmd_at
    assert beat(t).json()["sent"] is True
    assert t.controller._seq == seq and t.controller.last_human_cmd_at == human and t.controller.last is None


# -- audit: one event at the start, one at the stop, none per beat -----------------------------------------------------
def test_one_event_when_the_beats_start_and_one_when_they_stop_none_per_beat(t):
    ready(t)
    for _ in range(6):
        assert beat(t).json()["sent"] is True
        t.clock[0] += 4
    assert len(events(t, "keep_awake_started")) == 1 and events(t, "keep_awake_stopped") == []
    t.clock[0] += control.KEEP_AWAKE_GAP_S - 4                      # not long enough yet
    t.controller.keep_awake_tick()
    assert events(t, "keep_awake_stopped") == []
    t.clock[0] += 2
    t.controller.keep_awake_tick()
    t.controller.keep_awake_tick()                                   # said once
    assert len(events(t, "keep_awake_stopped")) == 1
    assert beat(t).json()["sent"] is True                            # a new start
    assert len(events(t, "keep_awake_started")) == 2
    assert not [e for e in t.store.events(100) if e["kind"].startswith("keep_awake") and "state" in e["detail"]]


def test_a_stop_nobody_noticed_is_said_when_the_next_beat_arrives(t):
    ready(t)
    assert beat(t).json()["sent"] is True
    t.clock[0] += 120                                                # the tick loop never ran
    assert beat(t).json()["sent"] is True
    rows = sorted(t.store.events(100), key=lambda e: e["id"])
    kinds = [e["kind"] for e in rows if e["kind"].startswith("keep_awake")]
    assert kinds == ["keep_awake_started", "keep_awake_stopped", "keep_awake_started"]


def test_a_failed_send_is_not_a_start(t):
    ready(t)

    class Broken(FakeLink):
        def keep_awake_beat(self, payload):
            raise NoRoute("no route")

    t.controller.link = Broken(t.cloud)
    beat(t)
    assert events(t, "keep_awake_started") == []


# -- the link's one door -----------------------------------------------------------------------------------------------
def _link(tmp_path, mode=None):
    store = Store(str(tmp_path))
    if mode:
        store.set_setting("robot_route", mode)
    rec, cl, marked = LinkRec(), LinkCloud(), []
    return RobotLink(rec, cl, store, mark_sent=marked.append), rec, cl, marked


def test_the_keep_awake_goes_through_the_cloud_only_and_is_marked_as_ours(tmp_path):
    lk, rec, cl, marked = _link(tmp_path)
    assert lk.keep_awake_beat({"state": 1}) == "cloud"
    assert cl.sent == ["set_working_state"] and rec.transport.commands == [] and marked == ["set_working_state"]


def test_the_keep_awake_waits_for_a_proved_local_route(tmp_path, monkeypatch):
    lk, rec, cl, _ = _link(tmp_path, mode="local")
    with pytest.raises(NoRoute):
        lk.keep_awake_beat({"state": 1})
    lk.store.set_setting("robot_route", "auto")
    cl.state, cl.connected = "signed out", False
    with pytest.raises(NoRoute):
        lk.keep_awake_beat({"state": 1})
    assert cl.sent == [] and rec.transport.commands == []
    monkeypatch.setattr(link_mod, "LOCAL_KEEPAWAKE_PROVED", True)
    assert lk.keep_awake_beat({"state": 1}) == "local" and rec.transport.commands == ["set_working_state"]


@pytest.mark.parametrize("payload", [{"state": 0}, {}, {"state": 1, "x": 1}, {"state": True}, {"state": 1.0},
                                     {"state": "1"}])
def test_the_link_refuses_any_keep_awake_payload_but_state_1(tmp_path, payload):
    lk, rec, cl, _ = _link(tmp_path)
    with pytest.raises(NoRoute):
        lk.keep_awake_beat(payload)
    assert cl.sent == [] and rec.transport.commands == []


def test_the_run_command_door_still_refuses_it(tmp_path):
    lk, rec, cl, _ = _link(tmp_path)
    with pytest.raises((NoRoute, ReadOnlyViolation)):
        lk.command("set_working_state", {"state": 1})
    assert cl.sent == []


def test_only_the_controller_uses_the_keep_awake_door():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app"
    users = {p.name for p in root.rglob("*.py")
             if any(isinstance(n, ast.Attribute) and n.attr == "keep_awake_beat"
                    for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))))}
    assert users == {"control.py"}                       # link.py defines the door; only Controller.keep_awake knocks


# -- the panel's own beats are not the phone app --------------------------------------------------------------------
def _recorder(tmp_path, trusted=True):
    r = Recorder.__new__(Recorder)
    r.store, r.session, r._sent, r.foreign_cmd_at, r.foreign_cmds = Store(str(tmp_path)), None, {}, 0.0, {}
    r._wifi_foreign_at, r._robot_at, r.loop = 0.0, 0.0, None
    r._own()["trusted"] = trusted               # most tests are about the matching; see the self-check tests
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


@pytest.mark.parametrize("name", ["set_working_state", "smart_vision_control"])
def test_the_panels_own_echo_is_ours_in_both_the_recorder_and_the_phone_app_check(tmp_path, name):
    r = _recorder(tmp_path)
    o = _ops(r)
    r.mark_sent(name)
    now = r._sent[name]
    deliver(o, r, f"snowbot/SN/app/{name}", {"state": 1}, now + 0.2)
    assert o.phone_app_at == 0.0 and seen(r, name) == []


@pytest.mark.parametrize("name", ["set_working_state", "smart_vision_control"])
def test_the_real_apps_beat_still_counts_while_the_panel_beats_every_four_seconds(tmp_path, monkeypatch, name):
    r = _recorder(tmp_path)
    o = _ops(r)
    clock = [5000.0]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])
    for _ in range(4):                                                        # the panel: a send and its echo every 4 s
        r.mark_sent(name)
        deliver(o, r, f"snowbot/SN/app/{name}", {"state": 1}, clock[0] + 0.3)
        clock[0] += 4
    assert o.phone_app_at == 0.0 and seen(r, name) == []
    app_at = clock[0] - 4 + 1.5                                                # the app's, 1.5 s after ours
    deliver(o, r, f"snowbot/SN/app/{name}", {"state": 1}, app_at)
    assert o.phone_app_at == app_at                                            # the interlock sees the real app
    assert len(seen(r, name)) == 1                                             # and the recorder logs another client


def test_one_send_accounts_for_one_echo_only(tmp_path):
    r = _recorder(tmp_path)
    r.mark_sent("set_working_state")
    t = r._sent["set_working_state"]
    assert r.own_echo("set_working_state", t + 0.1, {"state": 1}) is True   # the first of two with the same timestamp
    assert r.own_echo("set_working_state", t + 0.1, {"state": 1}) is False  # equal timestamps, two messages: one credit
    assert r.own_echo("set_working_state", t + 0.9, {"state": 1}) is False  # a later message is somebody else's
    r.mark_sent("set_working_state")
    t2 = r._sent["set_working_state"]
    assert r.own_echo("set_working_state", t2 + 7, {"state": 1}) is False  # an echo that never came ages out


def test_the_phone_app_interlock_reads_the_real_apps_beat_not_the_panels(tmp_path):
    r = _recorder(tmp_path)
    r.app_stream_at = 0.0
    o = _ops(r)
    o.cloud.connected_since = 1.0
    r.mark_sent("set_working_state")
    now = r._sent["set_working_state"]
    o._from_cloud("snowbot/SN/app/set_working_state", {"state": 1}, now + 0.1)
    assert o.phone_app_active(60) is False                                     # only the panel's own beat: app closed
    o._from_cloud("snowbot/SN/app/set_working_state", {"state": 1}, now + 2.0)
    assert o.phone_app_active(60) is True                                      # a beat we did not send: the app is open


def test_other_commands_keep_the_old_five_second_window(tmp_path):
    r = _recorder(tmp_path)
    r.mark_sent("pause")
    t = r._sent["pause"]
    assert r.own_echo("pause", t + 1, {"state": 1}) is True and r.own_echo("pause", t + 1.5, {"state": 1}) is True
    assert r.own_echo("pause", t + 6, {"state": 1}) is False
    assert recorder.COUNTED_OWN == {"set_working_state", "smart_vision_control"}


# -- review round 1: timing under the lock, the setting rechecked, marks before the send -------------------------
def _controller(tmp_path, monkeypatch, clock):
    from tests.test_phase2 import make_controller
    store, cloud, c = make_controller(tmp_path, enabled=True)
    store.set_setting("robot_video_enabled", True)
    monkeypatch.setattr(control.time, "monotonic", lambda: clock[0])
    return store, cloud, c


def test_a_beat_that_queued_behind_a_send_is_timed_when_it_goes_not_when_it_asked(tmp_path, monkeypatch):
    clock = [100.0]
    store, cloud, c = _controller(tmp_path, monkeypatch, clock)

    async def go():
        await c._lock.acquire()                          # a send is holding the controller
        first = asyncio.create_task(c.keep_awake())
        await asyncio.sleep(0)
        clock[0] = 103.0
        second = asyncio.create_task(c.keep_awake())
        await asyncio.sleep(0)
        clock[0] = 105.0
        c._lock.release()
        return await first, await second

    first, second = asyncio.run(go())
    assert first["sent"] is True and second == {"sent": False, "limited": True}     # not two back to back
    assert len(cloud.sent) == 1


def test_a_beat_that_queued_while_video_was_switched_off_is_not_sent(tmp_path, monkeypatch):
    store, cloud, c = _controller(tmp_path, monkeypatch, [100.0])

    async def go():
        await c._lock.acquire()
        task = asyncio.create_task(c.keep_awake())
        await asyncio.sleep(0)
        store.set_setting("robot_video_enabled", False)
        c._lock.release()
        return await task

    r = asyncio.run(go())
    assert r["sent"] is False and "video is off" in r["refused"] and cloud.sent == []


def test_the_mark_is_made_before_the_send_so_an_echo_during_publish_is_ours(tmp_path):
    r = _recorder(tmp_path)
    rec, store, answers = LinkRec(), Store(str(tmp_path)), []

    class EchoingCloud(LinkCloud):
        def publish(self, cmd, payload):                 # the broker's thread sees the echo before publish() returns
            answers.append(r.own_echo(cmd, __import__("time").time(), payload))
            super().publish(cmd, payload)

    lk = RobotLink(rec, EchoingCloud(), store, mark_sent=r.mark_sent)
    lk.keep_awake_beat({"state": 1})
    assert answers == [True]
    assert r.own_echo("set_working_state", __import__("time").time() + 0.5, {"state": 1}) is False  # used once


def test_a_send_that_fails_takes_its_mark_back(tmp_path):
    r = _recorder(tmp_path)
    r._sent["set_working_state"] = 1.0

    class Failing(LinkCloud):
        def publish(self, cmd, payload):
            raise ConnectionError("down")

    lk = RobotLink(LinkRec(), Failing(), Store(str(tmp_path)), mark_sent=r.mark_sent)
    with pytest.raises(ConnectionError):
        lk.keep_awake_beat({"state": 1})
    assert r._sent["set_working_state"] == 1.0 and not r._own()["beats"]["set_working_state"]
    assert r.own_echo("set_working_state", __import__("time").time(), {"state": 1}) is False


def test_a_send_recorded_after_a_message_arrived_is_not_what_it_echoes(tmp_path, monkeypatch):
    r = _recorder(tmp_path)
    monkeypatch.setattr(recorder.time, "time", lambda: 1000.5)
    r.mark_sent("set_working_state")
    assert r.own_echo("set_working_state", 1000.0, {"state": 1}) is False  # somebody else's, from before
    assert r.own_echo("set_working_state", 1000.7, {"state": 1}) is True        # the real echo still finds its send


# -- review round 2: only what we send can be our echo ------------------------------------------------------------
def test_the_apps_camera_off_before_our_camera_on_echo_is_not_ours_and_leaves_our_send_pending(tmp_path, monkeypatch):
    r = _recorder(tmp_path)
    o = _ops(r)
    monkeypatch.setattr(recorder.time, "time", lambda: 1000.0)
    r.mark_sent("smart_vision_control")
    name = "smart_vision_control"
    deliver(o, r, f"snowbot/SN/app/{name}", {"state": 0}, 1000.2)
    assert o.phone_app_at == 1000.2 and len(seen(r, name)) == 1             # seen as the app's, not hidden
    assert r.own_echo(name, 1000.4, {"state": 1}) is True                    # our send was still waiting for its echo
    for odd in ({"state": 2}, {"state": True}, {"state": "1"}, {}):
        r.mark_sent(name)
        assert r.own_echo(name, 1000.6 + len(str(odd)), odd) is False


def test_a_malformed_payload_takes_no_pending_send(tmp_path, monkeypatch):
    r = _recorder(tmp_path)
    monkeypatch.setattr(recorder.time, "time", lambda: 1000.0)
    r.mark_sent("set_working_state")
    for junk in (None, "oops", [1], b"junk", 7):
        assert r.own_echo("set_working_state", 1000.1, junk) is False
    assert r.own_echo("set_working_state", 1000.3, {"state": 1}) is True       # the real echo still finds its send


# -- metrics-only tokens (#43) are not people ------------------------------------------------------------------------------
def test_a_metrics_only_token_is_refused_on_both_video_routes_and_never_reaches_a_handler(t):
    ready(t)
    made = t.c.post("/api/auth/tokens", json={"name": "scrape", "scope": "metrics"}).json()["token"]
    scraper = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {made}"})
    for path, body in (("/api/video/keepalive", {}), ("/api/video/token", {"camera": "front"})):
        r = scraper.post(path, json=body)
        assert r.status_code == 403 and "only read /api/metrics" in r.json()["detail"]
    assert scraper.post("/api/control/send", json={"cmd": "pause", "confirm": True}).status_code == 403
    assert t.cloud.sent == []
    assert beat(t).json()["sent"] is True                          # the person still can


@pytest.mark.parametrize("who, actor", [("metrics", "token"), ("token", "token"), ("session", "user")])
def test_a_metrics_gate_result_is_never_a_person(who, actor):
    request = SimpleNamespace(state=SimpleNamespace(who=who))
    assert main._actor(request) == actor                           # the keep-awake refuses any actor but "user"


def test_a_person_check_wants_a_session_cookie_not_a_bearer_token(t, monkeypatch):
    monkeypatch.delenv("PANEL_AUTH", raising=False)
    from fastapi import HTTPException
    bearer = SimpleNamespace(cookies={}, headers={"authorization": "Bearer gk_x"}, state=SimpleNamespace(who="metrics"),
                             app=main.app)
    with pytest.raises(HTTPException) as e:
        main._person(bearer)
    assert e.value.status_code == 403
    assert asyncio.run(t.controller.keep_awake(actor=main._actor(bearer)))["sent"] is False


# -- review: the cloud may not echo our own publishes: the matching must fail towards "the app" ------------------------
def _beat_both(o, r, clock, app_offset, rounds, echo):
    """The panel and the Yarbo app each beat every 4 s. `echo`: the cloud sends the panel's own beat back (at once)."""
    app_seen = []
    for _ in range(rounds):
        r.mark_sent("set_working_state")
        if echo:
            deliver(o, r, "snowbot/SN/app/set_working_state", {"state": 1}, clock[0] + 0.05)
        before = o.phone_app_at
        deliver(o, r, "snowbot/SN/app/set_working_state", {"state": 1}, clock[0] + app_offset)     # the real app's beat
        app_seen.append(o.phone_app_at != before)
        clock[0] += 4
    return app_seen


def test_without_an_echo_the_apps_beats_are_seen_and_the_interlock_blocks(tmp_path, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])
    r = _recorder(tmp_path, trusted=False)                 # a fresh start: not trusted until echoes have been seen
    o = _ops(r)
    r.app_stream_at = 0.0
    seen_ = _beat_both(o, r, clock, 1.0, 15, echo=False)   # the app lands 1 s after ours, inside the 1.5 s window
    assert all(seen_)                                       # every one of the app's beats counted as the app's
    assert o.phone_app_active(60) is True                   # so automatic starts stay blocked


def test_the_matching_gives_up_after_three_credits_expire_and_says_so_once(tmp_path, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])
    r = _recorder(tmp_path, trusted=True)                  # it had been trusted ...
    o = _ops(r)
    for _ in range(recorder.OWN_UNMATCHED_MAX + 1):        # ... then the echoes stop coming
        r.mark_sent("set_working_state")
        clock[0] += 4
    r.mark_sent("set_working_state")
    assert r._own()["trusted"] is False
    deliver(o, r, "snowbot/SN/app/set_working_state", {"state": 1}, clock[0] + 0.3)
    assert o.phone_app_at == clock[0] + 0.3                 # even a message right after our send counts as the app's
    notes = [e for e in r.store.events(20) if e["kind"] == "cloud_echo_missing"]
    r.mark_sent("set_working_state")
    clock[0] += 4
    r.mark_sent("set_working_state")
    assert len([e for e in r.store.events(20) if e["kind"] == "cloud_echo_missing"]) == len(notes) == 1
    assert "count as the phone app" in notes[0]["detail"]


def test_an_echo_that_comes_back_restores_the_matching(tmp_path, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])
    r = _recorder(tmp_path, trusted=False)
    o = _ops(r)
    stamps = []
    for _ in range(recorder.OWN_TRUST_AFTER):               # quick echoes, one per send
        r.mark_sent("set_working_state")
        deliver(o, r, "snowbot/SN/app/set_working_state", {"state": 1}, clock[0] + 0.05)
        stamps.append((clock[0] + 0.05, o.phone_app_at))
        clock[0] += 4
    assert r._own()["trusted"] is True
    assert all(at == seen_at for at, seen_at in stamps[:-1])        # until trusted each counted as possibly the app's
    assert stamps[-1][1] == stamps[-2][0]                            # the one that earned the trust was ours
    r.mark_sent("set_working_state")
    last = o.phone_app_at
    deliver(o, r, "snowbot/SN/app/set_working_state", {"state": 1}, clock[0] + 0.05)
    assert o.phone_app_at == last                                    # trusted: ours, not the app's


def test_an_app_beat_that_lands_in_the_window_does_not_earn_trust(tmp_path, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])
    r = _recorder(tmp_path, trusted=False)
    o = _ops(r)
    for _ in range(6):
        r.mark_sent("set_working_state")
        deliver(o, r, "snowbot/SN/app/set_working_state", {"state": 1}, clock[0] + 1.0)   # slower than an echo
        clock[0] += 4
    assert r._own()["trusted"] is False and o.phone_app_at == clock[0] - 4 + 1.0


def test_the_window_for_the_two_commands_is_short_and_named():
    assert recorder.OWN_ECHO_COUNTED_S == 1.5 and recorder.OWN_ECHO_COUNTED_S < recorder.OWN_ECHO_S
    assert recorder.OWN_UNMATCHED_MAX == 3


def test_two_messages_with_the_same_timestamp_are_each_decided_on_their_own(tmp_path, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])
    r = _recorder(tmp_path)
    o = _ops(r)
    r.mark_sent("set_working_state")
    deliver(o, r, "snowbot/SN/app/set_working_state", {"state": 1}, 5000.1)       # ours
    assert o.phone_app_at == 0.0
    deliver(o, r, "snowbot/SN/app/set_working_state", {"state": 1}, 5000.1)       # the app, the very same timestamp
    assert o.phone_app_at == 5000.1
    assert len([e for e in r.store.events(20) if e["kind"] == "new_command_seen"]) == 1


def test_a_local_message_without_a_verdict_is_decided_once_by_the_recorder(tmp_path, monkeypatch):
    r = _recorder(tmp_path)
    monkeypatch.setattr(recorder.time, "time", lambda: 1000.0)
    r.mark_sent("smart_vision_control")
    r._handle("snowbot/SN/app/smart_vision_control", {"state": 1}, 1000.1)         # no ops in front of it
    assert [e for e in r.store.events(20) if e["kind"] == "new_command_seen"] == []


def test_a_publish_the_cloud_refuses_raises_so_the_mark_is_undone(tmp_path):
    import threading
    from app.cloud import CloudTransport
    r = _recorder(tmp_path)
    c = CloudTransport.__new__(CloudTransport)
    c._lock, c.sn, c.published, c.state, c.connected = threading.Lock(), "SN", 0, "connected", True

    class Client:
        def __init__(self, rc):
            self.rc = rc

        def publish(self, topic, payload):
            return SimpleNamespace(rc=self.rc)

    c._mqtt = Client(4)                                     # MQTT_ERR_NO_CONN
    lk = RobotLink(LinkRec(), c, Store(str(tmp_path)), mark_sent=r.mark_sent)
    with pytest.raises(ConnectionError):
        lk.keep_awake_beat({"state": 1})
    assert c.published == 0 and not r._own()["beats"]["set_working_state"] and "set_working_state" not in r._sent
    c._mqtt = Client(0)
    assert lk.keep_awake_beat({"state": 1}) == "cloud" and c.published == 1


def test_a_beat_does_not_replace_the_links_last_command(tmp_path):
    lk, rec, cl, _ = _link(tmp_path)
    lk.command("pause", {})
    lk.keep_awake_beat({"state": 1})
    assert lk.last_command["cmd"] == "pause"


def test_the_local_broker_takes_the_keep_awake_only_with_state_1():
    from app.transport_local import check_command
    check_command("set_working_state", {"state": 1})
    for bad in (None, {}, {"state": 0}, {"state": True}, {"state": 1, "x": 2}):
        with pytest.raises(ReadOnlyViolation):
            check_command("set_working_state", bad)


@pytest.mark.parametrize("who, actor", [("session", "user"), ("open", "user"), ("internal", "token"),
                                        ("signed", "token"), ("token", "token"), ("metrics", "token"),
                                        (None, "token")])
def test_only_a_session_or_an_open_panel_is_a_person(who, actor):
    assert main._actor(SimpleNamespace(state=SimpleNamespace(who=who))) == actor


def test_the_rate_limit_ignores_the_wall_clock(tmp_path, monkeypatch):
    store, cloud, c = _controller(tmp_path, monkeypatch, [100.0])     # monotonic stands still ...
    monkeypatch.setattr(control.time, "time", lambda: 1.0)           # ... while the wall clock jumps about
    assert asyncio.run(c.keep_awake())["sent"] is True
    monkeypatch.setattr(control.time, "time", lambda: 10 ** 9)
    assert asyncio.run(c.keep_awake()) == {"sent": False, "limited": True}
