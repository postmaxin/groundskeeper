"""The robot's serial with only the Yarbo cloud to go by: it comes from the account's robot list, a found serial is
kept across restarts, the wait is said plainly, and the cloud worker survives a fresh sign-in. No network, no robot:
the SDK client is a fake."""
import asyncio
import logging
import threading
import time

import pytest
from types import SimpleNamespace

from app.cloud import CloudTransport

SN = "YB0000DEMO0001"
SN2 = "YB0000DEMO0002"


def run(coro):
    return asyncio.run(coro)


class FakeSDK:
    """The slice of YarboClient the cloud transport uses for a sign-in and the account's robot list."""

    def __init__(self, robots=(), fail=None):
        self.robots, self.fail = list(robots), fail
        self.token, self.refresh_token = "jwt.synthetic", "refresh.synthetic"
        self.listed = 0
        self._auth = SimpleNamespace(refresh=lambda: None)

    def login(self, email, password):
        pass

    def get_devices(self):
        self.listed += 1
        if self.fail:
            raise self.fail
        return [SimpleNamespace(sn=sn, name=f"Robot {i}", model="YB-DEMO", online=True)
                for i, sn in enumerate(self.robots, 1)]


class CountingStop(threading.Event):
    """A stop event whose waits return at once and that sets itself after `n` waits, so _run's loop is driven
    without sleeping."""

    def __init__(self, n):
        super().__init__()
        self.n, self.waits = n, 0

    def wait(self, timeout=None):
        self.waits += 1
        if self.waits >= self.n:
            self.set()
        return self.is_set()


def cloud(tmp_path, sdk=None, sn=None, on_sn=None):
    c = CloudTransport(str(tmp_path), sn, lambda *a: None, on_sn=on_sn)
    c._client = sdk
    return c


# -- the serial from the account ------------------------------------------------------------------------------------
def test_the_accounts_only_robot_gives_the_serial(tmp_path):
    found = []
    stop = threading.Event()
    c = cloud(tmp_path, FakeSDK([SN]), on_sn=lambda sn: (found.append(sn), stop.set()))
    c._run(stop)
    assert c.sn == SN and found == [SN] and c.status()["devices"] == []


def test_several_robots_on_the_account_are_offered_not_guessed(tmp_path):
    sdk = FakeSDK([SN, SN2])
    c = cloud(tmp_path, sdk, on_sn=lambda sn: (_ for _ in ()).throw(AssertionError("must not pick one")))
    c._run(CountingStop(3))
    assert c.sn is None and sdk.listed == 1                      # asked once, not on every wait
    assert [d["sn"] for d in c.status()["devices"]] == [SN, SN2]
    c.sn = SN                                                    # chosen in Settings → Robot
    assert c.status()["devices"] == []


def test_a_failed_robot_list_is_asked_again_after_a_minute(tmp_path):
    sdk = FakeSDK(fail=ConnectionError("offline"))
    c = cloud(tmp_path, sdk)
    assert c._find_sn() is None and sdk.listed == 1
    assert c._find_sn() is None and sdk.listed == 1              # not again straight away
    c._devices_at -= 61
    c._find_sn()
    assert sdk.listed == 2


def test_no_robot_list_without_a_session(tmp_path):
    c = cloud(tmp_path, None)
    c._sdk = lambda: (_ for _ in ()).throw(AssertionError("must not build the SDK client"))
    assert c._find_sn() is None


def test_waiting_for_the_serial_is_said_in_the_status_and_once_in_the_log(tmp_path, caplog):
    c = cloud(tmp_path, FakeSDK([]))
    with caplog.at_level(logging.INFO, logger="app.cloud"):
        c._run(CountingStop(4))
    assert c.status()["error"] == "waiting for the robot's serial (Settings → Robot)"
    said = [r for r in caplog.records if "waiting for the robot's serial" in r.getMessage()]
    assert len(said) == 1


# -- cloud.py bugs ---------------------------------------------------------------------------------------------------
def test_a_refresh_with_no_session_says_needs_login(tmp_path):
    c = cloud(tmp_path, None, sn=SN)
    c.username = "user@example.com"
    assert c.refresh_session() == "expired"
    assert c.state == "needs login" and c.error


