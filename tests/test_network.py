"""The Network card: link strengths, Wi-Fi scan, join and forget (app/network.py, main._network_write_block).
All synthetic: a stand-in recorder and map writer, made-up network names."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app import main, mapwrite, network
from app.mapwrite import WriteRefused
from app.network import Network

SCAN = {"state": 0, "msg": "", "data": {"wifi_list": [
    {"wifi_name": "DemoNet", "signal": "71", "security": "WPA2", "saved": True},
    {"wifi_name": "DemoNet", "signal": "40", "security": "WPA2", "saved": False},     # a second access point
    {"wifi_name": "DemoNet-IoT", "signal": "55", "security": "WPA2", "saved": False},
    {"wifi_name": "Cafe-Open", "signal": "20", "security": "", "saved": False},
    {"wifi_name": "", "signal": "90"}]}}


class Store:
    def __init__(self):
        self.events, self.settings = [], {}

    def event(self, kind, detail):
        self.events.append((kind, detail))

    def setting(self, key, default=None):
        return self.settings.get(key, default)


class Rec:
    """Answers reads like the robot: the scan, and the connected name (none = no answer, as the robot does)."""

    def __init__(self, connected="DemoNet", scan=SCAN):
        self.t = {"halow": -63, "charging": 2}
        self.snapshot = {"net_module_status": {"lte_rssi": 0, "lte_rsrp": 0, "lte_status": 0}}
        self.update_at = time.time() - 3
        self.replies, self.asked = {}, []
        self.connected, self.scan_reply, self.surveying = connected, scan, False
        self.run_id, self.plan = None, False

    def _surveying(self):
        return self.surveying

    def plan_active(self):
        return self.plan

    async def request(self, cmd, timeout=15, payload=None):
        self.asked.append(cmd)
        if cmd == "get_connect_wifi_name":
            if self.connected is None:
                raise asyncio.TimeoutError
            reply = {"state": 0, "msg": "", "data": self.connected}
        elif cmd == "get_wifi_list":
            reply = self.scan_reply
        else:
            raise AssertionError(cmd)
        self.replies[cmd] = {"ts": time.time(), "data": reply}
        return reply


class Lock:
    def __init__(self):
        self._l = asyncio.Lock()

    async def __aenter__(self):
        await self._l.acquire()

    async def __aexit__(self, *exc):
        self._l.release()

    def locked(self):
        return self._l.locked()


class Writer:
    """The map writer's two parts the network uses. `effect` is what the robot does when the command arrives."""

    def __init__(self, effect=None, reply=None):
        self.lock, self.sent, self.effect, self.reply = Lock(), [], effect, reply

    async def _send(self, cmd, payload, rollback=False):
        assert cmd in mapwrite.WRITE_COMMANDS
        self.sent.append((cmd, payload))
        if self.effect:
            self.effect(cmd, payload)
        return {"sent": True, **(self.reply or {"ok": False, "error": "no reply in 15 s"})}


def make(**kw):
    rec, store = Rec(**{k: v for k, v in kw.items() if k in ("connected", "scan")}), Store()
    w = Writer(kw.get("effect"), kw.get("reply"))
    return Network(store, rec, w), rec, w, store


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(network, "VERIFY_S", 0.05)
    monkeypatch.setattr(network, "VERIFY_EVERY_S", 0.01)
    monkeypatch.setattr(network, "FRESH_SCAN_S", 0)       # the stand-in answers at once


def test_the_list_keeps_one_row_per_network_strongest_first():
    rows = network.parse_list(SCAN["data"])
    assert [(r["name"], r["signal"], r["saved"]) for r in rows] == [
        ("DemoNet", 71.0, True), ("DemoNet-IoT", 55.0, False), ("Cafe-Open", 20.0, False)]
    assert network.parse_list("not a list") is None


def test_status_shows_halow_level_lte_and_the_connected_networks_signal():
    n, rec, *_ = make()
    assert n.status()["wifi"]["networks"] is None                  # nothing asked yet: nothing invented
    run(n.scan())
    st = n.status()
    assert st["halow"]["dbm"] == -63 and st["halow"]["level"] == "good"
    assert st["lte"]["reported"] is False                          # all zeros: not reported
    assert st["wifi"]["connected"] == "DemoNet" and st["wifi"]["signal"] == 71.0
    assert [network.halow_level(v) for v in (-70, -80, -82, -95, 0, None)] == ["good", "weak", "fallback", "fallback",
                                                                              None, None]


