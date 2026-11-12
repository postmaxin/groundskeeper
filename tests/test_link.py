"""The one route to the robot (app/link.py): everything goes through the Yarbo cloud, nothing goes while it is down,
the doors for reads, run commands and writes stay apart, every send is marked as the panel's own, and the phone app
is seen. All with stand-ins: no broker, no robot."""
import asyncio

import pytest

from app import control
from app.link import NoRoute, RobotLink
from app.mapwrite import MapWriter
from app.store import Store


class FakeCloud:
    def __init__(self, state="connected"):
        self.state, self.connected, self.sent = state, state == "connected", []

    def publish(self, cmd, payload):
        self.sent.append(cmd)


class FakeRec:
    app_stream_at = 0.0


def _link(tmp_path, cloud="connected"):
    store = Store(str(tmp_path))
    rec, cl = FakeRec(), FakeCloud(cloud)
    return RobotLink(rec, cl, store), rec, cl, store


def test_reads_commands_and_writes_go_through_the_cloud(tmp_path):
    lk, rec, cl, _ = _link(tmp_path)
    assert lk.read("get_device_msg") == "cloud" and lk.command("pause", {}) == "cloud"
    assert lk.write_command("save_nogozone", {"id": 1}) == "cloud"
    assert cl.sent == ["get_device_msg", "pause", "save_nogozone"]
    assert lk.status()["commands"] == "cloud" and lk.state == "connected"


@pytest.mark.parametrize("state", ["signed out", "connecting", "reconnecting", "needs login"])
def test_nothing_goes_while_the_cloud_is_not_connected(tmp_path, state):
    lk, rec, cl, _ = _link(tmp_path, cloud=state)
    for send in (lambda: lk.read("get_map"), lambda: lk.command("pause", {}),
                 lambda: lk.write_command("save_nogozone", {"id": 1})):
        with pytest.raises(NoRoute) as e:
            send()
        assert state in str(e.value)
    assert cl.sent == [] and lk.state == "no route to the robot" and lk.read_route() is None


def test_a_cloud_that_takes_no_commands_still_reads(tmp_path):
    lk, rec, cl, _ = _link(tmp_path)
    cl.takes_commands = False                       # the demo robot (app/demo_robot.py DemoCloud)
    assert lk.read("get_device_msg") == "cloud"
    with pytest.raises(NoRoute):
        lk.command("pause", {})
    assert cl.sent == ["get_device_msg"] and lk.state == "no route to the robot"


def test_a_write_names_no_other_route(tmp_path):
    lk, rec, cl, _ = _link(tmp_path)
    with pytest.raises(NoRoute):
        lk.write_command("save_nogozone", {"id": 1}, route="local")
    assert lk.write_command("save_nogozone", {"id": 1}, route="cloud") == "cloud"


def test_the_link_refuses_what_the_robot_must_never_get(tmp_path):
    lk, rec, cl, _ = _link(tmp_path)
    from app.commands import ReadOnlyViolation
    for cmd in ("cmd_vel", "light_ctrl"):
        with pytest.raises(ReadOnlyViolation):
            lk.command(cmd, {})
    with pytest.raises(ReadOnlyViolation):
        lk.read("start_plan")
    assert cl.sent == []


def test_commands_and_writes_have_separate_doors(tmp_path):
    lk, *_ = _link(tmp_path)
    with pytest.raises(NoRoute):
        lk.command("save_nogozone", {"id": 1})                 # a write is not a run command
    with pytest.raises(NoRoute):
        lk.write_command("start_plan", {"id": 1})              # a run command is not a write


@pytest.mark.parametrize("cmd", ["smart_vision_control", "set_working_state"])
@pytest.mark.parametrize("payload", [{"state": 0}, {}, {"state": 1, "x": 1}, {"state": True}, {"state": 1.0}])
def test_the_link_refuses_any_on_only_payload_but_on(tmp_path, cmd, payload):
    lk, rec, cl, _ = _link(tmp_path)
    with pytest.raises(NoRoute):
        lk.command(cmd, payload)
    assert cl.sent == []
    lk.command(cmd, {"state": 1})
    assert cl.sent == [cmd]


