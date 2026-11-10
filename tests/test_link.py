"""Local first, the cloud as a fallback (app/link.py): which route reads and commands take, falling back and coming
back, the controller repeating a safe command by the other route, map writes keeping one route, and the phone app
seen on the local broker. All with stand-ins: no broker, no robot."""
import asyncio

import pytest

from app import control, link as link_mod
from app.link import NoRoute, RobotLink
from app.mapwrite import MapWriter
from app.store import Store


class FakeLocal:
    def __init__(self):
        self.connected, self.reads, self.commands = True, [], []

    def request(self, cmd, payload=None):
        self.reads.append(cmd)

    def command(self, cmd, payload):
        self.commands.append(cmd)


class FakeCloud:
    def __init__(self, state="connected"):
        self.state, self.connected, self.sent = state, state == "connected", []

    def publish(self, cmd, payload):
        self.sent.append(cmd)


class FakeRec:
    def __init__(self):
        self.transport = FakeLocal()


def _link(tmp_path, mode=None, cloud="connected"):
    store = Store(str(tmp_path))
    if mode:
        store.set_setting("robot_route", mode)
    rec, cl = FakeRec(), FakeCloud(cloud)
    return RobotLink(rec, cl, store), rec, cl, store


def test_reads_and_commands_go_local_while_it_is_up(tmp_path):
    lk, rec, cl, _ = _link(tmp_path)
    assert lk.read("get_device_msg") == "local" and lk.command("pause", {}) == "local"
    assert rec.transport.reads == ["get_device_msg"] and rec.transport.commands == ["pause"] and cl.sent == []


def test_with_the_local_broker_down_everything_goes_through_the_cloud(tmp_path):
    lk, rec, cl, _ = _link(tmp_path)
    rec.transport.connected = False
    assert lk.read("get_map") == "cloud" and lk.command("cmd_recharge", {"cmd": 2}) == "cloud"
    assert cl.sent == ["get_map", "cmd_recharge"]


