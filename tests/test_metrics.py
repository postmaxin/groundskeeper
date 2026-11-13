"""GET /api/metrics (#95, app/metrics.py): Prometheus text, off by default, no location, nothing asked of the robot
on a scrape, robot values dropped when stale, and reachable by a metrics-only token. Synthetic data only."""
import re
import socket
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, demo_robot as dr, main, mcp_server, metrics, telemetry
from app.store import Store
from tests.test_demo_robot import _recorder

# Made-up values that must never be printed: if one shows up in a scrape, a location field leaked.
LAT, LON, RTK_LAT, RTK_LON, X, Y, HGT = 38.123456, -76.654321, 38.222222, -76.333333, 123.25, -87.75, 279.375
SERIAL = "YB0000DEMO0001"
LOCATION_WORDS = {"lat", "lon", "lng", "latitude", "longitude", "hgt", "height", "altitude", "elevation", "position",
                  "x", "y", "serial", "sn", "id", "hash", "address", "location", "coordinates"}


def status(age=2.0, **over):
    base = {
        "sn": SERIAL, "state": "mowing", "activity": "mowing", "uptime_s": 1000, "snapshot_age_s": age,
        "heartbeat_age_s": 4.0, "stalled": False, "lost_contact": False,
        "run": {"id": 1, "started": 1.0, "distance_m": 5.0, "plan": None},
        "plan": {"active": True, "progress_pct": 40.0, "left_s": 900, "plan_id": 3, "session": f"3:{SERIAL}"},
        "transport": {"robot": {"connected": True, "heard": True, "last_message_age_s": 1.5, "received": 321,
                                "error": "", "via": "Yarbo cloud"}},
        "robot": {"position": {"lon": LON, "lat": LAT, "x": X, "y": Y, "heading": 90.0, "t": 1}},
    }
    return {**base, **over}


def telemetry_row():
    return {"x": X, "y": Y, "phi": 0.0, "lat": LAT, "lon": LON, "rtk_lat": RTK_LAT, "rtk_lon": RTK_LON, "hgt": HGT,
            "rtk_status": 4, "sats": 31, "heading_dop": 0.8, "odom_conf": 0.9, "battery": 82, "voltage_mv": 26500,
            "current_ma": -1500, "charging": 0, "working_state": 1, "error_code": 0, "speed_l": 0.5, "speed_r": 0.55,
            "wheel_cur_l": 1.4, "wheel_cur_r": 1.6, "odo_l": 1000.5, "odo_r": 1001.5, "halow": -60, "cpu_temp": 44.5,
            "cell_temp_max": 31.0, "cpu_usage": 43.8,
            "mem_available_kb": 5895624, "mem_free_kb": 748496, "mem_cached_kb": 4704332,
            "disk_available_b": 17685383168, "disk_used_b": 22105933824}


def render(st=None, t=None, cloud="connected", guards=None, last=None, now=1000.0):
    return metrics.render(st or status(), telemetry_row() if t is None else t, cloud, guards or [], last, now, "0.0.0")


def samples(text):
    """{'name{labels}': value} for every sample line."""
    return {m.group(1): float(m.group(2)) for line in text.splitlines()
            if (m := re.fullmatch(r"(\S+) (\S+)", line)) and not line.startswith("#")}


# -- the text ----------------------------------------------------------------------------------------------------
def test_robot_and_panel_values_come_out_in_prometheus_units():
    s = samples(render(guards=[{"key": "rain", "active": True}],
                       last={"cmd": "pause", "at": 990.0, "confirmed": True, "done": True}))
    assert s["groundskeeper_up"] == 1 and s["groundskeeper_cloud_connected"] == 1 and s["groundskeeper_run_active"] == 1
    assert s["groundskeeper_robot_messages_total"] == 321 and s["groundskeeper_route_connected"] == 1
    assert s['groundskeeper_guard_active{guard="rain"}'] == 1 and s['groundskeeper_guard_active{guard="person"}'] == 0
    assert s['groundskeeper_last_command_result{result="confirmed"}'] == 1
    assert s["groundskeeper_last_command_age_seconds"] == 10
    assert s["yarbo_battery_ratio"] == 0.82 and s["yarbo_battery_voltage_volts"] == 26.5
    assert s["yarbo_battery_current_amperes"] == -1.5 and s["yarbo_cpu_temperature_celsius"] == 44.5
    assert s['yarbo_rtk_fix{quality="rtk_fixed"}'] == 1 and s['yarbo_rtk_fix{quality="rtk_float"}'] == 0
    assert s['yarbo_wheel_speed_metres_per_second{side="right"}'] == 0.55
    assert s['yarbo_wheel_distance_metres_total{side="left"}'] == 1000.5 and s["yarbo_halow_signal_dbm"] == -60
    assert s["yarbo_plan_progress_ratio"] == 0.4 and s["yarbo_plan_time_left_seconds"] == 900
    assert s['yarbo_activity{state="mowing"}'] == 1 and s['yarbo_activity{state="docked"}'] == 0
    assert s["yarbo_heading_degrees"] == telemetry.heading_deg(0.0)       # heading is allowed; position is not


