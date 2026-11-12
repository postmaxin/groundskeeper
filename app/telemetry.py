"""Turn the robot's snapshot (get_device_msg / DeviceMSG) into flat, typed values.

Ingestion is tolerant by design: firmware updates rename and drop fields, so a missing or non-numeric value
becomes None and never an error.
"""
import math

FIELDS = {
    "x": "CombinedOdom.x",
    "y": "CombinedOdom.y",
    "phi": "CombinedOdom.phi",
    "odom_ts": "CombinedOdom.timestamp",             # the robot's time for this pose (ms; stored in s)
    "rtk_status": "RTKMSG.status",
    "sats": "RTKMSG.sat_num",
    "heading_dop": "RTKMSG.heading_dop",
    "rtk_lat": "RTKMSG.lan",
    "rtk_lon": "RTKMSG.lon",
    "rtcm_age": "rtcm_age",
    "battery": "BatteryMSG.capacity",
    "battery_health": "BatteryMSG.health",
    "voltage_mv": "BatteryMSG.voltage",
    "current_ma": "BatteryMSG.current",
    "charging": "StateMSG.charging_status",
    "working_state": "StateMSG.working_state",
    "planning": "StateMSG.on_going_planning",
    "recharging": "StateMSG.on_going_recharging",
    "planning_paused": "StateMSG.planning_paused",   # 0 none, 1 paused, 2 parked on the charger mid-plan
    "car_controller": "StateMSG.car_controller",     # True while the plan drives the wheels
    "odom_conf": "combined_odom_confidence",         # 0-1 position confidence; below 0.5 = dead reckoning (wiki: PPVS)
    "heading_obs": "RTKMSG.heading_obs",             # satellites seen by the heading antenna pair
    "heading_multi": "RTKMSG.heading_multi",         # ...shared by both antennas (probably the app's "L2" count)
    "hgt": "RTKMSG.hgt",                             # RTK height in metres (about 279 at the dock; cm-grade on a fix)
    "error_code": "StateMSG.error_code",
    "stuck": "StateMSG.stuck",
    "obstacle": "StateMSG.obstacle",
    "speed_l": "WheelSpeedMSG.left",
    "speed_r": "WheelSpeedMSG.right",
    "wheel_cur_l": "EletricMSG.lwheel_current",
    "wheel_cur_r": "EletricMSG.rwheel_current",
    "pitch": "RunningStatusMSG.gyro_pitch",
    "roll": "RunningStatusMSG.gyro_roll",
    "halow": "halow_status.strength",
    "rain": "RunningStatusMSG.rain_sensor_data",
    "head_type": "HeadMsg.head_type",
    "chute_angle": "RunningStatusMSG.chute_angle",
    "blade_rpm_l": "mower_head_info03.left_blade_motor_rpm",
    "blade_rpm_r": "mower_head_info04.right_blade_motor_rpm",
    "blade_cur_l": "mower_head_info03.left_blade_motor_current",
    "blade_cur_r": "mower_head_info04.right_blade_motor_current",
    "blade_temp_l": "mower_head_info03.left_blade_motor_temp",
    "blade_temp_r": "mower_head_info04.right_blade_motor_temp",
    "cpu_temp": "system_info.cpu.Temperature",
    "cpu_usage": "system_info.cpu.Usage",                      # percent, 0-100 for the whole CPU
    "mem_available_kb": "system_info.mem.MemAvailable",         # kB, as in /proc/meminfo
    "mem_free_kb": "system_info.mem.MemFree",
    "mem_cached_kb": "system_info.mem.Cached",
    "disk_available_b": "system_info.userdata.disk.availableSize",   # bytes, the robot's user-data partition
    "disk_used_b": "system_info.userdata.disk.usedSize",
    "odo_l": "WheelSpeedMSG.dist_left",
    "odo_r": "WheelSpeedMSG.dist_right",
}

# StateMSG code tables, from the official integration (YarboInc/YarboHA sensor.py) and yarbo-local codes.yaml
# (research 2026-10-06, docs/dev/robot-states.md). Negative values are errors.
PLAN_ERRORS = {
    -2: ("WP002", "Create plan history failed"), -10: ("WP003", "Plan not found"), -11: ("WP004", "Failed to read plan"),
    -12: ("WP005", "Failed to calculate route"), -20: ("WP006", "Outside mapped area"), -21: ("WP007", "Area data error"),
    -22: ("WP008", "Route data error"), -23: ("", "In a no-go zone"), -88: ("", "In a no-go zone"),
    -24: ("", "Low battery"), -26: ("WP012", "Module position failure"), -30: ("WP013", "Location data exception"),
    -31: ("WP014", "Docking station exception"), -40: ("", "Obstacle mark failed"), -42: ("", "Out of boundary"),
    -43: ("WP016", "Unable to navigate obstacle"), -44: ("WP017", "Exceeded boundary"),
    -47: ("", "Out of boundary by more than 1.5 m"), -92: ("WP025", "Out of boundary"),
}
RECHARGE_ERRORS = {
    -2: ("", "Server error"), -3: ("", "Direction uninitialized"), -4: ("", "Docking station uninitialized"),
    -5: ("REC005", "Recharge failed"), -6: ("", "Failed to park"), -8: ("", "Docking connection failed"),
    -9: ("", "Stuck"), -20: ("", "Outside mapped area"),
}
PAUSE_REASONS = {1: "paused", 2: "charging mid-plan", 3: "power restart", 4: "emergency stop", 5: "bumper",
                 6: "stuck", 7: "fault", 9: "rain"}