def test_the_worker_stops_with_a_reason_when_the_session_is_gone(tmp_path):
    c = cloud(tmp_path, None, sn=SN)
    c.username = "user@example.com"
    c._run(threading.Event())                   # returns at once: nothing to connect with
    assert c.status()["state"] == "needs login" and c.status()["error"]


def test_a_sign_out_racing_the_worker_stays_signed_out(tmp_path):
    c = cloud(tmp_path, None, sn=SN)            # logout() cleared the session and the username
    c.state = "connecting"                       # the worker got past its stop check first
    assert c.refresh_session() == "expired"
    assert c.state == "signed out" and c.error == ""


def test_a_session_the_sdk_refreshes_during_the_lookup_is_saved(tmp_path):
    import json

    class Rotating(FakeSDK):
        def get_devices(self):                   # a 401: the SDK refreshes (and rotates) by itself, then lists
            self.token, self.refresh_token = "jwt.new", "refresh.new"
            return super().get_devices()

    c = cloud(tmp_path, Rotating([SN]))
    assert c._find_sn() == SN
    assert json.loads(c.path.read_text())["refresh_token"] == "refresh.new"


def test_an_expired_session_during_the_lookup_says_needs_login(tmp_path):
    from yarbo_robot_sdk.exceptions import TokenExpiredError
    c = cloud(tmp_path, FakeSDK(fail=TokenExpiredError("refresh token expired")))
    c._run(CountingStop(3))
    assert c.state == "needs login" and "expired" in c.error


def test_a_fresh_sign_in_starts_a_new_worker_without_waiting_for_the_old_one(tmp_path, monkeypatch):
    c = cloud(tmp_path, None, sn=SN)
    # The old worker is slow to notice the stop (as when it sits in _connect_once for up to 20 s).
    old = threading.Thread(target=lambda: time.sleep(3), daemon=True)
    old.start()
    c._thread = old
    started = threading.Event()
    monkeypatch.setattr(c, "_run", lambda stop: (started.set(), stop.wait(5)))

    def sdk():
        if c._client is None:
            c._client = FakeSDK()
        return c._client
    monkeypatch.setattr(c, "_sdk", sdk)
    t0 = time.time()
    c.login("user@example.com", "synthetic-password")
    assert c._thread is not old and started.wait(2) and time.time() - t0 < 2
    c.stop()


def test_a_running_worker_is_not_doubled(tmp_path, monkeypatch):
    c = cloud(tmp_path, None, sn=SN)
    monkeypatch.setattr(c, "_run", lambda stop: stop.wait(5))
    c.start()
    first = c._thread
    c.start()
    assert c._thread is first
    c.stop()


def test_a_stopped_worker_leaves_the_status_to_the_next_one(tmp_path, monkeypatch):
    c = cloud(tmp_path, FakeSDK(), sn=SN)
    stop = threading.Event()

    def connect(s):                              # a sign-in or restart lands while this worker is connecting
        c.state, c.error = "connected", ""       # what the new worker reports
        s.set()
        raise ConnectionError("stopped while connecting")
    monkeypatch.setattr(c, "_connect_once", connect)
    c._run(stop)
    assert c.state == "connected" and c.error == ""


# -- keeping a found serial ------------------------------------------------------------------------------------------
def _ops(tmp_path, monkeypatch):
    from app import config
    from app.imagery import Imagery
    from app.ops import Ops
    from app.recorder import Recorder
    from app.runtime import Runtime
    from app.store import Store

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("YARBO_SN", raising=False)
    env = config.load()
    store = Store(env.data_dir)
    cfg = Runtime(env, store)
    return Ops(cfg, Recorder(cfg, store, Imagery(env.data_dir), None)), env, store


def _kept(store):
    from app.runtime import found_sn
    return found_sn(store)


def test_a_serial_from_the_account_is_kept_and_followed(tmp_path, monkeypatch):
    o, env, store = _ops(tmp_path, monkeypatch)

    async def go():
        o._loop = asyncio.get_running_loop()
        o.cloud.sn = SN                          # the worker sets its own serial, then tells Ops
        o._cloud_found_sn(SN)
        await asyncio.sleep(0)
    run(go())
    assert o.rec.sn == SN and _kept(store) == {"sn": SN, "from": "account"}