def test_every_send_is_marked_as_the_panels_own(tmp_path):
    """The robot echoes commands back on app/<cmd>; the recorder must see them as the panel's, not someone else's
    (a guard would otherwise think a person had overridden it)."""
    store = Store(str(tmp_path))
    rec, cl, marked = FakeRec(), FakeCloud(), []
    lk = RobotLink(rec, cl, store, mark_sent=marked.append)
    lk.command("pause", {})
    lk.read("get_device_msg")
    lk.command("smart_vision_control", {"state": 1})    # app/ops.py takes an unmarked echo for the phone app
    lk.write_command("save_nogozone", {"id": 1})
    assert marked == ["pause", "get_device_msg", "smart_vision_control", "save_nogozone"]


def test_the_keep_awake_is_not_the_last_command(tmp_path):
    lk, *_ = _link(tmp_path)
    lk.command("pause", {})
    lk.command("set_working_state", {"state": 1})
    assert lk.last_command["cmd"] == "pause"


def test_a_pause_goes_once_and_is_never_repeated(tmp_path):
    """With one route there is nothing to repeat a command on: a pause the robot doesn't show goes once."""
    lk, rec, cl, store = _link(tmp_path)
    store.set_setting("control_enabled", True)

    async def context(args):
        return {"snapshot_age_s": 1, "route_state": "connected", "plans": [{"id": 1}], "error_code": 0, "battery": 90,
                "running": True, "yard": {"ok": True, "detail": "clear"}, "phone_app_active": False,
                "conditions": "Dry", "presence": True, "writing": False}

    def expect(cmd):
        return asyncio.get_running_loop().create_future()

    c = control.Controller(store, lk, context, expect, lambda: {"planning": 1, "planning_paused": 0})
    c.poll_s = 0.01

    async def go():
        out = await c.send("pause", wait=False)
        await asyncio.sleep(0.2)
        return out

    out = asyncio.run(go())
    assert out["sent"] and out["route"] == "cloud" and cl.sent == ["pause"]


def test_a_map_write_that_loses_the_cloud_halfway_is_refused_and_put_back_when_it_returns(tmp_path):
    lk, rec, cl, store = _link(tmp_path)

    def publish(cmd, payload, route=None):
        got = lk.write_command(cmd, payload, route=route)
        if cmd == "save_nogozone":
            cl.state, cl.connected = "reconnecting", False      # the cloud drops halfway through the write
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
            second = await w._send("save_pathway", {"id": 2})
            cl.state, cl.connected = "connected", True          # back for the put-back
            back = await w._send("del_nogozone", {"id": 1}, rollback=True)
        return first, second, back

    first, second, back = asyncio.run(go())
    assert first["ok"] and not second["sent"] and "NoRoute" in second["error"]
    assert back["ok"] and cl.sent == ["save_nogozone", "del_nogozone"]


# -- the phone app ---------------------------------------------------------------------------------------------
def test_the_phone_app_is_seen(tmp_path, monkeypatch):
    from app import ops as ops_mod
    o = ops_mod.Ops.__new__(ops_mod.Ops)
    lk, rec, cl, store = _link(tmp_path, cloud="signed out")
    o.link, o.rec, o.cloud, o.phone_app_at = lk, rec, cl, 0.0
    clock = [5000.0]
    monkeypatch.setattr(ops_mod.time, "time", lambda: clock[0])
    assert o.phone_app_active(60) is None                      # no cloud, no stream: not proof the app is closed
    rec.app_stream_at = clock[0] - 2
    assert o.phone_app_active(60) is True                      # the robot streams DeviceMSG: the app is open
    cl.state, cl.connected_since = "connected", 0.0
    rec.app_stream_at = 0.0
    assert o.phone_app_active(60) is False                     # no stream, no keep-awake = closed


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
