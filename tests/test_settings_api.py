"""The settings endpoints behind the Settings view, and the integrations layer (Settings over the environment):
what GET returns, what PUT refuses, what is applied live, and that no secret ever leaves the server.
Driven through a fake request with a real Store; no robot, no network."""
import asyncio
import re
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import control, main
from app.conditions import ConditionsConfig
from app.config import Config
from app.runtime import Runtime
from app.store import Store


class FakeRecorder:
    def __init__(self, path):
        self.store = Store(str(path))
        self.started_at = time.time() - 90
        self.evaluated = 0
        self.applied = []
        self.ha = SimpleNamespace(configure=self._ha)
        self.tempest = None
        self.detector = SimpleNamespace(idle_end_s=300)

    def conditions_config(self):
        return ConditionsConfig.from_dict(self.store.setting("conditions"))

    def evaluate_conditions(self):
        self.evaluated += 1

    async def _ha(self, url, token):
        self.applied.append(("ha", url, token))

    async def apply_robot(self):
        self.applied.append(("robot",))
        return {}

    async def apply_weather(self):
        self.applied.append(("weather",))

    def apply_panel(self):
        self.applied.append(("panel",))


class FakeOps:
    def __init__(self, rec, cfg):
        self.rec, self.cfg, self.applied = rec, cfg, []

    async def apply_frigate(self):
        self.applied.append(("frigate", self.cfg.frigate_url))

    async def apply_home_assistant(self):
        await self.rec.ha.configure(self.cfg.ha_url, self.cfg.ha_token)


def env(tmp_path, **over):
    cfg = Config(yarbo_host="192.0.2.10", yarbo_port=1883, yarbo_sn=None, data_dir=str(tmp_path), port=8425, poll_idle_s=60,
                 poll_active_s=2, map_refresh_s=21600, raw_every_s=60, raw_keep_days=90, run_idle_end_s=300, tempest_port=50222,
                 ha_url=None, ha_token=None, demo=False, frigate_url=None, panel_url=None)
    return Config(**{**cfg.__dict__, **over})


def fake_request(tmp_path, **cfg_over):
    r = FakeRecorder(tmp_path)
    cfg = Runtime(env(tmp_path, **cfg_over), r.store)
    o = FakeOps(r, cfg)
    app = SimpleNamespace(state=SimpleNamespace(rec=r, cfg=cfg, store=r.store, ops=o))
    return SimpleNamespace(app=app), r, o


def run(coro):
    return asyncio.run(coro)


# -- the layer ---------------------------------------------------------------------------------------------------
def test_runtime_reads_settings_over_the_environment(tmp_path):
    store = Store(str(tmp_path))
    rt = Runtime(env(tmp_path, frigate_url="http://env-frigate:5000"), store)
    assert rt.frigate_url == "http://env-frigate:5000" and rt.yarbo_host == "192.0.2.10" and rt.data_dir == str(tmp_path)
    assert rt.set("frigate", {"url": "http://other:5000/"}) == ["url"]
    assert rt.frigate_url == "http://other:5000"                     # trailing slash dropped
    assert rt.set("frigate", {"url": "http://other:5000"}) == []     # same value: nothing changed
    assert rt.set("frigate", {"url": None}) == ["url"]
    assert rt.frigate_url == "http://env-frigate:5000"               # cleared: the environment applies again
    assert store.setting("integrations") == {}
    rt.set("robot", {"host": "robot.local", "port": 1884, "sn": "YB0000DEMO0001"})
    again = Runtime(env(tmp_path), store)                             # survives a restart
    assert (again.yarbo_host, again.yarbo_port, again.yarbo_sn) == ("robot.local", 1884, "YB0000DEMO0001")


@pytest.mark.parametrize("group,bad", [
    ("robot", {"host": ""}), ("robot", {"host": "http://robot"}), ("robot", {"port": 70000}), ("robot", {"port": "1883"}),
    ("robot", {"sn": "x"}), ("frigate", {"url": "frigate:5000"}), ("home_assistant", {"token": ""}),
    ("weather", {"tempest_port": 1.5}), ("panel", {"poll_idle_s": 1}), ("panel", {"raw_keep_days": True}),
    ("panel", {"nonsense": 1}),
])
def test_runtime_refuses_bad_values(tmp_path, group, bad):
    rt = Runtime(env(tmp_path), Store(str(tmp_path)))
    with pytest.raises(ValueError):
        rt.set(group, bad)
    assert rt.store.setting("integrations") is None