def test_no_answer_to_the_name_request_means_no_wifi():
    n, *_ = make(connected=None)
    run(n.scan())
    assert n.status()["wifi"]["connected"] is None


def test_a_scan_is_not_asked_for_while_the_survey_scans():
    n, rec, *_ = make()
    rec.surveying = True
    with pytest.raises(WriteRefused):
        run(n.scan())
    assert rec.asked == []


def test_a_join_is_confirmed_by_the_robot_naming_the_network():
    def joins(cmd, payload):
        rec.connected = payload["wifi_name"]
    n, rec, w, store = make(effect=joins)
    run(n.scan())
    out = run(n.join("DemoNet-IoT", "correct horse"))
    assert out["ok"] is True and out["connected"] == "DemoNet-IoT"
    assert w.sent == [("change_to_wifi", {"wifi_name": "DemoNet-IoT", "password": "correct horse"})]
    assert store.events[-1][0] == "wifi_joined"
    assert "correct horse" not in json.dumps(store.events) and "DemoNet" not in json.dumps(store.events)


def test_a_join_the_robot_does_not_take_is_reported_unconfirmed():
    n, rec, w, store = make()
    run(n.scan())
    out = run(n.join("DemoNet-IoT", "correct horse"))
    assert out["ok"] is False and out["connected"] == "DemoNet"
    assert store.events[-1][0] == "wifi_join_unconfirmed"


def test_a_join_the_robot_refuses_is_not_waited_on():
    n, rec, w, store = make(reply={"ok": False, "state": -1, "msg": "wrong password"})
    run(n.scan())
    asked = len(rec.asked)
    out = run(n.join("DemoNet-IoT", "correct horse"))
    assert out == {"ok": False, "error": "wrong password"} and len(rec.asked) == asked
    assert store.events[-1][0] == "wifi_join_refused"


@pytest.mark.parametrize("name, pw", [("DemoNet-IoT", ""), ("DemoNet-IoT", "short"), ("DemoNet-IoT", "x" * 64),
                                      ("Somewhere-Else", "correct horse"), ("", "correct horse")])
def test_a_join_needs_a_scanned_network_and_a_valid_password(name, pw):
    n, rec, w, _ = make()
    run(n.scan())
    with pytest.raises(ValueError):
        run(n.join(name, pw))
    assert w.sent == []


@pytest.mark.parametrize("name, pw", [("DemoNet", ""), ("DemoNet", None), ("Cafe-Open", ""),
                                      ("Cafe-Open", "correct horse")])
def test_a_join_without_a_password_or_to_an_open_network_is_not_sent(name, pw):
    # Only a join with a password was captured: an empty one might replace a saved network's working password.
    n, rec, w, _ = make()
    run(n.scan())
    with pytest.raises(ValueError):
        run(n.join(name, pw))
    assert w.sent == []


def test_a_saved_network_joins_with_its_password_and_a_64_digit_key_is_a_password():
    n, rec, w, _ = make(effect=lambda c, p: setattr(rec, "connected", p["wifi_name"]))
    run(n.scan())
    assert run(n.join("DemoNet", "correct horse"))["ok"] is True
    assert run(n.join("DemoNet-IoT", "ab" * 32))["ok"] is True
    with pytest.raises(ValueError):
        run(n.join("DemoNet-IoT", "é" * 32))                     # 32 characters, 64 bytes


def test_the_gate_is_asked_again_once_the_write_lock_is_held():
    n, rec, w, _ = make()
    run(n.scan())
    with pytest.raises(WriteRefused):
        run(n.join("DemoNet-IoT", "correct horse", gate=lambda: "the robot must be on its charger"))
    with pytest.raises(WriteRefused):
        run(n.forget("DemoNet", gate=lambda: "a plan may be running"))
    assert w.sent == []


@pytest.mark.parametrize("data, name", [("DemoNet", "DemoNet"), ("1.0", "1.0"), ("true", "true"), ("MTIz", "MTIz"),
                                        ('"Home"', '"Home"'), ("[1]", "[1]"), ({"wifi_name": "Home"}, "Home"),
                                        ('{"wifi_name": "Home"}', "Home"), ("", None), (None, None), (5, None)])
def test_the_connected_name_is_taken_as_it_is(data, name):
    assert network.parse_connected({"state": 0, "data": data}) == name


def test_a_refused_name_request_is_not_a_name():
    assert network.parse_connected({"state": -1, "msg": "failed", "data": "failed"}) is None


