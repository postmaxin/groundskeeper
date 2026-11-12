"""Pathway mowing-settings editor: payload, validation, verified write and restore (2026-10-06)."""
import asyncio
import copy

import pytest

from app import mapwrite as mw
from app.store import Store

# What read_mower_pathway_params returned for Pathway 5 (52) and Pathway 10 (758) on 2026-10-06.
P52 = {"blade_height": 0, "blade_speed": 80, "current_offset": -1.0, "en_blade": False, "en_trimmer": False, "id": 52,
       "land_push_pod_place": 50, "offset_enable": False, "offset_range": [-0.5, 0.5], "offset_step": 0.1,
       "pathway_obs_perception_sw": True, "plan_speed": 0.5, "sensor_switch": 11, "turn_type": 0}


class ParamRobot:
    """Keeps pathway settings; save replies state 0 (as in capture C2). `ignore` = keys the robot silently drops."""

    def __init__(self, params, ignore=(), refuse=False, silent=False):
        self.params = {params["id"]: copy.deepcopy(params)}
        self.ignore, self.refuse, self.silent, self.sent, self.pending = set(ignore), refuse, silent, [], {}
        self.reads, self.read_fail_after, self.wrong_id = 0, None, None

    def expect(self, cmd):
        fut = asyncio.get_running_loop().create_future()
        self.pending.setdefault(cmd, []).append(fut)
        return fut

    def publish(self, cmd, payload):
        self.sent.append((cmd, copy.deepcopy(payload)))
        if self.refuse:
            reply = {"state": -1, "msg": "busy"}
        else:
            cur = self.params[payload["id"]]
            for k, v in payload.items():
                if k not in self.ignore and k in cur:
                    cur[k] = v
            self.ignore = set()          # only the first save is mangled
            reply = {"state": 0, "msg": "", "data": ""}
        if self.silent:                  # applied, but the reply is lost
            return
        for fut in self.pending.pop(cmd, []):
            fut.set_result(reply)

    async def read_map(self):
        return {}

    async def read_params(self, pid):
        self.reads += 1
        if self.read_fail_after is not None and self.reads > self.read_fail_after:
            raise TimeoutError()
        if self.wrong_id is not None:
            return {**copy.deepcopy(self.params[pid]), "id": self.wrong_id, "en_blade": True, "blade_height": 34}
        return copy.deepcopy(self.params.get(pid))


def _writer(robot, tmp_path):
    store = Store(str(tmp_path))
    return store, mw.MapWriter(store, robot.publish, robot.expect, robot.read_map, robot.read_params, lambda: "")


def test_payload_keeps_everything_and_only_changes_what_was_asked():
    p = mw.pathway_params_payload(P52, {"plan_speed": 0.6}, 52)
    assert p["plan_speed"] == 0.6 and p["id"] == 52
    assert {k: p[k] for k in ("blade_speed", "en_blade", "sensor_switch", "offset_range")} == \
        {"blade_speed": 80, "en_blade": False, "sensor_switch": 11, "offset_range": [-0.5, 0.5]}
    assert "current_offset" not in p and p["route_angle"] == 0 and p["route_dis"] == 0.0   # as the app sends it


def test_obstacle_sensing_keeps_the_sensor_switch_bit_in_step():
    off = mw.pathway_params_payload(P52, {"pathway_obs_perception_sw": False}, 52)
    assert off["sensor_switch"] == 10                       # Pathway 10 in the app: off -> 10
    on = mw.pathway_params_payload({**P52, "pathway_obs_perception_sw": False, "sensor_switch": 10},
                                   {"pathway_obs_perception_sw": True}, 52)
    assert on["sensor_switch"] == 11


@pytest.mark.parametrize("changes", [{"blade_height": 30}, {"en_blade": 1}, {"plan_speed": 0.9}, {"plan_speed": 0.0},
                                     {"plan_speed": True}, {"en_trimmer": 1}, {"nonsense": 1}])
def test_read_only_and_out_of_range_changes_are_refused(changes):
    with pytest.raises(ValueError):
        mw.pathway_params_payload(P52, changes, 52)


def test_a_change_is_written_read_back_and_kept_in_history(tmp_path):
    robot = ParamRobot(P52)
    store, w = _writer(robot, tmp_path)
    r = asyncio.run(mw.write_pathway_params(w, 52, {"plan_speed": 0.6, "en_trimmer": True}))
    assert r["ok"] and r["after"]["plan_speed"] == 0.6 and r["after"]["en_trimmer"] is True
    assert [c for c, _ in robot.sent] == ["save_mower_pathway_params"]
    assert len(store.versions("pathway_params:52")) == 2
    assert store.control_log()[0]["cmd"] == "save_mower_pathway_params"


def test_no_change_sends_nothing(tmp_path):
    robot = ParamRobot(P52)
    _, w = _writer(robot, tmp_path)
    r = asyncio.run(mw.write_pathway_params(w, 52, {"plan_speed": 0.5}))
    assert r["ok"] and r.get("unchanged") and robot.sent == []


def test_a_value_the_robot_did_not_keep_is_put_back(tmp_path):
    robot = ParamRobot(P52, ignore={"plan_speed"})
    store, w = _writer(robot, tmp_path)
    r = asyncio.run(mw.write_pathway_params(w, 52, {"plan_speed": 0.6, "en_trimmer": True}))
    assert not r["ok"] and "plan_speed" in r["error"] and r["rollback_ok"]
    assert robot.params[52]["en_trimmer"] is False and len(robot.sent) == 2


