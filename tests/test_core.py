import math

import pytest

from app import geo, telemetry
from app.codec import decode, encode
from app.runs import RunDetector, RunStats
from app.commands import COMPUTE_ONLY, READ_ONLY_COMMANDS, ReadOnlyViolation, check_read_only


# -- read-only guarantee -------------------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", ["start_plan", "stop", "pause", "resume", "cmd_recharge", "cmd_vel", "get_controller",
                                 "set_working_state", "light_ctrl", "smart_vision_control", "save_global_params",
                                 "song_cmd", "set_child_lock", ""])
def test_control_commands_are_refused(cmd):
    with pytest.raises(ReadOnlyViolation):
        check_read_only(cmd)


@pytest.mark.parametrize("cmd", sorted(READ_ONLY_COMMANDS))
def test_read_requests_are_allowed(cmd):
    check_read_only(cmd)
    assert cmd.startswith(("get_", "read_")) or cmd in COMPUTE_ONLY


def test_a_read_through_the_link_refuses_a_command_before_anything_is_published():
    from types import SimpleNamespace

    from app.link import RobotLink
    sent = []
    cloud = SimpleNamespace(state="connected", connected=True, publish=lambda cmd, payload: sent.append(cmd))
    with pytest.raises(ReadOnlyViolation):
        RobotLink(None, cloud, None).read("start_plan", {"id": 1})
    assert sent == []


# -- codec ---------------------------------------------------------------------------------------------------
def test_codec_round_trip_and_plain_json():
    assert decode(encode({"a": 1})) == {"a": 1}
    assert decode(b'{"working_state":0}') == {"working_state": 0}
    assert decode(b"\x00\x01garbage") is None


# -- geometry ------------------------------------------------------------------------------------------------
def test_local_frame_is_x_west_y_north():
    ref = (40.0, -100.0)
    lon_w, lat_w = geo.to_lonlat(ref, 10, 0)
    lon_n, lat_n = geo.to_lonlat(ref, 0, 10)
    assert lon_w < ref[1] and math.isclose(lat_w, ref[0])
    assert lat_n > ref[0] and math.isclose(lon_n, ref[1])
    # 10 m north is 10 m
    assert math.isclose((lat_n - ref[0]) * 111320, 10, rel_tol=1e-6)


def test_map_to_geojson_kinds_and_closure():
    raw = {
        "areas": [{"id": 11, "name": "All Yard", "range": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}],
                   "ref": {"latitude": 40.0, "longitude": -100.0},
                   "snowPiles": [{"range": [{"x": 1, "y": 1}, {"x": 2, "y": 1}, {"x": 2, "y": 2}]}]}],
        "nogozones": [{"id": 45, "name": "No-go", "range": [{"x": 1, "y": 1}, {"x": 2, "y": 1}, {"x": 2, "y": 2}]}],
        "pathways": [{"id": 52, "name": "Path", "range": [{"x": 0, "y": 0}, {"x": 5, "y": 5}]}],
        "allchargingData": [{"id": 1, "chargingPoint": {"x": 0.5, "y": 0.5, "phi": 0}}],
    }
    fc = geo.map_to_geojson(raw, gps_ref=(40.0, -100.0))
    kinds = sorted(f["properties"]["kind"] for f in fc["features"])
    assert kinds == ["areas", "charging", "nogozones", "pathways", "snowpiles"]
    ring = next(f for f in fc["features"] if f["properties"]["kind"] == "areas")["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 4
    assert geo.bounds(fc) is not None


# -- telemetry -----------------------------------------------------------------------------------------------
def test_extract_tolerates_missing_and_odd_values():
    t = telemetry.extract({"BatteryMSG": {"capacity": 95, "temperature1": 24, "temperature3": 25},
                           "system_info": {"cpu": {"Temperature": "44.384000 °C"}}})
    assert t["battery"] == 95
    assert t["x"] is None and t["rtk_status"] is None
    assert t["cell_temps"] == [24, 25] and t["cell_temp_max"] == 25
    assert t["cpu_temp"] == pytest.approx(44.384)


def test_labels():
    assert telemetry.head_label(5) == "Mower Pro"
    assert telemetry.head_label(1) == "Snow Blower"
    assert telemetry.rtk_label(4) == "RTK fixed"
    assert telemetry.rtk_label(None) == "unknown"


# -- run detection -------------------------------------------------------------------------------------------
def test_run_starts_on_movement_and_ends_after_idle():
    d = RunDetector(idle_end_s=300)
    assert d.update(0, {"speed_l": 0, "speed_r": 0}) is None
    assert d.update(10, {"speed_l": 0.4, "speed_r": 0.4}) == "start"
    assert d.update(20, {"speed_l": 0.4, "speed_r": 0.3}) is None
    assert d.update(200, {"speed_l": 0, "speed_r": 0}) is None
    assert d.update(321, {"speed_l": 0, "speed_r": 0}) == "end"


def test_run_ends_quickly_once_back_on_the_charger():
    d = RunDetector(idle_end_s=300)
    d.update(0, {"speed_l": 0.5})
    assert d.update(20, {"speed_l": 0, "charging": 2}) is None
    assert d.update(31, {"speed_l": 0, "charging": 2}) == "end"


def test_run_stats_ignore_pose_jumps():
    s = RunStats()
    s.add({"x": 0, "y": 0, "rtk_status": 4, "battery": 90})
    s.add({"x": 0.3, "y": 0.4, "rtk_status": 4, "battery": 89})
    s.add({"x": 100, "y": 100, "rtk_status": 5, "battery": 88})  # relocalisation jump
    assert s.distance_m == pytest.approx(0.5)
    assert s.fixed_pct == pytest.approx(66.7)
    assert (s.battery_start, s.battery_end) == (90, 88)


def test_raw_snapshots_are_compressed_and_readable(tmp_path):
    from app.store import Store
    st = Store(str(tmp_path))
    snap = {"BatteryMSG": {"capacity": 95, "temperature1": 24}, "padding": "x" * 5000}
    st.add_raw(100.0, snap)
    stored = st.db.execute("SELECT json FROM snapshot_raw").fetchone()[0]
    assert isinstance(stored, bytes) and len(stored) < 500
    assert st.raw(0, 200) == [(100.0, snap)]


@pytest.mark.parametrize("phi,bearing", [(0.0, 270.0), (math.pi / 2, 0.0), (math.pi, 90.0), (-math.pi / 2, 180.0),
                                         (math.pi / 4, 315.0)])
def test_phi_becomes_a_compass_heading(phi, bearing):
    # phi is the direction of travel in the robot's frame (x west, y north): +x is west, +y north
    assert telemetry.heading_deg(phi) == pytest.approx(bearing, abs=0.05)


def test_no_phi_no_heading():
    assert telemetry.heading_deg(None) is None
