"""Dead ends (captured 2026-10-11, app-edit-1791752041): added, replaced and deleted like pathways with save_deadend /
del_deadend, their own settings carried over to a replacement and edited with save_deadend_params, every write read
back. The fake robot answers as the real one did: the saved dead end in the reply, its start linked to the area it
starts in, read_deadend_params id 0 (defaults) until the settings are saved."""
import asyncio
import copy

import pytest

from app import mapstudio as ms, mapwrite as mw
from app.store import Store
from app.commands import READ_ONLY_COMMANDS
from tests.test_mapstudio import REF, lonlat, sample_map

DEFAULTS = {"blade_height": 0, "blade_speed": 80, "en_blade": True, "en_trimmer": False, "id": 0,
            "land_push_pod_place": 50, "plan_speed": 0.5, "sensor_switch": 0}


class DeadEndRobot:
    def __init__(self, raw, fail=(), keep=None):
        self.map = copy.deepcopy(raw)
        self.next_id = 801
        self.sent, self.params, self.pending = [], {}, {}
        self.fail = set(fail)
        self.keep = keep or {}                        # field -> value the robot keeps whatever it is sent

    def expect(self, cmd):
        fut = asyncio.get_running_loop().create_future()
        self.pending.setdefault(cmd, []).append(fut)
        return fut

    def publish(self, cmd, payload):
        self.sent.append((cmd, copy.deepcopy(payload)))
        reply = self._apply(cmd, payload)
        for fut in self.pending.pop(cmd, []):
            fut.set_result(reply)

    def _apply(self, cmd, payload):
        if cmd in self.fail:
            self.fail.discard(cmd)
            return {"state": -1, "msg": "refused"}
        if cmd == "save_deadend":
            f = {**copy.deepcopy(payload), "id": self.next_id, "start_id": 1, "end_id": -1, "head_type": 0}
            self.next_id += 1
            self.map.setdefault("deadends", []).append(f)
            return {"state": 0, "msg": "", "data": f}
        if cmd == "del_deadend":
            self.map["deadends"] = [f for f in self.map["deadends"] if f["id"] != payload["id"]]
            self.params.pop(payload["id"], None)
            return {"state": 0, "msg": ""}
        if cmd == "save_deadend_params":
            got = {k: payload[k] for k in mw.DEADEND_READ_KEYS}
            self.params[payload["id"]] = {**got, **self.keep, "id": payload["id"]}
            return {"state": 0, "msg": "Deadend settings saved successfully.", "data": ""}
        return {"state": 0, "msg": "", "data": ""}

    async def read_map(self):
        return copy.deepcopy(self.map)

    async def read_params(self, pid):
        return None

    async def read_deadend(self, did):
        return copy.deepcopy(self.params.get(did, DEFAULTS))


def writer(robot, tmp_path):
    store = Store(str(tmp_path))
    return store, mw.MapWriter(store, robot.publish, robot.expect, robot.read_map, robot.read_params, lambda: "me@x",
                               read_deadend=robot.read_deadend)


def with_deadend():
    base = sample_map()
    base["deadends"] = [{"id": 70, "name": "spur", "ref": base["areas"][0]["ref"], "start_id": 1, "end_id": -1,
                         "connectids": [], "trimming_edges": [],
                         "range": [{"x": 10.0, "y": 10.0, "phi": 0.0}, {"x": 10.0, "y": 25.0, "phi": 1.57}]}]
    return base


def test_the_read_is_on_the_read_only_list_and_the_writes_are_map_writes():
    assert "read_deadend_params" in READ_ONLY_COMMANDS
    assert {"save_deadend", "del_deadend", "save_deadend_params"} <= mw.WRITE_COMMANDS
    assert not ({"save_deadend", "del_deadend", "save_deadend_params"} & READ_ONLY_COMMANDS)


def test_a_new_dead_end_is_sent_like_the_app_and_keeps_the_robots_defaults(tmp_path):
    base = sample_map()
    draft, _ = ms.apply_edit(base, {"op": "add", "kind": "deadends", "name": "to the shed",
                                    "coords": [lonlat(10, 10), lonlat(10, 18), lonlat(10, 26)]}, REF)
    robot = DeadEndRobot(base)
    _, w = writer(robot, tmp_path)
    r = asyncio.run(w.write(base, draft, REF))
    assert r["ok"] and r["verified"], r
    cmds = [c for c, _ in robot.sent]
    assert cmds == ["save_deadend", "upload_cloud_map_backup"]           # no settings sent: the robot's defaults stay
    payload = robot.sent[0][1]
    assert sorted(payload) == ["connectids", "name", "range", "ref", "trimming_edges"]   # exactly the app's keys
    assert payload["name"] == "to the shed" and len(payload["range"]) == 3