def test_the_account_replaces_a_serial_kept_from_the_heart_beat(tmp_path, monkeypatch):
    """Before firmware V3.14.4x closed the local broker the serial came from heart_beat. The account is the only source
    now: a robot chosen there is followed, not refused."""
    from app.runtime import FOUND_SN
    o, env, store = _ops(tmp_path, monkeypatch)
    store.set_setting(FOUND_SN, {"sn": SN, "from": "heart_beat"})
    run(o.rec.apply_robot())
    assert o.rec.sn == SN and o.cloud.sn == SN                   # an older install still knows its robot
    assert o.rec.use_found_sn(SN2)                                # chosen in Settings → Robot
    assert o.rec.sn == SN2 and _kept(store) == {"sn": SN2, "from": "account"}


def test_the_lookup_never_replaces_a_robot_already_followed(tmp_path, monkeypatch):
    o, env, store = _ops(tmp_path, monkeypatch)
    assert o.rec.use_found_sn(SN2)                                # a person chose SN2 while the lookup ran
    assert not o.rec.use_found_sn(SN, chosen=False)               # the lookup's answer arrives late
    assert o.rec.sn == SN2 and _kept(store)["sn"] == SN2


def test_a_serial_once_typed_in_settings_is_moved_and_never_typed_again(tmp_path, monkeypatch):
    import pytest
    from app.runtime import Runtime
    o, env, store = _ops(tmp_path, monkeypatch)
    store.set_setting("integrations", {"robot": {"host": "192.0.2.50", "port": 1883, "sn": SN}})
    cfg = Runtime(env, store)
    assert _kept(store) == {"sn": SN, "from": "settings"}
    assert store.setting("integrations") == {} and "robot" not in cfg.describe()     # address and port go too
    with pytest.raises(KeyError):
        cfg.set("robot", {"sn": SN2})


def test_only_a_robot_the_account_listed_can_be_chosen(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from app import main
    o, env, store = _ops(tmp_path, monkeypatch)
    o.cloud.devices = [{"sn": SN, "name": "", "model": ""}, {"sn": SN2, "name": "", "model": ""}]
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rec=o.rec, ops=o)))
    try:
        run(main.robot_from_account(req, {"sn": "YB0000DEMO0099"}))
        raise AssertionError("a serial not on the account was taken")
    except HTTPException as exc:
        assert exc.status_code == 400
    assert run(main.robot_from_account(req, {"sn": SN2})) == {"ok": True}
    assert o.rec.sn == SN2 and o.cloud.sn == SN2 and _kept(store) == {"sn": SN2, "from": "account"}


def test_a_lookup_that_ends_after_a_stop_writes_nothing(tmp_path):
    """A stopped worker's late lookup must not overwrite the next worker's status or robot list."""
    stop = threading.Event()
    sdk = FakeSDK([SN, SN2])

    def stopped_meanwhile():
        stop.set()                              # signed out, or a new worker started, while the account answered
        return FakeSDK.get_devices(sdk)
    sdk.get_devices = stopped_meanwhile
    c = cloud(tmp_path, sdk)
    c.error = "before"
    c._run(stop)
    assert c.devices == [] and c.error == "before" and c.status()["devices"] == []


def test_a_rotated_session_that_cannot_be_saved_keeps_the_worker_going(tmp_path, monkeypatch, caplog):
    sdk = FakeSDK([SN, SN2])

    def rotate_then_list():
        sdk.token, sdk.refresh_token = "jwt.rotated", "refresh.rotated"
        return FakeSDK.get_devices(sdk)
    sdk.get_devices = rotate_then_list
    c = cloud(tmp_path, sdk)
    c.username = "user@example.com"

    def disk_full():
        raise OSError("no space left on device")
    monkeypatch.setattr(c, "_save", disk_full)
    stop = CountingStop(3)
    with caplog.at_level(logging.WARNING, logger="app.cloud"):
        c._run(stop)                            # no exception escapes the worker: it goes on waiting
    assert stop.waits == 3 and sdk.refresh_token == "refresh.rotated"
    assert any("could not save the Yarbo session" in r.getMessage() for r in caplog.records)


