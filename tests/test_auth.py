"""Sign-in (app/auth.py): the gate in front of the API, first-run setup with the code from the log, sessions,
API tokens, the cross-site check, the throttle, PANEL_AUTH=off and the reset command. Driven through the real app's
middleware with a fake recorder; no robot, no network."""
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.store import Store


class FakeRec:
    def __init__(self, store):
        self.store = store

    def status(self):
        return {"snapshot_age_s": 3, "run": None, "plan": {"active": True}}

    def local_status(self):
        return {"connected": True}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("PANEL_AUTH", raising=False)
    store = Store(str(tmp_path))
    gate = auth.Auth(store)
    gate.announce()
    old = {k: getattr(main.app.state, k, None) for k in ("auth", "rec")}
    main.app.state.auth, main.app.state.rec = gate, FakeRec(store)
    with_host = TestClient(main.app, base_url="http://panel.test")      # no `with`: the lifespan (recorder) never runs
    yield SimpleNamespace(c=with_host, auth=gate, store=store)
    for k, v in old.items():
        setattr(main.app.state, k, v)


def signed_in(t, password="correct horse"):
    r = t.c.post("/api/auth/setup", json={"code": t.auth.setup_code, "password": password})
    assert r.status_code == 200, r.text
    return r


def test_everything_with_data_is_locked(client):
    for path in ("/api/status", "/api/settings", "/api/stream", "/api/docs", "/tiles/world/1/0/0.jpg", "/imagery/yard.jpg"):
        assert client.c.get(path).status_code == 401, path
    r = client.c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login.html"


def test_health_and_sign_in_page_stay_open(client):
    h = client.c.get("/health").json()
    assert h["ok"] and h["busy"] is True                      # deploy's "not mid-mow" check needs no sign-in
    assert client.c.get("/api/auth/status").json() == {"disabled": False, "password_set": False, "signed_in": False,
                                                       "min_password": auth.MIN_PASSWORD, "password": True,
                                                       "oidc": None}
    assert not auth.needs_auth("/login.html") and not auth.needs_auth("/brand/favicon.svg")
    assert not auth.needs_auth("/mcp")                        # the MCP server checks its own token


def test_first_run_needs_the_code_from_the_log(client):
    assert client.auth.setup_code
    bad = client.c.post("/api/auth/setup", json={"code": "AAAA-BBBB-CCCC", "password": "correct horse"})
    assert bad.status_code == 403 and not client.auth.has_password()
    short = client.c.post("/api/auth/setup", json={"code": client.auth.setup_code, "password": "short"})
    assert short.status_code == 400
    r = signed_in(client)
    assert auth.COOKIE in r.cookies and client.auth.setup_code is None
    assert client.c.get("/api/auth/tokens").status_code == 200
    again = client.c.post("/api/auth/setup", json={"code": "x", "password": "another one"})
    assert again.status_code == 403                           # only once
    stored = client.store.setting("auth")
    assert "correct horse" not in str(stored) and stored["hash"]


def test_login_logout_and_the_throttle(client):
    signed_in(client)
    client.c.post("/api/auth/logout")
    assert client.c.get("/api/auth/tokens").status_code == 401
    for _ in range(auth.MAX_FAILS):
        assert client.c.post("/api/auth/login", json={"password": "nope nope"}).status_code == 403
    assert client.c.post("/api/auth/login", json={"password": "correct horse"}).status_code == 429   # waits even when right
    client.auth._fails.clear()
    assert client.c.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    assert client.c.get("/api/auth/tokens").status_code == 200


def test_api_tokens(client):
    signed_in(client)
    made = client.c.post("/api/auth/tokens", json={"name": "Home Assistant"}).json()
    assert made["token"].startswith("gk_") and made["name"] == "Home Assistant"
    listed = client.c.get("/api/auth/tokens").json()["tokens"]
    assert listed[0]["hint"] == made["token"][-4:] and "sha256" not in listed[0] and "token" not in listed[0]
    assert made["token"] not in str(client.store.setting("api_tokens"))

    bot = TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {made['token']}"})
    assert bot.get("/api/auth/tokens").status_code == 200
    assert bot.post("/api/auth/tokens", json={"name": "more"}).status_code == 403          # a token cannot mint tokens
    assert bot.post("/api/auth/password", json={"current": "x", "password": "yyyyyyyy"}).status_code == 403
    assert TestClient(main.app, headers={"Authorization": "Bearer gk_wrong"}).get("/api/auth/tokens").status_code == 401

    assert client.c.delete(f"/api/auth/tokens/{made['id']}").status_code == 200
    assert bot.get("/api/auth/tokens").status_code == 401