def test_describe_never_returns_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HA_URL", "http://ha:8123")
    monkeypatch.setenv("HA_TOKEN", "env-secret")
    rt = Runtime(env(tmp_path, ha_url="http://ha:8123", ha_token="env-secret"), Store(str(tmp_path)))
    d = rt.describe()
    assert d["home_assistant"]["token"] == {"source": "env", "env_name": "HA_TOKEN", "set": True, "env_set": True}
    assert d["home_assistant"]["url"] == {"source": "env", "env_name": "HA_URL", "value": "http://ha:8123", "env": "http://ha:8123"}
    assert d["weather"]["tempest_port"]["source"] == "default"          # a Config default, no variable behind it
    rt.set("home_assistant", {"token": "user-secret"})
    d = rt.describe()
    assert d["home_assistant"]["token"]["source"] == "setting" and rt.ha_token == "user-secret"
    assert "env-secret" not in str(d) and "user-secret" not in str(d)
    assert d["frigate"]["url"] == {"source": "none", "env_name": "FRIGATE_URL", "value": None, "env": None}


def test_false_switches_an_integration_off_over_the_environment(tmp_path):
    rt = Runtime(env(tmp_path, frigate_url="http://env-frigate:5000", ha_token="env-secret"), Store(str(tmp_path)))
    assert rt.set("frigate", {"url": False}) == ["url"]
    assert rt.frigate_url is False and rt.describe()["frigate"]["url"]["value"] is False
    assert rt.set("home_assistant", {"token": False}) == ["token"]
    assert rt.ha_token is False and rt.describe()["home_assistant"]["token"]["set"] is False
    with pytest.raises(ValueError):
        rt.set("panel", {"poll_idle_s": False})                         # not something you switch off
    assert rt.set("frigate", {"url": None}) == ["url"] and rt.frigate_url == "http://env-frigate:5000"


def test_stored_rows_are_checked_on_load_and_restore_puts_a_group_back(tmp_path):
    store = Store(str(tmp_path))
    store.set_setting("integrations", {"robot": {"port": "1883", "host": "robot.local", "nonsense": 1},
                                       "frigate": "junk", "unknown": {"x": 1}})
    rt = Runtime(env(tmp_path), store)
    assert rt.yarbo_port == 1883 and rt.yarbo_host == "robot.local"      # the bad port and the junk were dropped
    assert rt.describe()["robot"]["port"]["source"] == "default"
    before = rt.snapshot("robot")
    rt.set("robot", {"host": "other.local", "sn": "YB0000DEMO0002"})
    rt.restore("robot", before)
    assert rt.yarbo_host == "robot.local" and rt.yarbo_sn is None
    assert Runtime(env(tmp_path), store).yarbo_host == "robot.local"


# -- the API -----------------------------------------------------------------------------------------------------
def test_settings_lists_switches_groups_and_integrations(tmp_path):
    req, r, _ = fake_request(tmp_path)
    out = run(main.settings(req))
    assert out["control_enabled"] is False and out["map_write_enabled"] is False
    assert set(out["conditions"]) == set(ConditionsConfig.__dataclass_fields__)
    assert out["gates"]["min_battery_pct"] == 30
    assert set(out["integrations"]) == {"robot", "frigate", "home_assistant", "weather", "panel"}
    run(main.put_setting(req, "map_write_enabled", True))
    assert run(main.settings(req))["map_write_enabled"] is True