def test_another_robot_is_not_followed_while_a_run_is_open(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from app import main
    o, env, store = _ops(tmp_path, monkeypatch)
    assert o.rec.use_found_sn(SN)
    o.rec.run_id = 7                            # robot A is mid-run
    o.cloud.devices = [{"sn": SN, "name": "", "model": ""}, {"sn": SN2, "name": "", "model": ""}]
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rec=o.rec, ops=o)))
    try:
        run(main.robot_from_account(req, {"sn": SN2}))
        raise AssertionError("switched robots during a run")
    except HTTPException as exc:
        assert exc.status_code == 409
    assert not o.rec.use_found_sn(SN2)
    assert o.rec.sn == SN and _kept(store)["sn"] == SN       # B's telemetry can't land in A's run
    o.rec.run_id = None
    assert o.rec.use_found_sn(SN2) and o.rec.sn == SN2      # once the run has ended


def test_a_demo_leaves_a_real_installs_typed_serial_alone(tmp_path, monkeypatch):
    from dataclasses import replace
    from app import config
    from app.runtime import Runtime
    from app.store import Store
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store = Store(str(tmp_path))
    store.set_setting("integrations", {"robot": {"host": "192.0.2.50", "sn": SN}})
    Runtime(replace(config.load(), demo=True), store)
    assert store.setting("integrations") == {"robot": {"host": "192.0.2.50", "sn": SN}} and _kept(store) == {}


# -- another account, another robot ---------------------------------------------------------------------------------
def test_a_serial_from_another_account_is_dropped_after_a_sign_in(tmp_path):
    forgot = []
    stop = CountingStop(2)
    c = cloud(tmp_path, FakeSDK([SN2, "YB0000DEMO0003"]), sn=SN)
    c.sn_source, c.on_forget, c._check_sn = (lambda: "account"), forgot.append, True
    c._refresh = lambda: True
    c._run(stop)
    assert c.sn is None and forgot == [SN]
    assert [d["sn"] for d in c.status()["devices"]] == [SN2, "YB0000DEMO0003"]     # this account's robots to choose


def test_a_serial_the_new_account_also_has_is_kept(tmp_path):
    sdk = FakeSDK([SN, SN2])
    c = cloud(tmp_path, sdk, sn=SN)
    c.sn_source, c.on_forget = (lambda: "account"), (lambda sn: (_ for _ in ()).throw(AssertionError("dropped")))
    c._check_sn = True
    assert c._kept_sn_elsewhere(threading.Event()) is None and c.sn == SN and sdk.listed == 1


def test_no_serial_is_checked_during_a_run(tmp_path):
    sdk = FakeSDK([SN2])
    c = cloud(tmp_path, sdk, sn=SN)
    c.sn_source, c._check_sn = (lambda: None), True          # Ops answers None while a run is open
    assert c._kept_sn_elsewhere(threading.Event()) is None and c.sn == SN and sdk.listed == 0


@pytest.mark.parametrize("source", ["heart_beat", "settings"])
def test_an_older_installs_serial_is_checked_too(tmp_path, source):
    """The cloud is the only route: a robot the signed-in account doesn't list can't be reached, whatever gave its
    serial before."""
    c = cloud(tmp_path, FakeSDK([SN2]), sn=SN)
    c.sn_source, c._check_sn = (lambda: source), True
    assert c._kept_sn_elsewhere(threading.Event()) is not None and c.sn is None


def test_the_recorder_forgets_a_serial_from_another_account(tmp_path, monkeypatch):
    o, env, store = _ops(tmp_path, monkeypatch)
    assert o.rec.use_found_sn(SN)
    o.rec.snapshot_at, o.rec.t = time.time(), {"battery": 80}
    o.rec.forget_account_sn(SN)
    assert o.rec.sn is None and _kept(store) == {} and o.rec.snapshot_at == 0.0 and o.rec.t == {}


def test_another_robot_starts_with_none_of_the_last_ones_state(tmp_path, monkeypatch):
    """Checks before a command must see robot B's state, not A's: unknown until B reports."""
    o, env, store = _ops(tmp_path, monkeypatch)
    r = o.rec
    assert r.use_found_sn(SN)
    r.snapshot, r.snapshot_at, r.t = {"x": 1}, time.time(), {"battery": 80, "planning": 0}
    r.map_geojson, r.plans, r.gps_ref = {"type": "FeatureCollection", "features": []}, [{"id": 1}], (38.9, -76.5)
    assert r.use_found_sn(SN2)
    assert (r.snapshot, r.snapshot_at, r.t, r.map_geojson, r.plans, r.gps_ref) == ({}, 0.0, {}, None, [], None)
    assert r._map_wake.is_set()                              # B's map is read now, not at the next refresh
    assert r.status()["snapshot_age_s"] is None              # "Robot online" stays closed until B reports


