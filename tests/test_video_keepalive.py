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
    monkeypatch.setattr(control.time, "time", lambda: clock[0])

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
    with pytest.raises(NoRoute):
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
def _recorder(tmp_path):
    r = Recorder.__new__(Recorder)
    r.store, r.session, r._sent, r.foreign_cmd_at, r.foreign_cmds = Store(str(tmp_path)), None, {}, 0.0, {}
    r._wifi_foreign_at, r._robot_at, r.loop = 0.0, 0.0, None
    return r


def _ops(rec, cloud_state="connected"):
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
    o._from_cloud(f"snowbot/SN/app/{name}", {"state": 1}, now + 0.2)          # the cloud thread's look at it
    r._handle(f"snowbot/SN/app/{name}", {"state": 1}, now + 0.2)               # the recorder's, later: the same answer
    assert o.phone_app_at == 0.0 and seen(r, name) == []


@pytest.mark.parametrize("name", ["set_working_state", "smart_vision_control"])
def test_the_real_apps_beat_still_counts_while_the_panel_beats_every_four_seconds(tmp_path, monkeypatch, name):
    r = _recorder(tmp_path)
    o = _ops(r)
    clock = [5000.0]
    monkeypatch.setattr(recorder.time, "time", lambda: clock[0])
    for _ in range(4):                                                        # the panel: a send and its echo every 4 s
        r.mark_sent(name)
        o._from_cloud(f"snowbot/SN/app/{name}", {"state": 1}, clock[0] + 0.3)
        r._handle(f"snowbot/SN/app/{name}", {"state": 1}, clock[0] + 0.3)
        clock[0] += 4
    assert o.phone_app_at == 0.0 and seen(r, name) == []
    app_at = clock[0] - 4 + 1.5                                                # the app's, 1.5 s after ours
    o._from_cloud(f"snowbot/SN/app/{name}", {"state": 1}, app_at)
    r._handle(f"snowbot/SN/app/{name}", {"state": 1}, app_at)
    assert o.phone_app_at == app_at                                            # the interlock sees the real app
    assert len(seen(r, name)) == 1                                             # and the recorder logs another client


def test_one_send_accounts_for_one_echo_only(tmp_path):
    r = _recorder(tmp_path)
    r.mark_sent("set_working_state")
    t = r._sent["set_working_state"]
    assert r.own_echo("set_working_state", t + 0.1) is True
    assert r.own_echo("set_working_state", t + 0.1) is True                    # asked again: the same answer
    assert r.own_echo("set_working_state", t + 0.9) is False                   # a second message is somebody else's
    r.mark_sent("set_working_state")
    t2 = r._sent["set_working_state"]
    assert r.own_echo("set_working_state", t2 + 7) is False                    # an echo that never came ages out


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
    assert r.own_echo("pause", t + 1) is True and r.own_echo("pause", t + 1.5) is True
    assert r.own_echo("pause", t + 6) is False
    assert recorder.COUNTED_OWN == {"set_working_state", "smart_vision_control"}


# -- review round 1: timing under the lock, the setting rechecked, marks before the send -------------------------
def _controller(tmp_path, monkeypatch, clock):
    from tests.test_phase2 import make_controller
    store, cloud, c = make_controller(tmp_path, enabled=True)
    store.set_setting("robot_video_enabled", True)
    monkeypatch.setattr(control.time, "time", lambda: clock[0])
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
            answers.append(r.own_echo(cmd, __import__("time").time()))
            super().publish(cmd, payload)

    lk = RobotLink(rec, EchoingCloud(), store, mark_sent=r.mark_sent)
    lk.keep_awake_beat({"state": 1})
    assert answers == [True]
    assert r.own_echo("set_working_state", __import__("time").time() + 0.5) is False  # used once


def test_a_send_that_fails_takes_its_mark_back(tmp_path):
    r = _recorder(tmp_path)
    r._sent["set_working_state"] = 1.0

    class Failing(LinkCloud):
        def publish(self, cmd, payload):
            raise ConnectionError("down")

    lk = RobotLink(LinkRec(), Failing(), Store(str(tmp_path)), mark_sent=r.mark_sent)
    with pytest.raises(ConnectionError):
        lk.keep_awake_beat({"state": 1})
    assert r._sent["set_working_state"] == 1.0 and not r._own_beats["set_working_state"]
    assert r.own_echo("set_working_state", __import__("time").time()) is False


def test_a_send_recorded_after_a_message_arrived_is_not_what_it_echoes(tmp_path, monkeypatch):
    r = _recorder(tmp_path)
    monkeypatch.setattr(recorder.time, "time", lambda: 1000.5)
    r.mark_sent("set_working_state")
    assert r.own_echo("set_working_state", 1000.0) is False       # that message was somebody else's, from before
    assert r.own_echo("set_working_state", 1000.7) is True        # the real echo still finds its send


# -- review round 2: only what we send can be our echo ------------------------------------------------------------
def test_the_apps_camera_off_before_our_camera_on_echo_is_not_ours_and_leaves_our_send_pending(tmp_path, monkeypatch):
    r = _recorder(tmp_path)
    o = _ops(r)
    monkeypatch.setattr(recorder.time, "time", lambda: 1000.0)
    r.mark_sent("smart_vision_control")
    name = "smart_vision_control"
    o._from_cloud(f"snowbot/SN/app/{name}", {"state": 0}, 1000.2)           # the app switching the cameras off
    r._handle(f"snowbot/SN/app/{name}", {"state": 0}, 1000.2)
    assert o.phone_app_at == 1000.2 and len(seen(r, name)) == 1             # seen as the app's, not hidden
    assert r.own_echo(name, 1000.4, {"state": 1}) is True                    # our send was still waiting for its echo
    for odd in ({"state": 2}, {"state": True}, {"state": "1"}, {}):
        r.mark_sent(name)
        assert r.own_echo(name, 1000.6 + len(str(odd)), odd) is False
