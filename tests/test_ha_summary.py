"""GET /api/ha/summary (#12): the one read a Home Assistant integration polls with an API token. Synthetic status
only; no robot, no network."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, ha, main
from app.store import Store


def robot_status(run=None, battery=82):
    return {
        "sn": "YB0000DEMO0001", "state": "docked", "activity": "idle", "pause_reason": None, "plan_error": None,
        "snapshot_age_s": 2.0, "run": run, "conditions": {"state": "Dry", "dry_by": None},
        "transport": {"robot": {"connected": True, "heard": True}},
        "robot": {"battery": battery, "charging_status": 1, "error_code": 0, "head": "Mower",
                  "position": {"lon": -76.49, "lat": 38.98, "x": 1.0, "y": 2.0, "heading": 90, "t": 1},
                  "rtk": {"label": "RTK fixed", "sats": 31}},
    }


def ops_status(control=True, held=None):
    return {"cloud": {"state": "connected"}, "control_enabled": control, "why": {"held": held, "last_run": None},
            "pending": None}


PLANS = [{"id": 3, "name": "Front"}, {"id": 7, "name": "Back"}]
WINDOW = {"window": {"id": "w1", "name": "Mornings", "plan_id": 7, "auto_start": False}, "next": 1_800_000_000.0,
          "active": False}


def test_the_summary_carries_no_position():
    s = ha.summary(robot_status(), ops_status(), None, [WINDOW], PLANS, None, {"version": "0.0.0", "build": None})
    text = json.dumps(s)
    assert "38.98" not in text and "-76.49" not in text and "position" not in text
    assert s["battery"] == 82 and s["rtk"] == {"label": "RTK fixed", "sats": 31} and s["local_link"] is True


def test_the_next_window_names_its_plan():
    s = ha.summary(robot_status(), ops_status(), None, [{"window": {"id": "old"}, "next": None, "active": False}, WINDOW],
                   PLANS, None, {"version": "0.0.0", "build": None})
    assert s["next_window"] == {"id": "w1", "name": "Mornings", "plan_id": 7, "plan_name": "Back",
                                "next": 1_800_000_000.0, "active": False, "auto_start": False}


def test_a_refused_start_says_why():
    start = {"plan_id": 7, "would_send": False, "reason": "lawn is Wet",
             "why": [{"code": "lawn_wet", "title": "The lawn is wet", "fix": "Wait until it dries", "link": "#x",
                      "detail": "", "severity": "block", "source": "check"}]}
    s = ha.summary(robot_status(), ops_status(), None, [], PLANS, start, {"version": "0.0.0", "build": None})
    assert s["can_start"] == {"plan_id": 7, "plan_name": "Back", "would_send": False, "reason": "lawn is Wet",
                              "why": [{"code": "lawn_wet", "title": "The lawn is wet", "fix": "Wait until it dries"}]}


def test_a_run_in_progress_and_the_last_run():
    run = {"id": 12, "started": 1.0, "distance_m": 40.2, "points": 9, "held_since": None, "plan": None}
    last = {"id": 11, "started": 0.0, "ended": 1.0, "end_reason": "finished", "distance_m": 300.0,
            "rtk_fixed_pct": 99.0, "summary_json": "{}"}
    s = ha.summary(robot_status(run=run), ops_status(), last, [], PLANS, None, {"version": "0.0.0", "build": None})
    assert s["running"] is True and s["run"] == {"id": 12, "started": 1.0, "distance_m": 40.2}
    assert s["last_run"] == {"id": 11, "started": 0.0, "ended": 1.0, "end_reason": "finished", "distance_m": 300.0,
                             "rtk_fixed_pct": 99.0}


# -- the route ----------------------------------------------------------------------------------------------------
class FakeRec:
    def __init__(self, store):
        self.store, self.plans = store, PLANS

    def status(self):
        return robot_status()

    def robot_status(self):
        return {"connected": True, "heard": True}


class FakeController:
    def __init__(self):
        self.calls = []

    def enabled(self):
        return True

    async def preview(self, cmd, args, actor="user"):
        self.calls.append((cmd, args, actor))
        return {"cmd": cmd, "would_send": True, "reason": "", "why": []}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("PANEL_AUTH", raising=False)
    store = Store(str(tmp_path))
    gate = auth.Auth(store)
    gate.announce()
    ctl = FakeController()
    upcoming = [WINDOW]
    fake_ops = SimpleNamespace(controller=ctl, status=ops_status,
                               scheduler=SimpleNamespace(upcoming=lambda now: upcoming))
    old = {k: getattr(main.app.state, k, None) for k in ("auth", "rec", "ops", "ha_start_cache")}
    main.app.state.auth, main.app.state.rec, main.app.state.ops = gate, FakeRec(store), fake_ops
    main.app.state.ha_start_cache = None
    c = TestClient(main.app, base_url="http://panel.test")
    yield SimpleNamespace(c=c, auth=gate, store=store, ctl=ctl, upcoming=upcoming)
    for k, v in old.items():
        setattr(main.app.state, k, v)


def _token(t):
    assert t.c.post("/api/auth/setup", json={"code": t.auth.setup_code, "password": "correct horse"}).status_code == 200
    return t.c.post("/api/auth/tokens", json={"name": "ha"}).json()["token"]


def test_a_token_reads_the_summary_and_its_start_check_is_the_strict_one(client):
    bot = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {_token(client)}"})
    r = bot.get("/api/ha/summary")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["api"] == ha.SUMMARY_API and s["version"] and s["control_enabled"] is True
    assert s["can_start"]["would_send"] is True and s["can_start"]["plan_name"] == "Back"
    assert client.ctl.calls == [("start_plan", {"plan_id": 7}, "token")]       # the next window's plan, as a token


def test_without_a_window_the_start_check_uses_the_first_plan(client):
    client.upcoming.clear()
    _token(client)
    assert client.c.get("/api/ha/summary").json()["next_window"] is None
    assert client.ctl.calls == [("start_plan", {"plan_id": 3}, "user")]


def test_the_summary_is_locked(client):
    assert client.c.get("/api/ha/summary").status_code == 401


def test_a_start_check_that_fails_leaves_the_rest(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("frigate down")
    monkeypatch.setattr(client.ctl, "preview", boom)
    _token(client)
    s = client.c.get("/api/ha/summary").json()
    assert s["can_start"] is None and s["battery"] == 82


def test_the_summary_names_the_robot_by_a_hash_never_its_serial():
    s = ha.summary(robot_status(), ops_status(), None, [], PLANS, None, {"version": "0.0.0", "build": None})
    assert "YB0000DEMO0001" not in json.dumps(s) and "sn" not in s
    assert s["robot_id"] == hashlib.sha256(b"YB0000DEMO0001").hexdigest()[:16]
    no_robot = {**robot_status(), "sn": None}
    assert ha.summary(no_robot, ops_status(), None, [], PLANS, None, {"version": "0", "build": None})["robot_id"] is None


def test_the_summary_says_when_the_robot_is_stuck_or_out_of_touch():
    s = ha.summary(robot_status(), ops_status(), None, [], PLANS, None, {"version": "0.0.0", "build": None})
    assert (s["stalled"], s["stalled_s"], s["rtk_wait"], s["lost_contact"]) == (False, 0, False, False)
    stuck = {**robot_status(), "stalled": True, "stalled_s": 420, "rtk_wait": False, "lost_contact": True}
    s = ha.summary(stuck, ops_status(), None, [], PLANS, None, {"version": "0.0.0", "build": None})
    assert (s["stalled"], s["stalled_s"], s["lost_contact"]) == (True, 420, True)


def test_the_last_run_is_the_last_finished_one_not_the_run_going_now(client, monkeypatch):
    going = {"id": 12, "started": 5.0, "ended": None, "end_reason": None, "distance_m": 10.0, "rtk_fixed_pct": 90.0}
    done = {"id": 11, "started": 1.0, "ended": 2.0, "end_reason": "finished", "distance_m": 300.0, "rtk_fixed_pct": 99.0}
    monkeypatch.setattr(client.store, "runs", lambda limit=100: [going, done])
    _token(client)
    assert client.c.get("/api/ha/summary").json()["last_run"]["id"] == 11


def test_the_start_check_is_reused_for_30_seconds(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    _token(client)
    for _ in range(3):
        assert client.c.get("/api/ha/summary").json()["can_start"]["would_send"] is True
    assert len(client.ctl.calls) == 1                      # Frigate and presence asked once, not on every read
    now[0] += 31
    client.c.get("/api/ha/summary")
    assert len(client.ctl.calls) == 2


def test_the_start_check_is_kept_apart_for_a_person_and_a_token(client):
    bot = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {_token(client)}"})
    client.c.get("/api/ha/summary")
    bot.get("/api/ha/summary")
    assert [c[2] for c in client.ctl.calls] == ["user", "token"]


def test_a_failed_start_check_is_not_reused(client, monkeypatch):
    real, fails = client.ctl.preview, [True]

    async def flaky(*a, **k):
        if fails.pop() if fails else False:
            raise RuntimeError("frigate down")
        return await real(*a, **k)
    monkeypatch.setattr(client.ctl, "preview", flaky)
    _token(client)
    assert client.c.get("/api/ha/summary").json()["can_start"] is None
    assert client.c.get("/api/ha/summary").json()["can_start"]["would_send"] is True


def test_what_goes_to_home_assistant_reads_the_recorders_own_status(tmp_path):
    """The HA entities and the summary are built from the recorder's real status, not a hand-made copy of it, so a
    renamed field there fails here (the robot link moved from transport.local to transport.robot)."""
    import asyncio

    from tests.test_demo_robot import _recorder
    r = _recorder(tmp_path)
    asyncio.run(r.apply_robot())
    try:
        import time
        r.transport._on_message = r._handle                     # straight in, no event loop needed
        r.transport.tick(time.time() + 30)                      # its next heartbeat is due: now it is heard
        status = r.status()
        ents = ha.entities_from(status, None, None)
        assert ents[f"sensor.{ha.PREFIX}_local_link"]["state"] == "connected"
        s = ha.summary(status, ops_status(), None, [], PLANS, None, {"version": "0.0.0", "build": None})
        assert s["local_link"] is True
    finally:
        r.transport.stop()


def test_the_link_entity_needs_the_robot_heard_not_only_the_cloud():
    """With the cloud up and the robot off (or out of range), Home Assistant must see the link down, as /health does."""
    status = robot_status()
    status["transport"] = {"robot": {"connected": True, "heard": False}}
    assert ha.entities_from(status, None, None)[f"sensor.{ha.PREFIX}_local_link"]["state"] == "down"
    s = ha.summary(status, ops_status(), None, [], PLANS, None, {"version": "0.0.0", "build": None})
    assert s["local_link"] is False