def test_put_integration_applies_live_and_logs_names_only(tmp_path):
    req, r, o = fake_request(tmp_path)
    out = run(main.put_integration(req, "frigate", {"url": "http://frigate:5000"}))
    assert out["changed"] == ["url"] and out["fields"]["url"]["source"] == "setting"
    assert o.applied == [("frigate", "http://frigate:5000")]
    run(main.put_integration(req, "home_assistant", {"url": "http://ha:8123", "token": "s3cret"}))
    assert r.applied[-1] == ("ha", "http://ha:8123", "s3cret")
    run(main.put_integration(req, "robot", {"host": "robot.local"}))
    run(main.put_integration(req, "weather", {"tempest_port": 0}))
    run(main.put_integration(req, "panel", {"run_idle_end_s": 120}))
    assert [a[0] for a in r.applied] == ["ha", "robot", "weather", "panel"]
    events = " ".join(e["detail"] for e in r.store.events() if e["kind"] == "settings_changed")
    assert "home_assistant: url, token" in events and "s3cret" not in events
    assert run(main.put_integration(req, "frigate", {"url": "http://frigate:5000"}))["changed"] == []   # no re-apply
    with pytest.raises(HTTPException) as e:
        run(main.put_integration(req, "frigate", {"url": "nope"}))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        run(main.put_integration(req, "nothing", {}))
    assert e.value.status_code == 404


def test_a_change_that_cannot_be_applied_is_not_kept(tmp_path):
    req, r, o = fake_request(tmp_path)

    async def boom():
        raise RuntimeError("socket busy")
    r.apply_weather = boom
    with pytest.raises(HTTPException) as e:
        run(main.put_integration(req, "weather", {"tempest_port": 60000}))
    assert e.value.status_code == 502 and "socket busy" in e.value.detail
    assert req.app.state.cfg.tempest_port == 50222 and r.store.setting("integrations") == {}


def test_the_saved_token_only_goes_to_the_saved_address(tmp_path):
    req, r, o = fake_request(tmp_path, ha_url="http://ha:8123", ha_token="secret")
    with pytest.raises(HTTPException) as e:
        run(main.test_integration(req, "home_assistant", {"url": "http://elsewhere:8123"}))
    assert e.value.status_code == 400 and "secret" not in e.value.detail


def test_condition_thresholds_are_validated(tmp_path):
    req, r, _ = fake_request(tmp_path)
    out = run(main.put_setting(req, "conditions", {"lookback_h": 12, "raining_window_s": 300}))
    assert out["lookback_h"] == 12 and out["raining_window_s"] == 300 and out["tau_h"] == 2.0    # the rest kept
    assert r.evaluated == 1
    for bad in ({"lookback_h": "12"}, {"lookback_h": 0}, {"tau_h": True}, {"wet_mm": True}, {"wet_mm": float("nan")},
                {"nonsense": 1}, {"damp_mm": 5, "wet_mm": 1}, {"robot_rain_wet_below": "wet"}):
        with pytest.raises(HTTPException) as e:
            run(main.put_setting(req, "conditions", bad))
        assert e.value.status_code == 400, bad
    assert run(main.settings(req))["conditions"]["lookback_h"] == 12                             # nothing bad stuck
    out = run(main.put_setting(req, "conditions", {"robot_rain_wet_below": 1200}))
    assert out["robot_rain_wet_below"] == 1200
    assert run(main.put_setting(req, "conditions", {"robot_rain_wet_below": None}))["robot_rain_wet_below"] is None


def test_about_has_no_secret_and_tests_need_an_address(tmp_path):
    req, r, _ = fake_request(tmp_path, ha_url="http://ha.local:8123", ha_token="secret-token")
    a = run(main.about(req))
    assert "secret-token" not in str(a) and a["uptime_s"] >= 90 and set(a["stats"]) >= {"telemetry_rows", "runs"}
    with pytest.raises(HTTPException) as e:
        run(main.test_integration(req, "frigate", {}))
    assert e.value.status_code == 400
    assert run(main.test_integration(req, "weather", {})) == {"ok": False, "detail": "listener off"}


def test_ha_sensors_lists_candidates_from_the_saved_connection(tmp_path):
    req, r, _ = fake_request(tmp_path)
    r.ha = SimpleNamespace(enabled=False)
    with pytest.raises(HTTPException) as e:
        run(main.ha_sensors(req))
    assert e.value.status_code == 409
    seen = []

    class Client:
        async def get(self, url):
            seen.append(url)
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: [
                {"entity_id": "sensor.outdoor_temperature", "state": "61", "attributes": {"device_class": "temperature", "unit_of_measurement": "°F"}}])
    r.ha = SimpleNamespace(enabled=True, url="http://ha:8123", _client=Client())
    out = run(main.ha_sensors(req))
    assert seen == ["http://ha:8123/api/states"] and out["temp"][0]["entity"] == "sensor.outdoor_temperature"


