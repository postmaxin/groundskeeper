"""GET /api/ha/stream (#12): the live feed for a Home Assistant map card. Synthetic status only; no robot, no network."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import auth, ha, main
from app.store import Store
from tests.test_ha_summary import robot_status

FOLLOW = {"camera": "garden", "covered": True, "reason": "covers the robot's position"}


def test_the_state_event_has_the_position_run_follow_camera_and_map_version():
    run = {"id": 12, "started": 1.0, "distance_m": 40.2, "points": 9}
    s = ha.live(robot_status(run=run), FOLLOW, "abc123")
    assert s == {"api": ha.LIVE_API, "state": "docked", "activity": "idle",
                 "position": {"t": 1, "lon": -76.49, "lat": 38.98, "heading": 90, "pitch": None, "roll": None,
                              "speed": None},
                 "run": {"id": 12, "distance_m": 40.2}, "follow": {"camera": "garden", "covered": True},
                 "map": "abc123"}


def test_no_fix_no_run_no_camera_no_map():
    status = robot_status()
    status["robot"]["position"] = {"lon": None, "lat": None, "x": None, "y": None, "heading": None, "t": None}
    s = ha.live(status, None, None)
    assert (s["position"], s["run"], s["follow"], s["map"]) == (None, None, None, None)


def test_a_pose_keeps_only_what_a_map_needs():
    p = {"seq": 4, "t": 9.5, "lon": 1.0, "lat": 2.0, "heading": 45, "pitch": 3, "roll": 1, "speed": 0.4, "x": 7.0}
    assert ha.pose(p) == {"t": 9.5, "lon": 1.0, "lat": 2.0, "heading": 45, "pitch": 3, "roll": 1, "speed": 0.4}


def test_the_state_position_carries_tilt_and_the_mean_wheel_speed():
    status = robot_status()
    status["robot"].update(pitch=2.5, roll=-1.0, speed=[0.4, 0.6])
    pos = ha.live(status, None, None)["position"]
    assert (pos["pitch"], pos["roll"], pos["speed"]) == (2.5, -1.0, 0.5)
    assert "x" not in pos and "y" not in pos
    status["robot"].update(speed=[None, None])
    assert ha.live(status, None, None)["position"]["speed"] is None


class FakeRec:
    def __init__(self, store):
        self.store, self.pose_seq = store, 2
        self.poses = [{"seq": 1, "t": 1.0, "lon": 1.0, "lat": 2.0, "heading": 0, "pitch": 1, "roll": 0, "speed": 0},
                      {"seq": 2, "t": 2.0, "lon": 1.1, "lat": 2.1, "heading": 10, "pitch": 1, "roll": 0, "speed": 0},
                      {"seq": 3, "t": 3.0, "lon": 1.2, "lat": 2.2, "heading": 20, "pitch": 1, "roll": 0, "speed": 0.3}]

    def status(self):
        return robot_status()

    def poses_since(self, seq):
        return [p for p in self.poses if p["seq"] > seq]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("PANEL_AUTH", raising=False)
    store = Store(str(tmp_path))
    gate = auth.Auth(store)
    gate.announce()
    fake_ops = SimpleNamespace(follow_camera=lambda: FOLLOW)
    old = {k: getattr(main.app.state, k, None) for k in ("auth", "rec", "ops")}
    main.app.state.auth, main.app.state.rec, main.app.state.ops = gate, FakeRec(store), fake_ops
    calls = [0]

    async def gone_after_one_loop(self):          # the stream's loop ends once the caller has had one round
        calls[0] += 1
        return calls[0] > 1
    monkeypatch.setattr(Request, "is_disconnected", gone_after_one_loop)
    yield SimpleNamespace(c=TestClient(main.app, base_url="http://panel.test"), auth=gate)
    for k, v in old.items():
        setattr(main.app.state, k, v)


def _events(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_a_token_gets_a_state_event_then_only_the_new_poses(client):
    assert client.c.post("/api/auth/setup", json={"code": client.auth.setup_code,
                                                  "password": "correct horse"}).status_code == 200
    token = client.c.post("/api/auth/tokens", json={"name": "ha"}).json()["token"]
    bot = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {token}"})
    r = bot.get("/api/ha/stream")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = _events(r.text)
    assert [e[0] for e in events] == ["state", "pose"]
    assert events[0][1]["follow"] == {"camera": "garden", "covered": True}
    assert events[1][1] == [{"t": 3.0, "lon": 1.2, "lat": 2.2, "heading": 20, "pitch": 1, "roll": 0,
                             "speed": 0.3}]   # after the seq at connect only


def test_the_stream_is_locked(client):
    assert client.c.get("/api/ha/stream").status_code == 401


def test_a_metrics_only_token_cannot_read_the_position(client):
    assert client.c.post("/api/auth/setup", json={"code": client.auth.setup_code,
                                                  "password": "correct horse"}).status_code == 200
    token = client.c.post("/api/auth/tokens", json={"name": "scrape", "scope": "metrics"}).json()["token"]
    scrape = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {token}"})
    assert scrape.get("/api/ha/stream").status_code == 403


def test_the_newest_map_version_is_named(tmp_path):
    store = Store(str(tmp_path))
    assert store.latest_sha("map") is None
    store.save_version("map", {"features": [1]})
    store.save_version("map", {"features": [1, 2]})
    assert store.latest_sha("map") == store.versions("map")[0]["sha"]