def _token(client):
    signed_in(client)
    return client.c.post("/api/auth/tokens", json={"name": "script"}).json()["token"]


def _bot(token):
    return TestClient(main.app, base_url="http://panel.test", headers={"Authorization": f"Bearer {token}"})


CLOSED_TO_TOKENS = [
    ("PUT", "/api/settings/control_enabled", True),
    ("PUT", "/api/settings/map_write_enabled", True),
    ("PUT", "/api/settings/mcp_control_enabled", True),
    ("PUT", "/api/settings/gates", {"guard_resume": True}),
    ("PUT", "/api/settings/robot_route", "cloud"),
    ("PUT", "/api/integrations/robot", {"host": "192.0.2.20"}),
    ("POST", "/api/mcp/token", None),
    ("POST", "/api/cloud/login", {"username": "a", "password": "b"}),
    ("POST", "/api/studio/draft/apply", {}),
    ("POST", "/api/schedule/pending/confirm", None),
    ("DELETE", "/api/studio/overlay", None),
    ("POST", "/api/a/route/added/later", {}),            # closed by default, not by being listed somewhere
]


@pytest.mark.parametrize("method, path, body", CLOSED_TO_TOKENS)
def test_a_token_cannot_change_anything_but_ask_for_commands(client, method, path, body):
    bot = _bot(_token(client))
    settings = lambda: [tuple(row) for row in client.store.db.execute("SELECT k, v FROM settings WHERE k != 'api_tokens' ORDER BY k")]   # noqa: E731
    before = settings()
    r = bot.request(method, path, json=body, headers={"X-Panel-Request": "1"})
    assert r.status_code == 403, r.text
    assert "password" in r.json()["detail"]
    assert settings() == before


def test_a_token_can_still_read(client):
    bot = _bot(_token(client))
    assert bot.get("/api/auth/tokens").status_code == 200
    assert bot.get("/api/events").status_code == 200


def test_a_person_can_still_change_settings(client):
    signed_in(client)
    r = client.c.put("/api/settings/control_enabled", json=True)
    assert r.status_code == 200 and client.store.setting("control_enabled") is True