def test_no_answer_after_a_known_name_means_no_wifi_now():
    n, rec, *_ = make()
    run(n.scan())
    assert n.status()["wifi"]["connected"] == "DemoNet"
    rec.connected = None                                          # the robot left the network: no answer any more
    run(n.scan())
    assert n.status()["wifi"]["connected"] is None


def test_a_scan_is_not_asked_for_during_a_plan():
    n, rec, *_ = make()
    rec.plan = True
    with pytest.raises(WriteRefused):
        run(n.scan())
    rec.plan, rec.run_id = None, 7
    with pytest.raises(WriteRefused):
        run(n.scan())
    assert rec.asked == []


def test_an_answer_too_soon_after_the_ask_is_an_earlier_scans(monkeypatch):
    monkeypatch.setattr(network, "FRESH_SCAN_S", 5)
    n, rec, *_ = make()
    with pytest.raises(WriteRefused):
        run(n.scan())
    assert rec.asked == ["get_wifi_list", "get_wifi_list"]       # asked once more, then gave up


def test_a_failed_name_read_does_not_fail_the_scan():
    n, rec, *_ = make()

    async def no_name(cmd, timeout=15, payload=None):
        if cmd == "get_connect_wifi_name":
            raise ConnectionError("no route")
        return await Rec.request(rec, cmd, timeout, payload)
    rec.request = no_name
    assert run(n.scan())["wifi"]["networks"]


def test_a_forget_is_confirmed_by_a_fresh_scan():
    def forgets(cmd, payload):
        rec.scan_reply = {"state": 0, "data": {"wifi_list": [
            {"wifi_name": "DemoNet", "signal": "70", "security": "WPA2", "saved": False}]}}
    n, rec, w, store = make(effect=forgets)
    run(n.scan())
    assert run(n.forget("DemoNet"))["ok"] is True
    assert w.sent == [("forget_wifi", {"wifi_name": "DemoNet"})] and store.events[-1][0] == "wifi_forgotten"


def test_a_forget_that_leaves_it_saved_is_reported():
    n, rec, w, store = make()
    run(n.scan())
    out = run(n.forget("DemoNet"))
    assert out["ok"] is False and store.events[-1][0] == "wifi_forget_unconfirmed"


def test_only_a_saved_or_connected_network_can_be_forgotten():
    n, rec, w, _ = make(connected="DemoNet")
    run(n.scan())
    with pytest.raises(ValueError):
        run(n.forget("DemoNet-IoT"))
    assert w.sent == []


def test_the_control_log_never_holds_the_wifi_password():
    logged = mapwrite._audit_args("change_to_wifi", {"wifi_name": "DemoNet", "password": "correct horse"})
    assert "correct horse" not in logged and "DemoNet" not in logged and json.loads(logged)["password"] == "***"
    assert "DemoNet" not in mapwrite._audit_args("forget_wifi", {"wifi_name": "DemoNet"})


def _request(**over):
    t = {"charging": 2, **over.pop("t", {})}
    r = SimpleNamespace(t=t, store=Store(), snapshot_at=time.time() - 5, run_id=None,
                        plan_active=lambda: over.get("plan", False))
    r.store.settings["map_write_enabled"] = over.get("writes", True)
    o = SimpleNamespace(cloud=SimpleNamespace(state=over.get("cloud", "connected")),
                        map_writer=SimpleNamespace(lock=Lock()),
                        controller=SimpleNamespace(start_settling=lambda: over.get("starting")))
    from app.link import RobotLink
    o.link = RobotLink(r, o.cloud, r.store)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rec=r, ops=o)))


def test_wifi_writes_need_the_robot_docked_idle_and_writes_on():
    assert main._network_write_block(_request()) is None
    assert "switched off" in main._network_write_block(_request(writes=False))
    assert "charger" in main._network_write_block(_request(t={"charging": 0}))
    assert "plan" in main._network_write_block(_request(plan=True))
    assert "plan" in main._network_write_block(_request(plan=None))          # unknown plan state blocks
    assert "cloud" in main._network_write_block(_request(cloud="connecting"))
    busy = _request()
    run(busy.app.state.ops.map_writer.lock.__aenter__())
    assert "in progress" in main._network_write_block(busy)
    assert main._network_write_block(busy, held=True) is None          # the write itself holds the lock
    stale = _request()
    stale.app.state.rec.snapshot_at = time.time() - 300
    assert "recent data" in main._network_write_block(stale)
    assert "a moment" in main._network_write_block(_request(starting="Start plan was sent 2 s ago; try again in a moment"))