def test_a_dead_end_must_start_inside_an_area():
    base = sample_map()
    draft, _ = ms.apply_edit(base, {"op": "add", "kind": "deadends", "coords": [lonlat(10, 30), lonlat(10, 40)]}, REF)
    issues = ms.validate_draft(base, draft, REF)
    assert any(i["code"] == "deadend_start" and i["level"] == "error" for i in issues)
    ok, _ = ms.apply_edit(base, {"op": "add", "kind": "deadends", "coords": [lonlat(10, 10), lonlat(10, 30)]}, REF)
    assert not any(i["code"] == "deadend_start" for i in ms.validate_draft(base, ok, REF))


def test_a_reshaped_dead_end_is_replaced_and_keeps_its_settings(tmp_path):
    base = with_deadend()
    robot = DeadEndRobot(base)
    robot.params[70] = {**DEFAULTS, "id": 70, "plan_speed": 0.7, "blade_height": 3}
    draft, _ = ms.apply_edit(base, {"op": "shape", "kind": "deadends", "id": 70,
                                    "coords": [lonlat(10, 10), lonlat(12, 26)]}, REF)
    _, w = writer(robot, tmp_path)
    r = asyncio.run(w.write(base, draft, REF))
    assert r["ok"] and r["verified"], r
    assert [c for c, _ in robot.sent] == ["save_deadend", "save_deadend_params", "del_deadend", "upload_cloud_map_backup"]
    new = robot.sent[1][1]
    assert new["id"] == 801 and new["plan_speed"] == 0.7 and new["blade_height"] == 3
    assert list(new) == ["blade_speed", "blade_height", "turn_type", "land_push_pod_place", "route_angle", "route_dis",
                         "en_blade", "plan_speed", "sensor_switch", "en_trimmer", "id"]       # the app's order


def test_a_dead_end_on_defaults_is_replaced_without_sending_settings(tmp_path):
    base = with_deadend()
    robot = DeadEndRobot(base)
    draft, _ = ms.apply_edit(base, {"op": "rename", "kind": "deadends", "id": 70, "name": "spur 2"}, REF)
    _, w = writer(robot, tmp_path)
    r = asyncio.run(w.write(base, draft, REF))
    assert r["ok"], r
    assert "save_deadend_params" not in [c for c, _ in robot.sent]


def test_a_failed_delete_puts_the_new_one_back_out(tmp_path):
    base = with_deadend()
    robot = DeadEndRobot(base, fail={"del_deadend"})
    draft, _ = ms.apply_edit(base, {"op": "shape", "kind": "deadends", "id": 70,
                                    "coords": [lonlat(10, 10), lonlat(12, 26)]}, REF)
    _, w = writer(robot, tmp_path)
    r = asyncio.run(w.write(base, draft, REF))
    assert not r["ok"] and r["rollback_ok"], r
    assert [f["id"] for f in robot.map["deadends"]] == [70]


def test_settings_are_edited_read_back_and_refused_out_of_range(tmp_path):
    robot = DeadEndRobot(with_deadend())
    _, w = writer(robot, tmp_path)
    r = asyncio.run(mw.write_deadend_params(w, 70, {"plan_speed": 0.6, "en_blade": False}))
    assert r["ok"], r
    assert robot.params[70]["plan_speed"] == 0.6 and robot.params[70]["en_blade"] is False
    for bad in ({"plan_speed": 0.9}, {"blade_height": 3}, {"en_trimmer": "yes"}, {"made_up": 1}):
        with pytest.raises(ValueError):
            asyncio.run(mw.write_deadend_params(w, 70, bad))


def test_a_setting_the_robot_does_not_keep_is_put_back(tmp_path):
    robot = DeadEndRobot(with_deadend(), keep={"en_trimmer": False})
    _, w = writer(robot, tmp_path)
    r = asyncio.run(mw.write_deadend_params(w, 70, {"en_trimmer": True}))
    assert not r["ok"] and "en_trimmer" in r["error"] and r["rollback_ok"]


