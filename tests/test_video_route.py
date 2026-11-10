"""POST /api/video/token and the robot_video_enabled setting (§13): off by default, people only, and no secret in a
log record, an event or a header it should not be in. Driven through the real app's middleware with a fake recorder,
cloud and HTTP client; no robot, no network."""
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, main, mcp_server
from app.store import Store
from tests.fakehttp import FakeHTTP, FakeResponse
from tests.test_video import APP_ID, JWT, KEY, SALT, SN, TOKEN, FakeCloud

BASE = "https://robot-service.invalid"


class FakeRec:
    def __init__(self, store):
        self.store = store


@pytest.fixture
def t(tmp_path, monkeypatch):
    monkeypatch.delenv("PANEL_AUTH", raising=False)
    store = Store(str(tmp_path))
    gate = auth.Auth(store)
    gate.announce()
    http = FakeHTTP(answer_post=lambda u, j, h: FakeResponse(
        200, json.dumps({"code": "00000", "data": {"token": TOKEN, "key": KEY, "salt": SALT}}).encode(),
        "application/json"))
    names = ("auth", "rec", "cfg", "ops", "video_http")
    old = {k: getattr(main.app.state, k, None) for k in names}
    cfg = SimpleNamespace(robot_service_url=BASE)
    main.app.state.auth, main.app.state.rec, main.app.state.cfg = gate, FakeRec(store), cfg
    main.app.state.ops, main.app.state.video_http = SimpleNamespace(cloud=FakeCloud()), http
    c = TestClient(main.app, base_url="http://panel.test")
    r = c.post("/api/auth/setup", json={"code": gate.setup_code, "password": "correct horse"})
    assert r.status_code == 200, r.text
    yield SimpleNamespace(c=c, auth=gate, store=store, http=http, cfg=cfg)
    for k, v in old.items():
        setattr(main.app.state, k, v)


def switch(t, on=True):
    r = t.c.put("/api/settings/robot_video_enabled", json=on)
    assert r.status_code == 200, r.text


def ask(t, camera="front", client=None):
    return (client or t.c).post("/api/video/token", json={"camera": camera})


def test_it_is_off_by_default_so_a_person_is_refused(t):
    assert main.SETTINGS_DEFAULTS["robot_video_enabled"] is False
    r = ask(t)
    assert r.status_code == 403 and "off" in r.json()["detail"]
    assert t.http.posts == []


def test_a_person_gets_the_join_details_and_nothing_more(t):
    switch(t)
    r = ask(t, "left")
    assert r.status_code == 200
    got = r.json()
    assert set(got) == {"app_id", "channel", "token", "uid", "key", "salt"}
    assert (got["app_id"], got["channel"], got["token"], got["key"], got["salt"]) == (
        APP_ID, f"{SN}_left_image", TOKEN, KEY, SALT)
    assert isinstance(got["uid"], int) and got["uid"] > 1
    assert r.headers["cache-control"] == "no-store"
    assert t.http.posts[0]["json"]["uid"] == got["uid"]
    assert t.http.posts[0]["url"].startswith(BASE)


def test_the_viewer_uid_is_chosen_by_the_server_and_never_zero_or_one(t):
    switch(t)
    uids = {t.c.post("/api/video/token", json={"camera": "front", "uid": 1}).json()["uid"] for _ in range(20)}
    assert min(uids) >= 2 and len(uids) > 1


@pytest.mark.parametrize("body", [{}, {"camera": "up"}, {"camera": 3}, {"camera": ["front"]}])
def test_an_unknown_camera_is_a_400(t, body):
    switch(t)
    assert t.c.post("/api/video/token", json=body).status_code == 400
    assert t.http.posts == []


def test_an_api_token_is_refused_even_with_the_setting_on(t):
    switch(t)
    made = t.c.post("/api/auth/tokens", json={"name": "script"}).json()["token"]
    bot = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {made}"})
    assert ask(t, client=bot).status_code == 403
    assert bot.put("/api/settings/robot_video_enabled", json=False).status_code == 403
    assert ("POST", "/api/video/token") not in auth.TOKEN_WRITES
    assert t.http.posts == []
    assert ask(t).status_code == 200                       # the person still can