def code_label(table: dict, code) -> str | None:
    if not isinstance(code, (int, float)) or code >= 0:
        return None
    wp, text = table.get(int(code), ("", f"error {int(code)}"))
    return f"{wp} {text}".strip()


# Plan-status and recharge strings the official HA integration knows (its enum options), as a starting dictionary.
STATUS_DICTIONARY = {
    "WP002": ("Create plan history failed", "Retry; check the robot's storage"),
    "WP003": ("Plan not found", "Refresh plans and pick the plan again"),
    "WP004": ("Failed to read plan", "Retry; re-save the plan in the app"),
    "WP005": ("Failed to calculate route", "Check the area boundary and no-go zones"),
    "WP006": ("Outside mapped area", "Drive the robot back inside the map"),
    "WP007": ("Area data error", "Re-save the area"),
    "WP008": ("Route data error", "Retry; re-save the plan"),
    "WP012": ("Module position failure", "Check the head is seated"),
    "WP013": ("Location data exception", "Check RTK and the base station"),
    "WP014": ("Docking station exception", "Check the dock and its approach"),
    "WP016": ("Unable to navigate obstacle", "Clear the obstacle"),
    "WP017": ("Exceeded boundary", "Check the boundary near the last position"),
    "WP025": ("Out of boundary", "Drive the robot back inside"),
    "REC005": ("Recharge failed", "Check the dock approach path"),
}

FIRMWARE = {
    "body": "BodyVersionMsg.body_fw_version",
    "head": "HeadAndVersionCheck.head_firmware_version",
    "chassis": "chassis_version_msg.firmware_version",
    "data_center": "dc_version",
    "ble": "ble_version",
}

# HeadMsg.head_type values, in the order of the official HA integration's option list.
HEAD_TYPES = {0: "None", 1: "Snow Blower", 2: "Blower", 3: "Mower", 4: "Smart Cover", 5: "Mower Pro"}

# RTKMSG.status follows NMEA GGA fix quality.
RTK_LABELS = {0: "no fix", 1: "single point", 2: "DGPS", 4: "RTK fixed", 5: "RTK float", 6: "dead reckoning"}


_MISSING = object()


def _find(cur, parts):
    """Walk `parts` down nested dicts. A key may also be a flat dotted string: the robot's system_info sends
    "cpu.Usage" as one key, not {"cpu": {"Usage": ...}}, so every way of joining the next parts is tried."""
    if not parts:
        return cur
    if not isinstance(cur, dict):
        return _MISSING
    for i in range(1, len(parts) + 1):
        key = ".".join(parts[:i])
        if key in cur:
            found = _find(cur[key], parts[i:])
            if found is not _MISSING:
                return found
    return _MISSING


def get(d, path: str, default=None):
    found = _find(d, path.split("."))
    return default if found is _MISSING else found


def _number(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        # e.g. "44.384000 °C"
        try:
            return float(value.split()[0])
        except (ValueError, IndexError):
            return None
    return None


# A boolean is a flag for the state fields but never a reading for these, so it is left out, not turned into 1.
MEASURED = ("cpu_usage", "mem_available_kb", "mem_free_kb", "mem_cached_kb", "disk_available_b", "disk_used_b")


def extract(snapshot: dict) -> dict:
    out = {col: _number(get(snapshot, path)) for col, path in FIELDS.items()}
    for col in MEASURED:
        if isinstance(get(snapshot, FIELDS[col]), bool):
            out[col] = None
    if out.get("odom_ts") is not None and out["odom_ts"] > 1e11:    # milliseconds, as CombinedOdom sends it
        out["odom_ts"] = out["odom_ts"] / 1000
    temps = [_number(get(snapshot, f"BatteryMSG.temperature{i}")) for i in range(1, 7)]
    out["cell_temps"] = [t for t in temps if t is not None]
    out["cell_temp_max"] = max(out["cell_temps"]) if out["cell_temps"] else None
    return out


def firmware(snapshot: dict) -> dict:
    out = {}
    for name, path in FIRMWARE.items():
        value = get(snapshot, path)
        if isinstance(value, str) and value.strip():
            out[name] = value.strip()
    cams = get(snapshot, "camera_state") or {}
    versions = sorted({v for k, v in cams.items() if k.endswith("_version") and isinstance(v, str) and v})
    if versions:
        out["cameras"] = ", ".join(versions)
    return out


def head_label(head_type) -> str:
    if head_type is None:
        return "unknown"
    return HEAD_TYPES.get(int(head_type), f"type {int(head_type)}")


def heading_deg(phi) -> float | None:
    """The robot's heading as a compass bearing (degrees clockwise from north) from CombinedOdom.phi: radians, the
    direction of travel in the robot's x/y frame, where x points west and y north (checked on run 4, 2026-10-10:
    phi matched the driven direction over 8,020 samples, R 0.99, and x/lon correlate -1, y/lat +1)."""
    if phi is None:
        return None
    return round(math.degrees(math.atan2(-math.cos(phi), math.sin(phi))), 1) % 360.0      # round first: north is 0, not 360


def rtk_label(status) -> str:
    if status is None:
        return "unknown"
    return RTK_LABELS.get(int(status), f"status {int(status)}")


def deep_merge(base: dict, update: dict) -> dict:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base