# -- the picker's route through the whole HTTP stack ----------------------------------------------------------------
def test_a_person_can_choose_a_robot_and_an_api_token_cannot(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import auth, main
    monkeypatch.delenv("PANEL_AUTH", raising=False)
    o, env, store = _ops(tmp_path, monkeypatch)
    o.cloud.devices = [{"sn": SN, "name": "", "model": ""}, {"sn": SN2, "name": "", "model": ""}]
    gate = auth.Auth(store)
    gate.announce()
    old = {k: getattr(main.app.state, k, None) for k in ("auth", "rec", "ops")}
    main.app.state.auth, main.app.state.rec, main.app.state.ops = gate, o.rec, o
    try:
        person = TestClient(main.app, base_url="http://panel.test")
        setup = person.post("/api/auth/setup", json={"code": gate.setup_code, "password": "correct horse"})
        assert setup.status_code == 200
        token = person.post("/api/auth/tokens", json={"name": "script"}).json()["token"]
        bot = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {token}"})
        r = bot.post("/api/robot/account", json={"sn": SN2}, headers={"X-Panel-Request": "1"})
        assert r.status_code == 403 and o.rec.sn is None and _kept(store) == {}
        r = person.post("/api/robot/account", json={"sn": SN2}, headers={"X-Panel-Request": "1"})
        assert r.status_code == 200 and o.rec.sn == SN2 and _kept(store)["sn"] == SN2
        r = person.post("/api/robot/account", json={"sn": "YB0000DEMO0099"}, headers={"X-Panel-Request": "1"})
        assert r.status_code == 400 and o.rec.sn == SN2      # only a robot the account listed
    finally:
        for k, v in old.items():
            setattr(main.app.state, k, v)


# -- round 4: pending checks, restored state, queued messages, runs across a switch -------------------------------
def test_the_check_after_a_sign_in_stays_pending_until_it_can_run(tmp_path):
    sdk = FakeSDK(fail=ConnectionError("offline"))
    c = cloud(tmp_path, sdk, sn=SN)
    c.sn_source, c._check_sn = (lambda: None), True              # a run is open
    assert c._kept_sn_elsewhere(threading.Event()) is None and c._check_sn and sdk.listed == 0
    c.sn_source = lambda: "account"                              # the run ended; the account doesn't answer
    assert c._kept_sn_elsewhere(threading.Event()) is None and c._check_sn and sdk.listed == 1
    sdk.fail, sdk.robots = None, [SN]                             # it answers again, and has the robot ...
    assert c._kept_sn_elsewhere(threading.Event()) is None and c._check_sn and sdk.listed == 1   # ... not asked yet
    c._check_tried_at -= 601                                      # ten minutes on
    assert c._kept_sn_elsewhere(threading.Event()) is None and not c._check_sn and c.sn == SN


def test_a_map_restored_with_no_serial_kept_is_not_another_robots(tmp_path, monkeypatch):
    """A serial was forgotten (another account) and the panel restarted: what _restore loaded belongs to the old
    robot, so the first robot followed starts clean."""
    o, env, store = _ops(tmp_path, monkeypatch)
    r = o.rec
    assert r.sn is None
    r.map_raw, r.map_geojson, r.plans = {"areas": []}, {"type": "FeatureCollection", "features": []}, [{"id": 1}]
    assert r.use_found_sn(SN2)
    assert (r.map_raw, r.map_geojson, r.plans) == (None, None, []) and r._map_wake.is_set()


def test_a_message_queued_for_the_last_robot_is_dropped(tmp_path, monkeypatch):
    o, env, store = _ops(tmp_path, monkeypatch)
    r = o.rec
    assert r.use_found_sn(SN2)
    r._handle(f"snowbot/{SN}/device/DeviceMSG", {"BatteryMSG": {"capacity": 80}}, time.time())
    r._handle(f"snowbot/{SN}/device/heart_beat", {"working_state": 1}, time.time())
    assert r.snapshot == {} and r.t == {} and r.heartbeat_at == 0.0


def test_a_switch_closes_the_last_robots_open_run(tmp_path, monkeypatch):
    o, env, store = _ops(tmp_path, monkeypatch)
    r = o.rec
    r._follow(SN)
    now = time.time()
    r.run_id = store.start_run(now - 600, 1, None)
    r.run_stats, r.run_started = recmod_runstats(), now - 600
    r._follow(SN2)                                               # a heart_beat names another robot
    assert r.run_id is None and r.run_stats is None
    assert store.runs()[0]["ended"] is not None
    assert any(e["kind"] == "run_ended" and "robot changed" in e["detail"] for e in store.events(10))


def test_a_late_forget_leaves_a_newer_serial_alone(tmp_path, monkeypatch):
    from app.runtime import FOUND_SN
    o, env, store = _ops(tmp_path, monkeypatch)
    r = o.rec
    r._follow(SN2)                                               # a heart_beat found B ...
    store.set_setting(FOUND_SN, {"sn": SN2, "from": "heart_beat"})
    r.forget_account_sn(SN)                                      # ... before the account's "drop A" arrived
    assert r.sn == SN2 and _kept(store) == {"sn": SN2, "from": "heart_beat"}


def recmod_runstats():
    from app.runs import RunStats
    return RunStats()


# -- the whole-PR review: serial format, mowing robots, a heard robot, the last robot's lists ------------------------
def test_a_malformed_serial_from_the_account_is_never_followed(tmp_path):
    """It would go into the MQTT topics: a "+" or "#" would subscribe to every robot."""
    c = cloud(tmp_path, FakeSDK(["YB+0000", "YB/../X", SN2]))
    assert c._find_sn() == SN2


def test_the_account_check_waits_while_the_robot_is_mowing(tmp_path, monkeypatch):
    o, env, store = _ops(tmp_path, monkeypatch)
    assert o.rec.use_found_sn(SN)
    o.rec.plan_active = lambda ts=None: True                      # a plan is going, no run recorded yet
    assert o._kept_sn_source() is None
    o.rec.plan_active = lambda ts=None: False
    assert o._kept_sn_source() == "account"


def test_no_robot_switch_while_a_plan_is_going(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from app import main
    o, env, store = _ops(tmp_path, monkeypatch)
    assert o.rec.use_found_sn(SN)
    o.rec.plan_active = lambda ts=None: True
    o.cloud.devices = [{"sn": SN, "name": "", "model": ""}, {"sn": SN2, "name": "", "model": ""}]
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rec=o.rec, ops=o)))
    try:
        run(main.robot_from_account(req, {"sn": SN2}))
        raise AssertionError("switched robots during a plan")
    except HTTPException as exc:
        assert exc.status_code == 409 and o.rec.sn == SN