# -- from the review ------------------------------------------------------------------------------------------------
def test_settings_for_a_dead_end_no_longer_on_the_map_are_refused(tmp_path):
    robot = DeadEndRobot(sample_map())                    # no dead end 70 on the robot (its id may now be an area's)
    _, w = writer(robot, tmp_path)
    with pytest.raises(mw.WriteRefused, match="not on the robot's map"):
        asyncio.run(mw.write_deadend_params(w, 70, {"plan_speed": 0.6}))
    assert not robot.sent


def test_a_read_back_that_answers_id_0_is_not_a_save(tmp_path):
    robot = DeadEndRobot(with_deadend())

    def ignore(cmd, payload, _orig=robot._apply):
        if cmd == "save_deadend_params":
            return {"state": 0, "msg": "", "data": ""}      # says yes, keeps nothing: reads stay id 0
        return _orig(cmd, payload)
    robot._apply = ignore
    _, w = writer(robot, tmp_path)
    r = asyncio.run(mw.write_deadend_params(w, 70, {"plan_speed": 0.5}))   # equal to the default value
    assert r.get("unchanged") or not r["ok"]
    r = asyncio.run(mw.write_deadend_params(w, 70, {"en_trimmer": True}))
    assert not r["ok"] and "id" in r["error"]


def test_on_off_as_numbers_reads_as_booleans(tmp_path):
    robot = DeadEndRobot(with_deadend())
    robot.params[70] = {**DEFAULTS, "id": 70, "en_blade": 1, "en_trimmer": 0}
    _, w = writer(robot, tmp_path)
    got, err = asyncio.run(mw._read_deadend(w, 70))
    assert err is None and got["en_blade"] is True and got["en_trimmer"] is False


def test_a_configured_read_missing_a_field_refuses_the_replacement(tmp_path):
    base = with_deadend()
    robot = DeadEndRobot(base)
    robot.params[70] = {"id": 70, "plan_speed": 0.7}      # configured, but incomplete
    draft, _ = ms.apply_edit(base, {"op": "rename", "kind": "deadends", "id": 70, "name": "spur 2"}, REF)
    _, w = writer(robot, tmp_path)
    with pytest.raises(mw.WriteRefused, match="lack"):
        asyncio.run(w.write(base, draft, REF))
    assert not robot.sent


def test_a_dead_end_the_robot_does_not_link_to_its_area_is_undone(tmp_path):
    base = sample_map()
    robot = DeadEndRobot(base)
    orig = robot._apply

    def unlinked(cmd, payload):
        r = orig(cmd, payload)
        if cmd == "save_deadend":
            r["data"]["start_id"] = -1
            robot.map["deadends"][-1]["start_id"] = -1
        return r
    robot._apply = unlinked
    draft, _ = ms.apply_edit(base, {"op": "add", "kind": "deadends", "coords": [lonlat(10, 10), lonlat(10, 26)]}, REF)
    _, w = writer(robot, tmp_path)
    r = asyncio.run(w.write(base, draft, REF))
    assert not r["ok"] and any("linked its start" in p for p in r.get("problems", [])) and r["rollback_ok"]
    assert robot.map["deadends"] == []


def test_the_recorder_drops_a_late_id_0_answer_after_a_timeout():
    from app import recorder as recmod

    class Rec:
        _pathway_lock = asyncio.Lock()
        _deadend_timeouts = []
        answers = []

        async def request(self, cmd, timeout=10, payload=None):
            a = self.answers.pop(0)
            if a == "timeout":
                raise asyncio.TimeoutError
            return {"state": 0, "data": a}

    r = Rec()
    r.answers = ["timeout"]
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(recmod.Recorder.read_deadend_params(r, 70))
    r.answers = [{**DEFAULTS}, {**DEFAULTS, "id": 70, "plan_speed": 0.7}]   # the late id-0, then the real answer
    got = asyncio.run(recmod.Recorder.read_deadend_params(r, 70))
    assert got["id"] == 70 and got["plan_speed"] == 0.7
    r.answers = [{**DEFAULTS}]                             # nothing outstanding any more: id 0 means defaults
    assert asyncio.run(recmod.Recorder.read_deadend_params(r, 71))["id"] == 0