def test_a_refused_save_changes_nothing(tmp_path):
    robot = ParamRobot(P52, refuse=True)
    _, w = _writer(robot, tmp_path)
    r = asyncio.run(mw.write_pathway_params(w, 52, {"plan_speed": 0.6}))
    assert not r["ok"] and "busy" in r["error"] and robot.params[52]["plan_speed"] == 0.5


def test_a_reply_for_another_pathway_is_never_used(tmp_path):
    """Review finding 1: replies are matched by topic; a stray reply must not become the base of a save."""
    robot = ParamRobot(P52)
    robot.wrong_id = 758
    _, w = _writer(robot, tmp_path)
    with pytest.raises(mw.WriteRefused):
        asyncio.run(mw.write_pathway_params(w, 52, {"plan_speed": 0.6}))
    assert robot.sent == [] and robot.params[52]["en_blade"] is False


def test_a_failed_read_back_is_reported_as_unverified_not_rolled_back(tmp_path):
    robot = ParamRobot(P52)
    robot.read_fail_after = 1
    store, w = _writer(robot, tmp_path)
    r = asyncio.run(mw.write_pathway_params(w, 52, {"plan_speed": 0.6}))
    assert not r["ok"] and r["unverified"] and len(robot.sent) == 1
    assert any(e["kind"] == "pathway_settings_unverified" for e in store.events(10))


def test_a_save_whose_reply_is_lost_is_still_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "REPLY_TIMEOUT_S", 0.05)
    robot = ParamRobot(P52, silent=True)
    _, w = _writer(robot, tmp_path)
    r = asyncio.run(mw.write_pathway_params(w, 52, {"plan_speed": 0.6}))
    assert r["ok"] and r["after"]["plan_speed"] == 0.6


def test_every_field_is_checked_on_read_back(tmp_path):
    """A save that changed an untouched field (e.g. blades) is caught and put back."""
    robot = ParamRobot(P52)
    orig = robot.publish

    def publish(cmd, payload):
        orig(cmd, payload)
        if len(robot.sent) == 1:
            robot.params[52]["en_blade"] = True
    robot.publish = publish
    _, w = _writer(robot, tmp_path)
    r = asyncio.run(mw.write_pathway_params(w, 52, {"plan_speed": 0.6}))
    assert not r["ok"] and "en_blade" in r["error"] and r["rollback_ok"] and robot.params[52]["en_blade"] is False


@pytest.mark.parametrize("read", [{k: v for k, v in P52.items() if k != "sensor_switch"}, {**P52, "new_firmware_field": 3}])
def test_an_incomplete_or_unknown_read_is_refused(read):
    with pytest.raises(ValueError):
        mw.pathway_params_payload(read, {"plan_speed": 0.6}, 52)


def test_obstacle_sensing_is_locked_for_an_unseen_sensor_value():
    odd = {**P52, "sensor_switch": 3}
    assert mw.obstacle_lock(odd) and mw.obstacle_lock(P52) is None
    with pytest.raises(ValueError):
        mw.pathway_params_payload(odd, {"pathway_obs_perception_sw": False}, 52)
    assert mw.pathway_params_payload(odd, {"plan_speed": 0.6}, 52)["sensor_switch"] == 3   # untouched otherwise


def _gate(**over):
    import time
    from types import SimpleNamespace
    from app import main
    store_settings = {"map_write_enabled": over.get("enabled", True)}
    rec = SimpleNamespace(store=SimpleNamespace(setting=lambda k, d=None: store_settings.get(k, d)),
                          snapshot_at=time.time() - over.get("age", 2), run_id=over.get("run_id"),
                          plan_active=lambda: over.get("active", False))
    ops = SimpleNamespace(cloud=SimpleNamespace(state=over.get("cloud", "connected")),
                          controller=SimpleNamespace(start_settling=lambda: over.get("starting")))
    from app.link import RobotLink
    ops.link = RobotLink(rec, ops.cloud, rec.store)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rec=rec, ops=ops)))
    return main._pathway_write_block(req)


def test_the_write_gate():
    assert _gate() is None
    assert "switched off" in _gate(enabled=False)
    assert "no recent data" in _gate(age=600)
    assert "plan is running" in _gate(active=True)
    assert "unknown" in _gate(active=None)
    assert "cloud" in _gate(cloud="signed out")
    assert "a moment" in _gate(starting="Resume was sent 3 s ago; try again in a moment")     # #69


def test_a_map_write_that_cannot_read_a_replaced_pathways_settings_is_refused(tmp_path):
    """Second review: a replaced pathway used to fall back to the defaults when its settings read failed."""
    from tests.test_mapstudio import REF, sample_map
    from tests import test_mapwrite as tm
    robot = tm.FakeRobot(sample_map())

    async def broken(pid):
        raise TimeoutError()
    robot.read_params = broken
    store, w = tm.writer(robot, tmp_path)
    draft = copy.deepcopy(robot.map)
    p = draft["pathways"][0]
    p["range"] = [{**pt, "x": pt["x"] + 0.5} for pt in p["range"]]
    with pytest.raises(mw.WriteRefused):
        asyncio.run(w.write(robot.map, draft, REF))
    assert robot.sent == []


def test_the_gate_is_checked_again_inside_the_lock(tmp_path):
    robot = ParamRobot(P52)
    _, w = _writer(robot, tmp_path)
    with pytest.raises(mw.WriteRefused):
        asyncio.run(mw.write_pathway_params(w, 52, {"plan_speed": 0.6}, check=lambda: "a plan is running"))
    assert robot.sent == []