def test_a_token_is_limited_even_beside_a_signed_in_cookie(client):
    token = _token(client)                               # client.c also holds the session cookie
    r = client.c.put("/api/settings/control_enabled", json=True, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and client.store.setting("control_enabled") is None


class RecordingController:
    """Says who the route named as the actor. Sends nothing."""

    def __init__(self):
        self.calls = []

    def enabled(self):
        return False

    async def preview(self, cmd, args, actor="user"):
        self.calls.append(("preview", actor))
        return {"cmd": cmd, "would_send": False}

    async def send(self, cmd, args=None, actor="user", override=False, wait=True):
        self.calls.append(("send", actor))
        return {"sent": False, "cmd": cmd}


@pytest.fixture
def controller(monkeypatch):
    fake = RecordingController()
    monkeypatch.setattr(main.app.state, "ops", SimpleNamespace(controller=fake), raising=False)
    return fake


def test_a_token_asks_for_commands_as_a_token_and_a_person_as_a_user(client, controller):
    bot = _bot(_token(client))
    for who, actor in ((bot, "token"), (client.c, "user")):
        controller.calls.clear()
        assert who.post("/api/control/preview", json={"cmd": "pause"}).status_code == 200
        assert who.post("/api/control/send", json={"cmd": "pause", "confirm": True}).status_code == 200
        assert controller.calls == [("preview", actor), ("send", actor)]


def test_with_sign_in_off_the_panel_treats_callers_as_before(client, controller, monkeypatch):
    monkeypatch.setenv("PANEL_AUTH", "off")
    assert client.c.post("/api/control/send", json={"cmd": "pause", "confirm": True}).status_code == 200
    assert controller.calls == [("send", "user")]


def test_cross_site_changes_are_refused(client):
    signed_in(client)
    evil = client.c.post("/api/auth/tokens", json={"name": "x"}, headers={"Origin": "http://evil.example"})
    assert evil.status_code == 403
    own = client.c.post("/api/auth/tokens", json={"name": "x"}, headers={"Origin": "http://panel.test"})
    assert own.status_code == 200
    assert client.c.get("/api/auth/tokens", headers={"Origin": "http://evil.example"}).status_code == 200   # reads: CORS


def test_password_change_signs_out_everyone_else(client):
    signed_in(client)
    other = TestClient(main.app, base_url="http://panel.test")
    assert other.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    assert client.c.post("/api/auth/password", json={"current": "wrong one", "password": "new password"}).status_code == 403
    assert client.c.post("/api/auth/password", json={"current": "correct horse", "password": "new password"}).status_code == 200
    assert client.c.get("/api/auth/tokens").status_code == 200          # this browser got a fresh session
    assert other.get("/api/auth/tokens").status_code == 401
    assert client.auth.check_password("new password") and not client.auth.check_password("correct horse")


def test_internal_calls_from_the_mcp_server_pass(client):
    signed_in(client)
    inner = TestClient(main.app, headers={auth.INTERNAL_HEADER: auth.INTERNAL})
    assert inner.get("/api/auth/tokens").status_code == 200
    assert inner.post("/api/auth/tokens", json={"name": "x"}).status_code == 403   # but it is not a person
    assert TestClient(main.app, headers={auth.INTERNAL_HEADER: "guess"}).get("/api/auth/tokens").status_code == 401


def test_sessions_expire(client):
    signed_in(client)
    sessions = client.store.setting("auth_sessions")
    for s in sessions.values():
        s["created"] = time.time() - auth.SESSION_DAYS * 86400 - 1     # in use or not, a session ends with its cookie
    client.store.set_setting("auth_sessions", sessions)
    assert client.c.get("/api/auth/tokens").status_code == 401


def test_panel_auth_off(client, monkeypatch):
    monkeypatch.setenv("PANEL_AUTH", "off")
    assert client.c.get("/api/auth/tokens").status_code == 200
    assert client.c.get("/api/auth/status").json()["disabled"] is True


def test_reset_command(client, tmp_path, monkeypatch, capsys):
    signed_in(client)
    client.c.post("/api/auth/tokens", json={"name": "x"})
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert auth.main(["app.auth", "reset"]) == 0
    assert client.store.setting("auth") is None and client.store.setting("api_tokens") is None
    assert client.c.get("/api/auth/tokens").status_code == 401
    assert auth.main(["app.auth"]) == 2


# -- review fixes ---------------------------------------------------------------------------------------------------
def test_the_gate_marks_its_own_401(client):
    r = client.c.get("/api/status")
    assert r.status_code == 401 and r.headers["X-GK-Auth"] == "required"      # the pages only sign out on this one
    assert client.c.get("/api/openapi.json").status_code == 401
    assert client.c.get("/openapi.json").status_code != 200


def test_signed_snapshot_links(client):
    path = "/api/frigate/snapshot/front.jpg"
    signed = client.auth.sign_path(path)
    p, q = signed.split("?")
    assert client.auth.who("GET", p, {}, {}, q) == "signed"
    assert client.auth.who("POST", p, {}, {}, q) is None                         # a link only reads
    assert client.auth.who("GET", "/api/frigate/snapshot/back.jpg", {}, {}, q) is None   # one path only
    assert client.auth.who("GET", p, {}, {}, q.replace("sig=", "sig=0")) is None
    assert client.auth.who("GET", p, {}, {}, f"exp={int(time.time()) - 1}&sig=x") is None
    with pytest.raises(ValueError):
        client.auth.sign_path("/api/status")


def test_odd_input_is_refused_not_a_crash(client):
    assert client.c.post("/api/auth/setup", json={"code": "é", "password": "correct horse"}).status_code == 403
    assert client.c.post("/api/auth/setup", json={"code": 123, "password": "correct horse"}).status_code == 403
    assert TestClient(main.app, headers={auth.INTERNAL_HEADER: "é".encode("latin-1")}).get("/api/auth/tokens").status_code == 401
    signed_in(client)
    assert client.c.post("/api/auth/login", json={"password": 12345678}).status_code == 403


def test_origin_behind_a_proxy(client):
    h = {"origin": "https://mow.example.org", "host": "groundskeeper:8425"}
    assert not auth.same_origin("POST", h)
    assert auth.same_origin("POST", h, "https://mow.example.org")                 # PANEL_URL names it
    assert auth.same_origin("POST", {**h, "x-forwarded-host": "MOW.example.org, inner"})
    assert not auth.same_origin("POST", {"origin": "null", "host": "x"})


def test_password_change_is_throttled(client):
    signed_in(client)
    for _ in range(auth.MAX_FAILS):
        assert client.c.post("/api/auth/password", json={"current": "nope nope", "password": "new password"}).status_code == 403
    assert client.c.post("/api/auth/password", json={"current": "correct horse", "password": "new password"}).status_code == 429


def test_no_gate_yet_fails_closed(client):
    main.app.state.auth = None
    assert client.c.get("/api/auth/tokens").status_code == 503
    assert client.c.get("/login.html").status_code in (200, 404)
