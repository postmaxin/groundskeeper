"""GET /api/metrics: the panel's state in the Prometheus text format (#95).

Built by hand (no prometheus_client) from what the panel already holds in memory: the recorder's status and its last
telemetry, the cloud session's state, the guards and the last command. A scrape asks nothing of the robot, Home
Assistant or Frigate, and never runs the start check the HA summary runs.

Privacy: every value is named here, one by one (an allowlist, not "everything but position"). No latitude, longitude,
RTK latitude, longitude or height, local x/y, serial or robot label; heading is the only pose value. The only labels
are small fixed sets (a state, a side, a guard), so the number of series never grows.

Stale data: the robot's values are left out once the snapshot is older than STALE_S, so a flat battery line cannot
look healthy. `yarbo_snapshot_age_seconds` and `yarbo_data_fresh` are always there to alert on.

Units: wheel speed in m/s, wheel current in A and wheel distance in m, as the rest of the panel reads them
(app/problems.py, app/main.py). The robot's memory comes in kB (as /proc/meminfo) and is shown in bytes; its CPU use
is a percent, shown as a ratio; its disk is in bytes. The robot's process list (`topProc`) is never read."""
import math

from . import telemetry
from .runs import ACTIVITY_LABELS

STALE_S = 300.0
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
# The fixed sets behind the one-hot gauges. A value outside its set reads as all zeros, never as a new series.
RTK_STATES = {0: "no_fix", 1: "single", 2: "dgps", 4: "rtk_fixed", 5: "rtk_float", 6: "dead_reckoning"}
GUARDS = ("person", "rain", "away", "dark")
COMMAND_RESULTS = ("pending", "confirmed", "unconfirmed")


