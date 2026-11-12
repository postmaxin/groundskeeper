"""DEMO=1 (app/demo_robot.py, app/demo.py): the made-up robot answers reads only, its replies parse through the
recorder, a short mow records a run inside the lawns and outside the no-go zones, and demo mode never builds a
CloudTransport (so it can never reach a real robot or the Yarbo cloud)."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app import config, demo, demo_robot as dr, geo
from app import recorder as recmod
from app.config import Config
from app.recorder import Recorder
from app.runtime import Runtime
from app.store import Store
from app.commands import READ_ONLY_COMMANDS, ReadOnlyViolation


def _cfg(tmp_path, **over) -> Config:
    base = dict(data_dir=str(tmp_path), port=8425, poll_idle_s=60,
                poll_active_s=2, map_refresh_s=21600, raw_every_s=60, raw_keep_days=90, run_idle_end_s=300,
                tempest_port=0, ha_url=None, ha_token=None, demo=True, frigate_url=None, panel_url=None)
    return Config(**{**base, **over})


def _recorder(tmp_path, **over) -> Recorder:
    async def no_imagery(*a, **k):
        return None
    imagery = SimpleNamespace(yard_meta=None, ensure_yard=no_imagery)       # never fetched in a test
    return Recorder(_cfg(tmp_path, **over), Store(str(tmp_path)), imagery, None)


# -- read-only ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", ["start_plan", "pause", "resume", "stop", "cmd_recharge", "cmd_vel", "save_plan",
                                 "save_clean_area", "del_nogozone", "set_working_state", "get_controller"])
def test_the_demo_robot_refuses_anything_but_a_read(cmd):
    t = dr.DemoTransport(dr.DEMO_SN, lambda *a: None, dr.DemoRobot(time.time(), plan_id=0))
    t.connected = True
    with pytest.raises(ReadOnlyViolation):
        t.request(cmd, {})
    assert t.published == 0 and t._requests.empty()


def test_every_read_the_panel_makes_is_answered():
    t = dr.DemoTransport(dr.DEMO_SN, lambda *a: None, dr.DemoRobot(time.time()))
    now = time.time()
    for cmd in READ_ONLY_COMMANDS - {"read_deadend_params"}:      # the demo yard has no dead ends
        state, msg, data = t.answer(cmd, {"id": {"read_mower_area_params": 11, "read_mower_pathway_params": 41,
                                                 "get_map_buckup_from_id": 7}.get(cmd, 0),
                                          "range": dr.area_record(11)["range"]}, now)
        assert state == 0, (cmd, msg)
        assert data is not None, cmd


# -- through the recorder ----------------------------------------------------------------------------------------
def test_replies_parse_through_the_recorder(tmp_path):
    r = _recorder(tmp_path)

    async def go():
        r.loop = asyncio.get_running_loop()
        robot = dr.DemoRobot(time.time(), plan_id=1, progress=0.3)
        r.transport = dr.DemoTransport(dr.DEMO_SN, r._from_thread, robot)
        r.transport.connected = True
        consume = asyncio.create_task(r._consume())

        async def ticking():
            while True:
                r.transport.tick(time.time())
                await asyncio.sleep(0.01)
        ticker = asyncio.create_task(ticking())
        try:
            await r.refresh_map()
            records = await r.read_area_records()
            params = await r.read_area_params(12)
            pathway = await r.read_pathway_params(41)
            backups = await r.refresh_backups()
            backup = await r.fetch_backup(backups[0]["id"])
            preview = await r.preview_area_path({"id": 11, "range": dr.area_record(11)["range"], "ref": {}})
            await r.request("get_device_msg")
            await r.request("get_plan_feedback")
            await asyncio.sleep(0.05)
        finally:
            ticker.cancel()
            consume.cancel()
        return records, params, pathway, backups, backup, preview

    records, params, pathway, backups, backup, preview = asyncio.run(go())
    assert r.gps_ref == dr.REF
    kinds = sorted(f["properties"]["kind"] for f in r.map_geojson["features"])
    assert kinds.count("areas") == 3 and kinds.count("nogozones") == 2 and "charging" in kinds and "pathways" in kinds
    assert [p["name"] for p in r.plans] == ["Mow All", "Front lawn"]
    assert set(r.cfd["area_low_cfd_configs"]) == {"11", "12", "13"}
    assert set(records) == {11, 12, 13} and params["id"] == 12 and pathway["id"] == 41
    assert {b["name"] for b in backups} == {"Auto Saved", "Manually Saved 1"}
    assert len(backup["areas"]) == 3 and len(preview["path"]) > 50
    assert r.snapshot and r.t["head_type"] == 5 and r.firmware.get("body")
    assert r.plan is not None and r.plan.active and r.plan.session.startswith("1:") and 25 < r.plan.progress_pct < 40
    assert r.t["lon"] == pytest.approx(geo.to_lonlat(dr.REF, r.t["x"], r.t["y"])[0])


def test_a_short_mow_records_a_run_inside_the_lawns(tmp_path):
    """Front lawn from the dock and back, at twice the speed, fed straight into the recorder."""
    r = _recorder(tmp_path)
    t0 = 1_800_000_000.0
    robot = dr.DemoRobot(t0, plan_id=2, progress=0.0, speed=2.0)
    t = dr.DemoTransport(dr.DEMO_SN, r._handle, robot)
    t.connected = True
    t.tick(t0)
    t.request("read_gps_ref")
    t.tick(t0)
    now = t0
    while now < t0 + robot.route.total / dr.SPEED / 2 + 120:
        now += 1.0
        if int(now) % 60 == 0:
            t.request("get_plan_feedback")
        t.tick(now)
    runs = r.store.runs()
    assert len(runs) == 1
    run = runs[0]
    assert run["ended"] and run["end_reason"] == "plan finished" and run["plan"] == "Front lawn"
    assert run["plan_session"].startswith("2:") and run["plan_progress_pct"] >= 90
    rows = r.store.run_points(run["id"])
    assert len(rows) > 500
    lawn = next(a[2] for a in dr.AREAS if a[0] == 11)
    margin = dr.offset(lawn, -0.05)
    mowing = [p for p in rows if p["planning"] == 1]
    assert len(mowing) > 0.8 * len(rows)
    assert all(dr.inside(margin, p["x"], p["y"]) for p in mowing)
    for _, _, _, zone in dr.NOGOS:
        assert not any(dr.inside(zone, p["x"], p["y"]) for p in rows)
    assert 0.9 < run["distance_m"] / robot.route.total < 1.1
    assert run["battery_end"] < run["battery_start"]


def test_the_seeded_history(tmp_path):
    st = Store(str(tmp_path))
    sim = demo.prepare(st, now=1_800_000_000.0)
    assert demo.prepare(st) == sim                                      # once per DATA_DIR
    runs = st.runs()
    assert [r["end_reason"] for r in runs] == ["recorder restarted", "plan finished", "plan finished", "plan finished"]
    current = runs[0]
    assert current["plan_session"] == f"1:{sim['session_start']}" and current["plan_progress_pct"] == 45.0
    polys = {a[0]: a[2] for a in dr.AREAS}
    for run in runs:
        rows = st.run_points(run["id"])
        assert rows and all(not any(dr.inside(z, p["x"], p["y"]) for _, _, _, z in dr.NOGOS) for p in rows)
        mowing = [p for p in rows if p["planning"] == 1]
        assert all(any(dr.inside(dr.offset(poly, -0.05), p["x"], p["y"]) for poly in polys.values()) for p in mowing)
    assert st.setting("imagery_source")["provider"] == "or_portland_2025"
    assert len(st.setting("schedule_windows")) == 2 and st.weather_since(0)


def test_the_recorder_carries_on_the_seeded_mow(tmp_path):
    r = _recorder(tmp_path)
    demo.prepare(r.store)
    asyncio.run(r.apply_robot())
    try:
        t = r.transport
        now = time.time()
        r.transport._on_message = r._handle                              # straight in, no event loop needed
        for i in range(5):
            t.tick(now + i)
        t.request("get_plan_feedback")
        t.tick(now + 6)
        runs = r.store.runs()
        assert len(runs) == 4 and runs[0]["ended"] is None and r.run_id == runs[0]["id"]   # joined, not a new run
    finally:
        r.transport.stop()


# -- never the real robot ----------------------------------------------------------------------------------------
def test_demo_mode_never_builds_a_cloud_transport(tmp_path, monkeypatch):
    from app import ops as ops_mod

    def boom(*a, **k):
        raise AssertionError("DEMO=1 must never contact a robot or the Yarbo cloud")
    monkeypatch.setattr(ops_mod, "CloudTransport", boom)
    r = _recorder(tmp_path)
    o = ops_mod.Ops(r.cfg, r)
    status = asyncio.run(r.apply_robot())
    try:
        assert isinstance(o.cloud, dr.DemoCloud) and isinstance(r.transport, dr.DemoTransport)
        assert status["connected"] and r.sn == dr.DEMO_SN
        assert o.link.command_route() is None                             # the demo robot takes no commands
    finally:
        r.transport.stop()


@pytest.mark.parametrize("cmd", ["start_plan", "pause", "cmd_vel", "save_plan", "set_working_state"])
def test_the_demo_cloud_passes_on_reads_only(cmd):
    t = dr.DemoTransport(dr.DEMO_SN, lambda *a: None, dr.DemoRobot(time.time(), plan_id=0))
    t.connected = True
    cloud = dr.DemoCloud(SimpleNamespace(transport=t))
    with pytest.raises(ReadOnlyViolation):
        cloud.publish(cmd, {})
    cloud.publish("get_device_msg", {})
    assert cloud.published == 1 and cloud.state == "connected"


def test_demo_never_gets_the_robot_service_address(monkeypatch):
    monkeypatch.setenv("YARBO_ROBOT_SERVICE_URL", "https://robot-service.invalid")
    monkeypatch.setenv("DEMO", "1")
    assert config.load().robot_service_url is None
    monkeypatch.delenv("DEMO")
    monkeypatch.delenv("DEMO_SEED", raising=False)
    assert config.load().robot_service_url == "https://robot-service.invalid"     # the control: it is read outside demo


def test_the_robot_service_address_is_built_in_and_the_environment_replaces_it(monkeypatch):
    from app import video
    for name in ("DEMO", "DEMO_SEED", "YARBO_ROBOT_SERVICE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert video.DEFAULT_SERVICE_URL.startswith("https://") and not video.DEFAULT_SERVICE_URL.endswith("/")
    assert config.load().robot_service_url == video.DEFAULT_SERVICE_URL
    monkeypatch.setenv("YARBO_ROBOT_SERVICE_URL", "  ")                 # blank is unset, not "off"
    assert config.load().robot_service_url == video.DEFAULT_SERVICE_URL
    monkeypatch.setenv("YARBO_ROBOT_SERVICE_URL", "https://robot-service.invalid")
    assert config.load().robot_service_url == "https://robot-service.invalid"
    monkeypatch.setenv("DEMO", "1")
    assert config.load().robot_service_url is None


def test_demo_config_drops_the_real_robot_and_integrations(tmp_path, monkeypatch):
    for k, v in {"DEMO": "1", "YARBO_SN": "YB0000DEMO0009", "HA_URL": "http://ha.local",
                 "HA_TOKEN": "x", "FRIGATE_URL": "http://frigate.local", "TEMPEST_PORT": "50222"}.items():
        monkeypatch.setenv(k, v)
    c = config.load()
    assert c.demo and c.ha_url is None and c.frigate_url is None
    assert c.tempest_port == 0
    monkeypatch.delenv("DEMO")
    monkeypatch.setenv("DEMO_SEED", "1")                                  # the older name
    assert config.load().demo
    st = Store(str(tmp_path))
    st.set_setting("integrations", {"robot": {"sn": "YB0000DEMO0009"}, "home_assistant": {"url": "http://ha.local"}})
    rt = Runtime(c, st)
    from app.runtime import found_sn
    assert found_sn(st) == {} and rt.ha_url is None                   # a demo never keeps a serial
    with pytest.raises(ValueError):
        rt.set("home_assistant", {"url": "http://ha.local"})


def test_demo_refuses_a_data_dir_with_a_robots_data(tmp_path):
    st = Store(str(tmp_path))
    st.add_telemetry({"ts": 1.0, "x": 1.0, "y": 2.0})
    with pytest.raises(RuntimeError):
        demo.prepare(st)
    assert not st.runs() and st.setting(demo.SEEDED) is None