def test_ha_notify_services_lists_phones_first(tmp_path):
    req, r, _ = fake_request(tmp_path)
    r.ha = SimpleNamespace(enabled=False)
    with pytest.raises(HTTPException) as e:
        run(main.ha_notify_services(req))
    assert e.value.status_code == 409
    seen = []
    services = [
        {"domain": "light", "services": {"turn_on": {}}},
        {"domain": "notify", "services": {
            "notify": {"name": "Send a notification with notify"},
            "persistent_notification": {"name": "Send a persistent notification"},
            "send_message": {"name": "Send a notification message"},
            "mobile_app_my_phone": {"name": "Send a notification via mobile_app_my_phone"},
            "alexa_media": {"name": "Alexa Media"},
            "Bad-Name": {}}}]

    class Client:
        async def get(self, url):
            seen.append(url)
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: services)
    r.ha = SimpleNamespace(enabled=True, url="http://ha:8123", _client=Client())
    out = run(main.ha_notify_services(req))["services"]
    assert seen == ["http://ha:8123/api/services"]
    assert [c["service"] for c in out] == ["notify.mobile_app_my_phone", "notify.alexa_media", "notify.notify",
                                           "notify.persistent_notification"]
    assert out[0]["likely"] and out[0]["name"] == "My Phone" and not out[1]["likely"]
    assert out[1]["name"] == "Alexa Media" and out[2]["name"] == "Every notifier"
    for c in out:   # every choice passes the setting's own check
        assert re.fullmatch(r"notify\.[a-z0-9_]+", c["service"])


def test_setup_status_reads_what_the_panel_knows(tmp_path):
    req, r, o = fake_request(tmp_path, yarbo_host=None)
    r.local_status = lambda: {"connected": False}
    r.map_geojson, r.tempest, r.yard = None, None, None
    r.ha = SimpleNamespace(enabled=False, error="")
    o.cloud = SimpleNamespace(status=lambda: {"state": "signed out"})
    o.frigate = SimpleNamespace(enabled=False)
    o.notifier = SimpleNamespace(channels=lambda: {"ha": False, "webhook": False})
    o.controller = SimpleNamespace(enabled=lambda: False)
    out = run(main.setup_status(req))
    steps = {s["id"]: s for s in out["steps"]}
    assert out["first_run"] and not out["required_done"]
    assert steps["robot"]["detail"] == "no address yet" and not steps["robot"]["done"]
    assert steps["safety"]["done"] and steps["cloud"]["optional"] and not steps["weather"]["done"]
    r.local_status = lambda: {"connected": True}
    r.map_geojson = {"type": "FeatureCollection", "features": []}
    r.store.set_setting("integrations", {"robot": {"host": "192.0.2.10"}})
    req.app.state.cfg = Runtime(env(tmp_path), r.store)
    r.store.set_setting("weather_sources", {"ha": {"temp": "sensor.out_temp"}})
    r.store.set_setting("gates", {"guard_resume": True})
    out = run(main.setup_status(req))
    steps = {s["id"]: s for s in out["steps"]}
    assert not out["first_run"] and steps["robot"]["done"] and steps["weather"]["done"] and not steps["safety"]["done"]


def test_a_change_to_the_run_rules_is_logged(tmp_path):
    req, r, _ = fake_request(tmp_path)
    run(main.put_setting(req, "gates", {"guard_resume": True, "guard_person": "notify"}))
    ev = [e for e in r.store.events(10) if e["kind"] == "gates_changed"]
    assert len(ev) == 1 and "guard_resume off → on" in ev[0]["detail"] and "guard_person pause → notify" in ev[0]["detail"]
    run(main.put_setting(req, "gates", {"guard_resume": True}))                  # nothing changed: nothing logged
    assert len([e for e in r.store.events(10) if e["kind"] == "gates_changed"]) == 1


def test_the_after_dark_guard_takes_dock_notify_or_ignore_but_not_pause(tmp_path):
    req, r, _ = fake_request(tmp_path)
    with pytest.raises(HTTPException):
        run(main.put_setting(req, "gates", {"guard_after_dark": "pause"}))
    run(main.put_setting(req, "gates", {"guard_after_dark": "notify"}))
    assert control.gates(r.store)["guard_after_dark"] == "notify"