def test_the_robot_computer_comes_out_in_ratios_and_bytes():
    s = samples(render())
    assert s["yarbo_cpu_usage_ratio"] == pytest.approx(0.438)
    assert s["yarbo_memory_available_bytes"] == 5895624 * 1024 and s["yarbo_memory_free_bytes"] == 748496 * 1024
    assert s["yarbo_memory_cached_bytes"] == 4704332 * 1024
    assert s["yarbo_disk_available_bytes"] == 17685383168 and s["yarbo_disk_used_bytes"] == 22105933824


def test_the_robot_computer_is_left_out_when_missing_or_odd():
    odd = {**telemetry_row(), "cpu_usage": float("nan"), "mem_available_kb": "n/a", "mem_free_kb": None,
           "mem_cached_kb": True, "disk_available_b": float("inf")}
    s = samples(render(t=odd))
    gone = {"yarbo_cpu_usage_ratio", "yarbo_memory_available_bytes", "yarbo_memory_free_bytes",
            "yarbo_memory_cached_bytes", "yarbo_disk_available_bytes"}
    assert not gone & set(s) and s["yarbo_disk_used_bytes"] == 22105933824
    assert not [k for k in samples(render(t={})) if "cpu_usage" in k or "memory" in k or "disk" in k]


def test_the_robot_computer_goes_when_the_snapshot_is_old():
    names = ("cpu_usage_ratio", "memory_available_bytes", "memory_free_bytes", "memory_cached_bytes",
             "disk_available_bytes", "disk_used_bytes")
    assert all(f"yarbo_{n}" in samples(render()) for n in names)
    stale = samples(render(st=status(age=metrics.STALE_S + 1)))
    assert not [n for n in names if f"yarbo_{n}" in stale]


def test_the_text_is_well_formed():
    text = render()
    assert text.endswith("\n")
    kinds = dict(re.findall(r"^# TYPE (\S+) (\S+)$", text, re.M))
    for line in text.splitlines():
        if not line.startswith("#"):
            name = re.match(r"[a-z_]+", line).group(0)
            assert name in kinds, f"{name} has no TYPE line"
            assert re.fullmatch(r'[a-z_]+(\{[a-z_]+="[^"]+"(,[a-z_]+="[^"]+")*\})? -?[0-9.e+-]+', line), line
    assert all(n.startswith(("yarbo_", "groundskeeper_")) for n in kinds)
    assert all(n.endswith("_total") for n, k in kinds.items() if k == "counter")
    assert all(not n.endswith("_total") for n, k in kinds.items() if k == "gauge")


def test_a_missing_or_odd_value_is_left_out_not_a_crash():
    text = render(t={"battery": None, "voltage_mv": "n/a", "rtk_status": 99, "speed_l": float("nan"), "halow": True})
    s = samples(text)
    assert not {"yarbo_battery_ratio", "yarbo_battery_voltage_volts", "yarbo_halow_signal_dbm"} & set(s)
    assert not [k for k in s if k.startswith("yarbo_rtk_fix") and s[k]]       # an unknown fix is all zeros
    assert "nan" not in text.lower()


def test_cardinality_is_bounded_whatever_the_robot_says():
    row = {**telemetry_row(), "rtk_status": 77, "error_code": -99999}
    text = render(st=status(activity="something new"), t=row, guards=[{"key": "a new guard", "active": True}],
                  last={"cmd": "x", "at": 1.0, "done": True, "confirmed": None})
    n = len(samples(text))
    row2 = {**telemetry_row(), "rtk_status": 1, "error_code": 5}
    assert len(samples(render(t=row2, last={"cmd": "y", "at": 1.0, "done": False}))) <= n + 1
    assert 'result="unconfirmed"} 1' in text and "a new guard" not in text and "something new" not in text


