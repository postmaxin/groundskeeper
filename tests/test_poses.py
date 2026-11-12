"""Position samples for the 3D robot and Follow (#35): kept in the robot's own pose order, so the backlog the robot
sends after a dropout (old poses mixed with new ones) never sends the drawn robot back on a detour."""
from types import SimpleNamespace

from app import demo_robot as dr
from app.config import Config
from app.recorder import POSE_RESET_S, Recorder
from app.store import Store


def _recorder(tmp_path) -> Recorder:
    async def no_imagery(*a, **k):
        return None
    cfg = Config(data_dir=str(tmp_path), port=8425, poll_idle_s=60,
                 poll_active_s=2, map_refresh_s=21600, raw_every_s=60, raw_keep_days=90, run_idle_end_s=300,
                 tempest_port=0, ha_url=None, ha_token=None, demo=True, frigate_url=None, panel_url=None)
    r = Recorder(cfg, Store(str(tmp_path)), SimpleNamespace(yard_meta=None, ensure_yard=no_imagery), None)
    r.gps_ref = dr.REF
    return r


def _pose(r: Recorder, x: float, robot_s: float | None, ts: float) -> None:
    odom = {"x": x, "y": 0.0, "phi": 0.0}
    if robot_s is not None:
        odom["timestamp"] = int(robot_s * 1000)
    r._ingest({"CombinedOdom": odom, "WheelSpeedMSG": {"left": 0.5, "right": 0.5}}, ts, full=False)


T0 = 1_790_000_000.0      # the robot's clock: epoch time (CombinedOdom sends it in milliseconds)


def test_backlog_after_a_dropout_is_dropped(tmp_path):
    r = _recorder(tmp_path)
    for i, robot_s in enumerate([100, 101, 150, 102, 103, 151, 152]):     # 102, 103: late, from before the dropout
        _pose(r, float(robot_s), T0 + robot_s, 1000.0 + i)
    kept = r.poses_since(0)
    assert [p["t"] - T0 for p in kept] == [100, 101, 150, 151, 152]
    assert all(a["seq"] < b["seq"] for a, b in zip(kept, kept[1:]))
    assert kept[-1]["speed"] == 0.5 and kept[-1]["heading"] is not None
    assert r.poses_since(kept[2]["seq"]) == kept[3:]


def test_same_pose_again_is_not_a_new_sample(tmp_path):
    r = _recorder(tmp_path)
    _pose(r, 1.0, T0 + 200, 1000.0)
    r._ingest({"BatteryMSG": {"capacity": 80}}, 1000.5, full=False)        # no CombinedOdom: not a position sample
    _pose(r, 1.0, T0 + 200, 1001.0)                                        # the same pose repeated
    assert len(r.poses_since(0)) == 1


def test_clock_set_back_is_taken_after_a_while(tmp_path):
    r = _recorder(tmp_path)
    _pose(r, 1.0, T0 + 5000, 1000.0)
    _pose(r, 2.0, T0 + 10, 1001.0)                                         # older by the robot's clock: dropped
    assert len(r.poses_since(0)) == 1
    _pose(r, 3.0, T0 + 11, 1000.0 + POSE_RESET_S + 1)                      # nothing newer for a while: taken
    assert [p["t"] - T0 for p in r.poses_since(0)] == [5000, 11]


def test_same_time_is_never_a_reset(tmp_path):
    r = _recorder(tmp_path)
    _pose(r, 1.0, T0 + 200, 1000.0)
    _pose(r, 1.0, T0 + 200, 1000.0 + POSE_RESET_S + 5)                     # a parked robot repeating its pose
    assert len(r.poses_since(0)) == 1


def test_a_pose_without_its_own_time_does_not_reuse_the_last_one(tmp_path):
    r = _recorder(tmp_path)
    _pose(r, 1.0, T0 + 200, 1000.0)
    _pose(r, 2.0, None, 1001.0)                 # the merged snapshot still holds the old timestamp: not this pose's
    _pose(r, 3.0, None, 1002.0)
    kept = r.poses_since(0)
    assert [p["t"] for p in kept] == [T0 + 200, None, None]


def test_samples_without_robot_time_keep_arrival_order(tmp_path):
    r = _recorder(tmp_path)
    for i in range(3):
        _pose(r, float(i), None, 1000.0 + i)
    kept = r.poses_since(0)
    assert len(kept) == 3 and all(p["t"] is None for p in kept)


def test_status_position_carries_the_robot_time(tmp_path):
    r = _recorder(tmp_path)
    _pose(r, 1.0, T0 + 300, 1000.0)
    assert r.status()["robot"]["position"]["t"] == T0 + 300