def test_unanswered_local_reads_move_reads_to_the_cloud_for_a_while(tmp_path, monkeypatch):
    lk, rec, cl, _ = _link(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(link_mod.time, "time", lambda: clock[0])
    lk.note_read("get_map", lk.read("get_map"), False)              # a slow read timing out does not count
    lk.note_read("get_map", lk.read("get_map"), False)
    assert lk.read("get_device_msg") == "local"
    for _ in range(link_mod.LOCAL_MISSES):
        lk.note_read("get_device_msg", lk.read("get_device_msg"), False)
    assert lk.read("get_device_msg") == "cloud"
    assert lk.command("pause", {}) == "local"             # commands still try local while it is connected
    clock[0] += link_mod.BACKOFF_S + 1
    assert lk.read("get_device_msg") == "local"


def test_a_route_change_is_reported_once(tmp_path):
    lk, rec, cl, _ = _link(tmp_path)
    seen = []
    lk.on_change = lambda what, before, after: seen.append((what, before, after))
    lk.read("get_device_msg")
    rec.transport.connected = False
    lk.read("get_device_msg")
    lk.read("get_device_msg")
    assert seen == [("reads", "local", "cloud")]


@pytest.mark.parametrize("mode, local, cloud, want", [
    ("local", True, True, "local"), ("local", False, True, None),
    ("cloud", True, True, "cloud"), ("cloud", True, False, None),
    ("auto", False, False, None),
])
def test_the_route_setting(tmp_path, mode, local, cloud, want):
    lk, rec, cl, _ = _link(tmp_path, mode, "connected" if cloud else "signed out")
    rec.transport.connected = local
    assert lk.command_route() == want
    if want is None:
        with pytest.raises(NoRoute):
            lk.command("pause", {})


def test_the_link_refuses_what_the_robot_must_never_get(tmp_path):
    lk, *_ = _link(tmp_path)
    from app.transport_local import ReadOnlyViolation
    for cmd in ("cmd_vel", "light_ctrl"):
        with pytest.raises(ReadOnlyViolation):
            lk.command(cmd, {})
    with pytest.raises(ReadOnlyViolation):
        lk.read("start_plan")


# -- the controller: a safe command not seen goes once more by the other route ---------------------------------
def _controller(tmp_path, monkeypatch, state):
    lk, rec, cl, store = _link(tmp_path)
    store.set_setting("control_enabled", True)
    monkeypatch.setattr(control, "REROUTE_AFTER_S", 0.05)

    async def context(args):
        return {"snapshot_age_s": 1, "route_state": "connected", "plans": [{"id": 1}], "error_code": 0, "battery": 90,
                "running": False, "yard": {"ok": True, "detail": "clear"}, "phone_app_active": False,
                "conditions": "Dry", "presence": True, "writing": False}

    def expect(cmd):
        return asyncio.get_running_loop().create_future()

    c = control.Controller(store, lk, context, expect, lambda: state)
    c.poll_s = 0.01
    return c, rec, cl, store


def test_a_pause_not_seen_is_sent_again_through_the_cloud(tmp_path, monkeypatch):
    c, rec, cl, store = _controller(tmp_path, monkeypatch, {"planning": 1, "planning_paused": 0})

    async def go():
        r = await c.send("pause", wait=False)
        await asyncio.sleep(0.2)
        return r

    r = asyncio.run(go())
    assert r["route"] == "local" and rec.transport.commands == ["pause"] and cl.sent == ["pause"]
    assert any(e["kind"] == "command_rerouted" for e in store.events(10))
    log = store.control_log()
    assert [(r["actor"], r["route"]) for r in log[:2]] == [("user (rerouted)", "cloud"), ("user", "local")]


def test_a_reroute_stands_down_when_a_newer_command_was_sent(tmp_path, monkeypatch):
    """Pause, then Resume within 10 s: the pause must not go again through the cloud and undo the resume."""
    state = {"planning": 1, "planning_paused": 0}
    c, rec, cl, store = _controller(tmp_path, monkeypatch, state)

    async def go():
        await c.send("pause", wait=False)
        await c.send("resume", wait=False)
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert rec.transport.commands == ["pause", "resume"] and cl.sent == []


def test_a_reroute_stands_down_when_control_is_switched_off(tmp_path, monkeypatch):
    c, rec, cl, store = _controller(tmp_path, monkeypatch, {"planning": 1, "planning_paused": 0})

    async def go():
        await c.send("pause", wait=False)
        store.set_setting("control_enabled", False)
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert cl.sent == []


def test_every_send_is_marked_as_the_panels_own(tmp_path):
    """The robot echoes commands back on app/<cmd>; the recorder must see them as the panel's, not someone else's
    (a guard would otherwise think a person had overridden it)."""
    store = Store(str(tmp_path))
    rec, cl, marked = FakeRec(), FakeCloud(), []
    lk = RobotLink(rec, cl, store, mark_sent=marked.append)
    lk.command("pause", {})
    lk.command("pause", {}, route="cloud")
    lk.read("get_device_msg")
    lk.command("smart_vision_control", {"state": 1})    # app/ops.py takes an unmarked echo for the phone app
    assert marked == ["pause", "pause", "get_device_msg", "smart_vision_control"]


def test_a_pause_that_took_is_not_sent_again(tmp_path, monkeypatch):
    c, rec, cl, store = _controller(tmp_path, monkeypatch, {"planning": 1, "planning_paused": 1})

    async def go():
        await c.send("pause", wait=False)
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert cl.sent == []


def test_a_start_is_never_sent_again_by_the_other_route(tmp_path, monkeypatch):
    c, rec, cl, store = _controller(tmp_path, monkeypatch, {"planning": 0, "planning_paused": 0})

    async def go():
        await c.send("start_plan", {"plan_id": 1}, wait=False)
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert rec.transport.commands == ["start_plan"] and cl.sent == []


# -- map writes keep one route ---------------------------------------------------------------------------------
def test_a_map_write_keeps_the_route_it_started_on(tmp_path, monkeypatch):
    monkeypatch.setattr(link_mod, "LOCAL_WRITES_PROVED", True)
    lk, rec, cl, store = _link(tmp_path)
    routes = []

    def publish(cmd, payload, route=None):
        got = lk.write_command(cmd, payload, route=route)
        routes.append(got)
        rec.transport.connected = False            # the local broker drops halfway through the write
        return got

    async def go():
        loop = asyncio.get_running_loop()

        def expect(cmd):
            fut = loop.create_future()
            fut.set_result({"state": 0, "topic": cmd})
            return fut

        w = MapWriter(store, publish, expect, None, None, lambda: "")
        async with w.lock:
            first = await w._send("save_nogozone", {"id": 1})
            second = await w._send("save_nogozone", {"id": 2})
            back = await w._send("del_nogozone", {"id": 1}, rollback=True)
        return first, second, back

    first, second, back = asyncio.run(go())
    assert first["ok"] and routes[0] == "local"
    # The local broker dropped mid-write: the rest of the write is refused rather than switched to the cloud ...
    assert not second["sent"] and "NoRoute" in second["error"]
    # ... but putting things back may take the cloud.
    assert back["ok"] and cl.sent == ["del_nogozone"]


# -- the phone app on the local broker ------------------------------------------------------------------------
def test_the_phone_app_is_seen_on_the_local_broker(tmp_path, monkeypatch):
    from app import ops as ops_mod
    o = ops_mod.Ops.__new__(ops_mod.Ops)
    lk, rec, cl, store = _link(tmp_path, cloud="signed out")
    rec.app_stream_at = 0.0
    o.link, o.rec, o.cloud, o.phone_app_at = lk, rec, cl, 0.0
    clock = [5000.0]
    monkeypatch.setattr(ops_mod.time, "time", lambda: clock[0])
    assert o.phone_app_active(60) is None                      # no cloud, no stream: not proof the app is closed
    rec.app_stream_at = clock[0] - 2
    assert o.phone_app_active(60) is True                      # the robot streams DeviceMSG: the app is open
    cl.state = "connected"
    rec.app_stream_at = 0.0
    assert o.phone_app_active(60) is False                     # with the cloud: no stream, no keep-awake = closed


def test_writes_go_only_through_the_cloud_until_local_writes_are_proved(tmp_path, monkeypatch):
    lk, rec, cl, _ = _link(tmp_path)
    monkeypatch.setattr(link_mod, "LOCAL_WRITES_PROVED", False)
    assert lk.write_route() == "cloud"
    cl.state, cl.connected = "signed out", False
    assert lk.write_route() is None
    with pytest.raises(NoRoute):
        lk.write_command("save_nogozone", {"id": 1})
    with pytest.raises(NoRoute):
        lk.write_command("save_nogozone", {"id": 1}, route="local")
    monkeypatch.setattr(link_mod, "LOCAL_WRITES_PROVED", True)
    cl.state, cl.connected = "connected", True
    assert lk.write_route() == "local"


def test_commands_and_writes_have_separate_doors(tmp_path):
    lk, *_ = _link(tmp_path)
    with pytest.raises(NoRoute):
        lk.command("save_nogozone", {"id": 1})                 # a write is not a run command
    with pytest.raises(NoRoute):
        lk.write_command("start_plan", {"id": 1})              # a run command is not a write


def test_a_local_broker_that_ignores_commands_moves_them_to_the_cloud(tmp_path):
    lk, rec, cl, store = _link(tmp_path)
    for _ in range(link_mod.LOCAL_MISSES):
        lk.note_command("local", False)
    assert lk.command_route() == "cloud"
    assert any(e["kind"] == "route_local_failing" for e in store.events(10))


def test_an_explicit_route_respects_the_setting(tmp_path):
    lk, *_ = _link(tmp_path, "local")
    with pytest.raises(NoRoute):
        lk.command("pause", {}, route="cloud")


def test_someone_elses_newer_command_stops_the_repeat(tmp_path, monkeypatch):
    """Pause from here, then the phone app resumes it: the pause must not be sent again through the cloud."""
    state = {"planning": 1, "planning_paused": 0}
    c, rec, cl, store = _controller(tmp_path, monkeypatch, state)
    others = [0.0]
    c.others_at = lambda cmd: others[0]

    async def go():
        await c.send("pause", wait=False)
        others[0] = __import__("time").time() + 0.01     # the phone app sent something just after
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert cl.sent == []


def test_a_command_the_robot_acts_on_late_is_not_a_miss(tmp_path, monkeypatch):
    state = {"planning": 0, "planning_paused": 0}
    c, rec, cl, store = _controller(tmp_path, monkeypatch, state)
    monkeypatch.setattr(control, "LATE_WATCH_S", 1.0)

    async def go():
        await c.send("start_plan", {"plan_id": 1}, wait=False)
        await asyncio.sleep(0.3)
        state.update(planning=1)                          # it starts late, inside the watch window
        await asyncio.sleep(0.3)

    asyncio.run(go())
    assert c.link._misses["commands"] == 0 and c.link.command_route() == "local"


def test_a_command_never_seen_counts_against_its_route(tmp_path, monkeypatch):
    c, rec, cl, store = _controller(tmp_path, monkeypatch, {"planning": 0, "planning_paused": 0})
    monkeypatch.setattr(control, "LATE_WATCH_S", 0.2)

    async def go():
        await c.send("start_plan", {"plan_id": 1}, wait=False)
        await asyncio.sleep(0.4)

    asyncio.run(go())
    assert c.link._misses["commands"] == 1


def test_an_auto_start_window_keeps_unknowns_hard(tmp_path):
    """Softening the unknown phone-app check is only for a window that asks first; an auto-start window sends at
    once and would be refused (and given up for the day) if the evaluation let it through."""
    from app import ops as ops_mod
    o = ops_mod.Ops.__new__(ops_mod.Ops)
    o.store = Store(str(tmp_path))

    async def context(args):
        return {"snapshot_age_s": 1, "route_state": "connected", "plans": [{"id": 1}], "error_code": 0, "battery": 90,
                "running": False, "yard": {"ok": True, "detail": "clear"}, "phone_app_active": None,
                "conditions": "Dry", "presence": True, "writing": False}

    o.context = context
    asks = asyncio.run(o._evaluate_window({"plan_id": 1}))
    auto = asyncio.run(o._evaluate_window({"plan_id": 1, "auto_start": True}))
    assert asks[0] is True and auto[0] is False and "Phone app" in auto[2]


def test_a_late_echo_of_the_watched_command_does_not_stop_the_repeat(tmp_path, monkeypatch):
    """In the weak patch the robot's echo of our own pause can arrive more than 5 s late and look like someone else's;
    only another command stands the repeat down."""
    c, rec, cl, store = _controller(tmp_path, monkeypatch, {"planning": 1, "planning_paused": 0})
    seen = {}
    c.others_at = lambda cmd: max((t for name, t in seen.items() if name != cmd), default=0.0)

    async def go():
        await c.send("pause", wait=False)
        seen["pause"] = __import__("time").time() + 0.01     # our own pause echoed back late
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert cl.sent == ["pause"]