def test_a_switch_drops_the_last_robots_lists_and_survey(tmp_path, monkeypatch):
    o, env, store = _ops(tmp_path, monkeypatch)
    r = o.rec
    r._follow(SN)
    r.replies["get_all_map_backup"] = {"ts": 1.0, "changed": 1.0, "data": [{"id": 7}]}
    r.wifi_survey, r._wifi_waiting = {"asked": 3}, (1.0, 0.0, 0.0)
    r._follow(SN2)
    assert r.replies == {} and r.wifi_survey is None and r._wifi_waiting is None


def test_a_stopped_worker_leaves_a_new_sign_ins_check_alone(tmp_path):
    stop = threading.Event()
    sdk = FakeSDK([SN])
    c = cloud(tmp_path, sdk, sn=SN)
    c.sn_source, c._check_sn = (lambda: "account"), True

    def signed_in_again_meanwhile():
        stop.set()
        c._check_sn = True                                       # login() set it for the new account
        return FakeSDK.get_devices(sdk)
    sdk.get_devices = signed_in_again_meanwhile
    assert c._kept_sn_elsewhere(stop) is None and c._check_sn


# -- a robot that starts mowing during the check, and a switch during a write --------------------------------------
def test_a_robot_that_starts_mowing_during_the_check_is_kept(tmp_path):
    sdk = FakeSDK([SN2])
    c = cloud(tmp_path, sdk, sn=SN)
    mowing = []
    c.sn_source = lambda: None if mowing else "account"
    c._check_sn = True

    def run_started_meanwhile():
        mowing.append(True)
        return FakeSDK.get_devices(sdk)
    sdk.get_devices = run_started_meanwhile
    assert c._kept_sn_elsewhere(threading.Event()) is None
    assert c.sn == SN and c._check_sn                            # kept, and checked again once it has finished