def _num(value):
    """A finite number, or None. A bool is not a reading."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _fmt(value) -> str:
    return str(int(value)) if float(value).is_integer() and abs(value) < 1e15 else repr(float(value))


def _label(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _scaled(value, divisor):
    value = _num(value)
    return None if value is None else value / divisor


def _kb_to_bytes(value):
    value = _num(value)
    return None if value is None else value * 1024


class _Out:
    def __init__(self):
        self.lines: list[str] = []

    def add(self, name: str, kind: str, help_: str, samples: list[tuple[dict, object]]) -> None:
        """One metric family. A sample whose value is not a finite number is dropped; no samples, no family."""
        rows = [(labels, _num(v)) for labels, v in samples]
        rows = [(labels, v) for labels, v in rows if v is not None]
        if not rows:
            return
        self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
        for labels, v in rows:
            tag = "{" + ",".join(f'{k}="{_label(x)}"' for k, x in labels.items()) + "}" if labels else ""
            self.lines.append(f"{name}{tag} {_fmt(v)}")

    def gauge(self, name: str, help_: str, value, **labels) -> None:
        self.add(name, "gauge", help_, [(labels, value)])

    def one_hot(self, name: str, help_: str, label: str, current, values) -> None:
        self.add(name, "gauge", help_, [({label: v}, 1 if v == current else 0) for v in values])


def render(status: dict, t: dict, cloud_state: str | None, guards: list[dict], last_command: dict | None,
           now: float, version: str | None = None) -> str:
    """The scrape body. `status` is Recorder.status(), `t` the recorder's last telemetry (Recorder.t), `guards`
    Ops.guard_status(), `last_command` Controller.last."""
    out = _Out()
    link = (status.get("transport") or {}).get("robot") or {}

    # -- the panel's own state (always) -------------------------------------------------------------------------
    out.gauge("groundskeeper_up", "The panel is running.", 1)
    if version:
        out.gauge("groundskeeper_build_info", "The panel's version (always 1).", 1, version=version)
    out.gauge("groundskeeper_uptime_seconds", "Seconds since the panel started.", status.get("uptime_s"))
    out.gauge("groundskeeper_cloud_connected", "The Yarbo cloud session is connected (1) or not (0).",
              1 if cloud_state == "connected" else 0)
    out.gauge("groundskeeper_route_connected", "A route to the robot is up, through the Yarbo cloud (1) or not (0).",
              1 if link.get("connected") else 0)
    out.gauge("groundskeeper_robot_heard", "The robot itself has sent a message lately (1) or not (0).",
              1 if link.get("heard") else 0)
    out.gauge("groundskeeper_last_message_age_seconds", "Seconds since the robot's last message.",
              link.get("last_message_age_s"))
    out.add("groundskeeper_robot_messages_total", "counter",
            "Messages received from the robot since the panel started.", [({}, link.get("received"))])
    out.gauge("groundskeeper_run_active", "A run is in progress (1) or not (0).", 1 if status.get("run") else 0)
    out.gauge("groundskeeper_stalled", "The panel's own stuck check: the robot has stopped moving mid-run.",
              1 if status.get("stalled") else 0)
    out.gauge("groundskeeper_lost_contact", "A run is open and the robot has gone quiet.",
              1 if status.get("lost_contact") else 0)
    active = {g.get("key") for g in guards or [] if isinstance(g, dict) and g.get("active")}
    out.add("groundskeeper_guard_active", "gauge", "A guard has detected its condition (1) or not (0).",
            [({"guard": g}, 1 if g in active else 0) for g in GUARDS])
    if isinstance(last_command, dict):
        result = "confirmed" if last_command.get("confirmed") else "unconfirmed" if last_command.get("done") \
            else "pending"
        out.one_hot("groundskeeper_last_command_result", "The last command's outcome: pending, confirmed, or "
                    "unconfirmed (the robot's state did not change as expected).", "result", result, COMMAND_RESULTS)
        at = _num(last_command.get("at"))
        out.gauge("groundskeeper_last_command_age_seconds", "Seconds since the last command was sent.",
                  max(0.0, now - at) if at else None)

    # -- the robot (only while its snapshot is fresh) -----------------------------------------------------------
    age = _num(status.get("snapshot_age_s"))
    fresh = age is not None and age <= STALE_S
    out.gauge("yarbo_snapshot_age_seconds", "Seconds since the robot's last full state snapshot.", age)
    out.gauge("yarbo_data_fresh", "The robot's snapshot is recent enough for the other yarbo_* values to be shown.",
              1 if fresh else 0)
    if not fresh:
        return "\n".join(out.lines) + "\n"

    out.gauge("yarbo_heartbeat_age_seconds", "Seconds since the robot's last heartbeat.", status.get("heartbeat_age_s"))
    out.one_hot("yarbo_activity", "What the robot is doing (one of a fixed set).", "state", status.get("activity"),
                ACTIVITY_LABELS)
    out.gauge("yarbo_working_state", "The robot's own working_state code.", t.get("working_state"))
    out.gauge("yarbo_error_code", "The robot's error code (0 = none).", t.get("error_code"))
    charging = _num(t.get("charging"))
    out.gauge("yarbo_charging", "The robot is on the charger (1) or not (0).",
              None if charging is None else 1 if charging > 0 else 0)
    out.gauge("yarbo_battery_ratio", "Battery charge, 0 to 1.", _scaled(t.get("battery"), 100))
    out.gauge("yarbo_battery_voltage_volts", "Battery voltage.", _scaled(t.get("voltage_mv"), 1000))
    out.gauge("yarbo_battery_current_amperes", "Battery current, as the robot reports its sign.",
              _scaled(t.get("current_ma"), 1000))
    out.gauge("yarbo_battery_temperature_celsius", "Hottest battery cell.", t.get("cell_temp_max"))
    out.gauge("yarbo_cpu_temperature_celsius", "The robot's onboard CPU.", t.get("cpu_temp"))
    out.gauge("yarbo_cpu_usage_ratio", "The robot's onboard CPU use, 0 to 1, as it reports it.",
              _scaled(t.get("cpu_usage"), 100))
    out.gauge("yarbo_memory_available_bytes", "Memory the robot's computer can still use.",
              _kb_to_bytes(t.get("mem_available_kb")))
    out.gauge("yarbo_memory_free_bytes", "Memory the robot's computer has not used at all.",
              _kb_to_bytes(t.get("mem_free_kb")))
    out.gauge("yarbo_memory_cached_bytes", "Memory the robot's computer holds as cache.",
              _kb_to_bytes(t.get("mem_cached_kb")))
    out.gauge("yarbo_disk_available_bytes", "Free space on the robot's user-data disk.", t.get("disk_available_b"))
    out.gauge("yarbo_disk_used_bytes", "Used space on the robot's user-data disk.", t.get("disk_used_b"))
    rtk = _num(t.get("rtk_status"))
    out.one_hot("yarbo_rtk_fix", "RTK fix quality (one of a fixed set).", "quality",
                None if rtk is None else RTK_STATES.get(int(rtk)), RTK_STATES.values())
    out.gauge("yarbo_rtk_satellites", "Satellites used by the RTK receiver.", t.get("sats"))
    out.gauge("yarbo_rtk_heading_dop", "Dilution of precision of the heading antenna pair.", t.get("heading_dop"))
    out.gauge("yarbo_odometry_confidence_ratio", "Position confidence, 0 to 1 (below 0.5 the robot dead-reckons).",
              t.get("odom_conf"))
    out.gauge("yarbo_heading_degrees", "Compass heading, clockwise from north.",
              telemetry.heading_deg(_num(t.get("phi"))))
    out.add("yarbo_wheel_speed_metres_per_second", "gauge", "Wheel speed.",
            [({"side": "left"}, t.get("speed_l")), ({"side": "right"}, t.get("speed_r"))])
    out.add("yarbo_wheel_current_amperes", "gauge", "Wheel motor current.",
            [({"side": "left"}, t.get("wheel_cur_l")), ({"side": "right"}, t.get("wheel_cur_r"))])
    out.add("yarbo_wheel_distance_metres_total", "counter", "Distance the wheels have turned, lifetime.",
            [({"side": "left"}, t.get("odo_l")), ({"side": "right"}, t.get("odo_r"))])
    out.gauge("yarbo_halow_signal_dbm", "HaLow link strength.", t.get("halow"))
    plan = status.get("plan") if (status.get("plan") or {}).get("active") else None
    out.gauge("yarbo_plan_active", "A plan is in progress (1) or not (0).", 1 if plan else 0)
    if plan:      # the plan's own progress (done area over total area), not a coverage worked out per scrape
        out.gauge("yarbo_plan_progress_ratio", "How much of the plan's area is done, 0 to 1.",
                  _scaled(plan.get("progress_pct"), 100))
        out.gauge("yarbo_plan_time_left_seconds", "The robot's estimate of the time left in the plan.",
                  plan.get("left_s"))
    return "\n".join(out.lines) + "\n"