# -- privacy -----------------------------------------------------------------------------------------------------
def test_no_location_value_or_name_is_ever_printed():
    text = render()
    for value in (LAT, LON, RTK_LAT, RTK_LON, X, Y, HGT, abs(X), abs(Y)):
        assert str(value) not in text and str(abs(value)) not in text, value
    assert SERIAL not in text and "YB0000" not in text
    words = {w for name in re.findall(r"^# TYPE (\S+) ", text, re.M) for w in name.split("_")}
    assert not words & LOCATION_WORDS, words & LOCATION_WORDS
    assert not re.search(r"(robot|serial|sn|device|id)=", text)          # no robot label of any kind
    assert set(re.findall(r"(\w+)=\"", text)) <= {"side", "state", "quality", "guard", "result", "version"}


def test_the_demo_robot_through_the_recorder_prints_no_position(tmp_path):
    r = _recorder(tmp_path)
    t0 = time.time()
    robot = dr.DemoRobot(t0, plan_id=2, progress=0.0, speed=2.0)
    tr = dr.DemoTransport(dr.DEMO_SN, r._handle, robot)
    tr.connected = True
    tr.tick(t0)
    tr.request("read_gps_ref")
    now = t0
    for _ in range(90):
        now += 1.0
        if int(now) % 30 == 0:
            tr.request("get_device_msg")
            tr.request("get_plan_feedback")
        tr.tick(now)
    assert r.snapshot and r.t.get("x") is not None and r.t.get("lat") is not None
    text = metrics.render(r.status(), r.t, "connected", [], None, time.time(), "0.0.0")
    s = samples(text)
    assert s["yarbo_data_fresh"] == 1 and 0 < s["yarbo_battery_ratio"] <= 1 and "yarbo_halow_signal_dbm" in s
    secrets_ = [r.t[k] for k in ("x", "y", "lat", "lon", "rtk_lat", "rtk_lon", "hgt") if isinstance(r.t.get(k), float)]
    assert secrets_
    for v in secrets_:
        assert f"{v:.2f}" not in text and str(v) not in text, v
    assert dr.DEMO_SN not in text
    assert 0 < s["yarbo_cpu_usage_ratio"] < 1 and s["yarbo_memory_available_bytes"] > 1e9
    assert s["yarbo_disk_used_bytes"] > 0 and "topProc" not in text and "demo_proc" not in text


# -- stale data --------------------------------------------------------------------------------------------------
def test_robot_values_go_when_the_snapshot_is_old_and_the_age_stays():
    fresh = samples(render(st=status(age=metrics.STALE_S)))
    stale = samples(render(st=status(age=metrics.STALE_S + 1)))
    assert fresh["yarbo_data_fresh"] == 1 and "yarbo_battery_ratio" in fresh
    assert stale["yarbo_data_fresh"] == 0 and stale["yarbo_snapshot_age_seconds"] == metrics.STALE_S + 1
    assert [k for k in stale if k.startswith("yarbo_")] == ["yarbo_snapshot_age_seconds", "yarbo_data_fresh"]
    assert stale["groundskeeper_up"] == 1 and stale["groundskeeper_cloud_connected"] == 1      # the panel still reports


def test_no_snapshot_yet_has_no_robot_values():
    s = samples(render(st=status(age=None)))
    assert s["yarbo_data_fresh"] == 0 and "yarbo_snapshot_age_seconds" not in s and "yarbo_battery_ratio" not in s


# -- the route ---------------------------------------------------------------------------------------------------
class NoAsk:
    """A stand-in that fails the test on any call the scrape should not make."""
    def __getattr__(self, name):
        raise AssertionError(f"a scrape must not touch .{name}")


class FakeRec:
    def __init__(self, store, age=2.0):
        self.store, self.age, self.t, self.status_calls = store, age, telemetry_row(), 0
        self.link, self.transport, self.ha, self.frigate = NoAsk(), NoAsk(), NoAsk(), NoAsk()

    def status(self):
        self.status_calls += 1
        return status(self.age)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("PANEL_AUTH", raising=False)
    store = Store(str(tmp_path))
    gate = auth.Auth(store)
    gate.announce()
    fake_ops = SimpleNamespace(cloud=SimpleNamespace(state="connected"), guard_status=lambda: [],
                               controller=SimpleNamespace(last=None), frigate=NoAsk(), notifier=NoAsk(),
                               scheduler=NoAsk(), link=NoAsk(), rec=NoAsk())
    old = {k: getattr(main.app.state, k, None) for k in ("auth", "rec", "ops")}
    main.app.state.auth, main.app.state.rec, main.app.state.ops = gate, FakeRec(store), fake_ops
    c = TestClient(main.app, base_url="http://panel.test")
    assert c.post("/api/auth/setup", json={"code": gate.setup_code, "password": "correct horse"}).status_code == 200
    yield SimpleNamespace(c=c, auth=gate, store=store, rec=main.app.state.rec)
    for k, v in old.items():
        setattr(main.app.state, k, v)