def test_the_setting_needs_a_person_and_a_bool(t):
    bad = t.c.put("/api/settings/robot_video_enabled", json="yes")
    assert bad.status_code == 400 and t.store.setting("robot_video_enabled") is None
    anon = TestClient(main.app, base_url="http://panel.test")
    assert anon.put("/api/settings/robot_video_enabled", json=True).status_code == 401
    assert anon.post("/api/video/token", json={"camera": "front"}).status_code == 401


def test_the_mcp_server_cannot_reach_it(t):
    switch(t)
    assert not mcp_server.allowed("POST", "/api/video/token")
    assert not mcp_server.allowed("PUT", "/api/settings/robot_video_enabled")
    inner = TestClient(main.app, base_url="http://panel", headers={auth.INTERNAL_HEADER: auth.INTERNAL})
    assert inner.post("/api/video/token", json={"camera": "front"}).status_code == 403     # no person's session
    assert t.http.posts == []


def test_without_an_address_it_says_so(t):
    switch(t)
    t.cfg.robot_service_url = None
    r = ask(t)
    assert r.status_code == 503 and "not configured" in r.json()["detail"]
    assert t.http.posts == []


def test_a_signed_out_cloud_is_a_clear_409(t):
    switch(t)
    main.app.state.ops.cloud.sdk.token = None
    r = ask(t)
    assert r.status_code == 409 and "Yarbo cloud" in r.json()["detail"]


def test_a_service_failure_is_a_502_with_a_fixed_phrase(t):
    switch(t)
    t.http.answer_post = lambda u, j, h: FakeResponse(200, json.dumps({"code": "B1234", "message": KEY}).encode(),
                                                      "application/json")
    r = ask(t)
    assert r.status_code == 502 and "B1234" in r.json()["detail"] and KEY not in r.text


def test_no_secret_is_logged_or_recorded_as_an_event(t, caplog):
    caplog.set_level(logging.DEBUG)
    switch(t)
    r = ask(t)
    assert r.status_code == 200
    channel = r.json()["channel"]
    logs = caplog.text + "\n".join(x.getMessage() for x in caplog.records)
    events = json.dumps([tuple(row) for row in t.store.db.execute("SELECT * FROM events")], default=str)
    stored = json.dumps([tuple(row) for row in t.store.db.execute("SELECT k, v FROM settings")], default=str)
    for secret in (TOKEN, KEY, SALT, APP_ID, JWT, BASE, channel):
        assert secret not in logs, secret
        assert secret not in events, secret
        assert secret not in stored, secret
    assert "robot video switched ON" in events


def test_the_address_is_an_integration_setting_applied_without_a_restart(tmp_path):
    from app.runtime import Runtime
    from tests.test_settings_api import env
    store = Store(str(tmp_path))
    rt = Runtime(env(tmp_path), store)
    assert rt.robot_service_url is None                                  # no default: unset means "not configured"
    assert rt.set("video", {"service_url": BASE + "/"}) == ["service_url"]
    assert rt.robot_service_url == BASE                                  # live, trailing slash dropped
    assert rt.describe()["video"]["service_url"]["env_name"] == "YARBO_ROBOT_SERVICE_URL"
    with pytest.raises(ValueError):
        rt.set("video", {"service_url": "robot-service"})
    assert Runtime(env(tmp_path), store).robot_service_url == BASE       # survives a restart
    assert rt.set("video", {"service_url": None}) == ["service_url"] and rt.robot_service_url is None
    demo = Runtime(env(tmp_path, demo=True), store)
    with pytest.raises(ValueError):
        demo.set("video", {"service_url": BASE})                         # a demo instance has no real service


def test_the_address_can_be_switched_off_over_the_environment(tmp_path):
    from app.runtime import Runtime
    from tests.test_settings_api import env
    rt = Runtime(env(tmp_path, robot_service_url=BASE), Store(str(tmp_path)))
    assert rt.robot_service_url == BASE
    assert rt.set("video", {"service_url": False}) == ["service_url"] and not rt.robot_service_url