def test_the_recorder_keeps_a_robot_that_is_mowing_when_the_forget_arrives(tmp_path, monkeypatch):
    o, env, store = _ops(tmp_path, monkeypatch)
    r = o.rec
    assert r.use_found_sn(SN)
    told = []
    r.on_sn = told.append
    r.run_id = 7
    r.forget_account_sn(SN)
    assert r.sn == SN and _kept(store)["sn"] == SN and told == [SN]    # and the cloud is sent back to it


def test_no_robot_switch_during_a_write(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from app import main
    o, env, store = _ops(tmp_path, monkeypatch)
    assert o.rec.use_found_sn(SN)
    o.rec.plan_active = lambda ts=None: False
    o.cloud.devices = [{"sn": SN, "name": "", "model": ""}, {"sn": SN2, "name": "", "model": ""}]
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rec=o.rec, ops=o)))

    async def go():
        async with o.map_writer.lock:
            try:
                await main.robot_from_account(req, {"sn": SN2})
            except HTTPException as exc:
                return exc.status_code
    assert run(go()) == 409 and o.rec.sn == SN


def test_the_robot_counts_as_connected_only_once_it_has_been_heard(tmp_path, monkeypatch):
    """The cloud can be up while the robot is off: /health and the setup guide need a message from the robot."""
    o, env, store = _ops(tmp_path, monkeypatch)
    r = o.rec
    o.cloud.state, o.cloud.connected = "connected", True
    assert r.robot_status()["connected"] and not r.robot_status()["heard"]
    r.last_message_at = time.time() - 5
    assert r.robot_status()["heard"]
    r.last_message_at = time.time() - 3600                       # an hour of silence
    assert not r.robot_status()["heard"]


def test_a_message_for_the_last_robot_does_not_count_as_hearing_the_next(tmp_path, monkeypatch):
    o, env, store = _ops(tmp_path, monkeypatch)
    r = o.rec
    r._follow(SN)
    r._handle(f"snowbot/{SN}/device/heart_beat", {"working_state": 1}, time.time())
    assert r.received == 1 and r.last_message_at
    r._follow(SN2)
    assert r.received == 0 and r.last_message_at == 0.0              # nothing from SN2 yet
    r._handle(f"snowbot/{SN}/device/heart_beat", {"working_state": 1}, time.time())   # queued before the switch
    assert r.received == 0 and r.last_message_at == 0.0


def test_a_read_is_marked_before_it_goes_and_unmarked_if_it_fails(tmp_path):
    from app.link import RobotLink
    seen, marks = [], []

    class Cloud:
        state, connected = "connected", True

        def __init__(self, fail):
            self.fail = fail

        def publish(self, cmd, payload):
            seen.append(list(marks))                             # what an echo handled now would find
            if self.fail:
                raise ConnectionError("refused")

    def mark(cmd):
        marks.append(cmd)
        return lambda: marks.remove(cmd)
    RobotLink(None, Cloud(False), None, mark_sent=mark).read("get_map")
    assert seen == [["get_map"]] and marks == ["get_map"]
    marks.clear()
    with pytest.raises(ConnectionError):
        RobotLink(None, Cloud(True), None, mark_sent=mark).read("get_map")
    assert marks == []


def test_an_echo_of_a_request_is_not_hearing_the_robot(tmp_path, monkeypatch):
    """The cloud also delivers app/ topics (the panel's own read requests come back): only the robot's device/
    messages count towards "heard"."""
    o, env, store = _ops(tmp_path, monkeypatch)
    r = o.rec
    r._follow(SN)
    o.cloud.state, o.cloud.connected = "connected", True
    r._handle(f"snowbot/{SN}/app/get_device_msg", {}, time.time())
    assert r.last_message_at == 0.0 and not r.robot_status()["heard"]
    r._handle(f"snowbot/{SN}/device/heart_beat", {"working_state": 1}, time.time())
    assert r.robot_status()["heard"]