def bot(client, scope):
    token = client.c.post("/api/auth/tokens", json={"name": scope, "scope": scope}).json()["token"]
    return TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {token}"})


def test_it_is_off_by_default_for_everyone_and_answers_403(client):
    assert main.SETTINGS_DEFAULTS["metrics_enabled"] is False
    for who in (client.c, bot(client, "metrics"), bot(client, "full")):
        r = who.get("/api/metrics")
        assert r.status_code == 403 and "off" in r.json()["detail"], r.text
    assert client.rec.status_calls == 0                       # off means no work at all
    assert TestClient(main.app, base_url="http://panel.test").get("/api/metrics").status_code == 401


def test_a_metrics_token_reads_metrics_and_nothing_else(client):
    assert client.c.put("/api/settings/metrics_enabled", json=True).status_code == 200
    scrape = bot(client, "metrics")
    r = scrape.get("/api/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "yarbo_battery_ratio" in r.text and "groundskeeper_up 1" in r.text
    for path in ("/api/status", "/api/ha/summary", "/api/settings", "/api/events"):
        assert scrape.get(path).status_code == 403, path       # the gate of #102 still holds beside the new route
    assert scrape.post("/api/metrics").status_code == 403
    assert bot(client, "full").get("/api/metrics").status_code == 200
    assert client.c.get("/api/metrics").status_code == 200


def test_only_a_person_switches_it_and_it_is_not_on_mcp(client):
    scrape, full = bot(client, "metrics"), bot(client, "full")
    for who in (scrape, full):
        assert who.put("/api/settings/metrics_enabled", json=True).status_code == 403
    assert client.store.setting("metrics_enabled") is None
    assert client.c.put("/api/settings/metrics_enabled", json="yes").status_code == 400
    assert client.c.put("/api/settings/metrics_enabled", json=True).json() == {"metrics_enabled": True}
    assert client.store.setting("metrics_enabled") is True
    assert client.c.put("/api/settings/metrics_enabled", json=False).status_code == 200
    assert not mcp_server.allowed("GET", "/api/metrics")
    assert not mcp_server.allowed("PUT", "/api/settings/metrics_enabled")


def test_with_sign_in_off_the_switch_works_and_the_endpoint_follows_it(client, monkeypatch):
    monkeypatch.setenv("PANEL_AUTH", "off")                    # a proxy signs people in: every caller is a person
    anon = TestClient(main.app, base_url="http://panel.test")
    assert anon.get("/api/metrics").status_code == 403         # off by default, as with sign-in on
    assert anon.put("/api/settings/metrics_enabled", json=True).json() == {"metrics_enabled": True}
    assert anon.get("/api/metrics").status_code == 200         # /api/ is open with sign-in off, so this is too
    assert anon.put("/api/settings/metrics_enabled", json=False).status_code == 200
    assert anon.get("/api/metrics").status_code == 403


def test_a_scrape_asks_nobody_and_runs_no_start_check(client, monkeypatch):
    client.store.set_setting("metrics_enabled", True)

    real_connect = socket.socket.connect

    def refuse(sock, addr, *a, **k):          # the event loop's own socketpair is loopback; anything else is a leak
        if str(addr[0]) not in ("127.0.0.1", "::1"):
            raise AssertionError(f"a scrape must not open a connection ({addr[0]})")
        return real_connect(sock, addr, *a, **k)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    scrape = bot(client, "metrics")
    for _ in range(3):
        assert scrape.get("/api/metrics").status_code == 200
    # FakeRec and the fake Ops raise on link, transport, Home Assistant, Frigate, scheduler and controller.preview.
    assert not getattr(main.app.state, "ha_start_cache", None)


def test_a_stale_snapshot_over_the_route(client):
    client.store.set_setting("metrics_enabled", True)
    client.rec.age = 9999.0
    s = samples(bot(client, "metrics").get("/api/metrics").text)
    assert s["yarbo_data_fresh"] == 0 and "yarbo_battery_ratio" not in s
