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

    def robot_status(self):
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
    ("PUT", "/api/settings/wifi_survey", "on"),
    ("POST", "/api/robot/account", {"sn": "YB0000DEMO0002"}),
    ("POST", "/api/mcp/token", None),
    ("POST", "/api/cloud/login", {"username": "a", "password": "b"}),
    ("POST", "/api/robot/account", {"sn": "YB0000DEMO0002"}),
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


def test_deck_width_is_fixed_per_head_not_a_setting(client):
    signed_in(client)
    client.store.set_setting("deck_m", 0.9)                # left behind by an older version
    assert client.c.put("/api/settings/deck_m", json=0.5).status_code == 404
    from app import coverage
    from app.main import SETTINGS_DEFAULTS
    assert "deck_m" not in SETTINGS_DEFAULTS                # so GET /api/settings no longer lists it
    from app.recorder import Recorder
    assert Recorder.deck_m(3) == Recorder.deck_m(5) == coverage.DEFAULT_DECK_M[3]
    assert Recorder.deck_m(1) == coverage.DEFAULT_DECK_M[1] != 0.9


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


# -- metrics-only tokens (scope) ---------------------------------------------------------------------------------
def _scoped(client, scope=None, name="scrape"):
    if not client.store.setting("auth"):                                # set the password once
        signed_in(client)
    body = {"name": name} if scope is None else {"name": name, "scope": scope}
    r = client.c.post("/api/auth/tokens", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_token_without_a_scope_is_full(client):
    signed_in(client)
    old = {"id": "aa11", "name": "old", "sha256": auth._sha("gk_old"), "hint": "_old", "created": 1.0, "used": None}
    client.store.set_setting("api_tokens", [old])                      # a row from before scopes: no scope field
    assert client.c.get("/api/auth/tokens").json()["tokens"][0]["scope"] == "full"
    bot = _bot("gk_old")
    assert bot.get("/api/events").status_code == 200
    assert client.auth.token_scope("Bearer gk_old") == "full"


def test_create_with_each_scope_and_refuse_an_invalid_one(client):
    assert _scoped(client)["scope"] == "full"                           # default when absent
    assert _scoped(client, "full", "b")["scope"] == "full"
    assert _scoped(client, "metrics", "c")["scope"] == "metrics"
    for bad in ("admin", "", "METRICS", None, 5, ["metrics"], {"a": 1}):
        r = client.c.post("/api/auth/tokens", json={"name": "bad", "scope": bad})
        assert r.status_code == 400, (bad, r.text)
    stored = (client.store.setting("api_tokens") or []) + (client.store.setting("api_tokens_metrics") or [])
    assert len(stored) == 3                                             # the refused ones made nothing
    listed = {t["name"]: t["scope"] for t in client.c.get("/api/auth/tokens").json()["tokens"]}
    assert listed == {"scrape": "full", "b": "full", "c": "metrics"}


def test_a_metrics_token_is_shown_once_and_only_its_hash_is_kept(client):
    made = _scoped(client, "metrics")
    assert made["token"].startswith("gk_")
    assert made["token"] not in str(client.store.setting("api_tokens_metrics"))
    listed = client.c.get("/api/auth/tokens").json()["tokens"][0]
    assert "token" not in listed and "sha256" not in listed and listed["scope"] == "metrics"
    assert made["token"] not in client.c.get("/api/auth/tokens").text
    assert client.store.setting("api_tokens_metrics")[0]["sha256"] == auth._sha(made["token"])


def test_a_metrics_token_is_not_stored_where_an_older_panel_would_read_it_as_full(client):
    """Rolling back to an image from before scopes must not turn a metrics token into a full one. That code reads only
    `api_tokens` and matches on `sha256`, ignoring any `scope`."""
    full = _scoped(client, None, "full")
    metrics = _scoped(client, "metrics", "prom")
    old_rows = client.store.setting("api_tokens")
    assert [t["id"] for t in old_rows] == [full["id"]]
    assert all(t.get("scope") is None for t in client.store.setting("api_tokens_metrics"))
    assert not [t for t in old_rows if t.get("sha256") == auth._sha(metrics["token"])]

    def old_token_ok(header):                           # the matching as it was before scopes
        want = auth._sha(header[7:].strip())
        return any(auth._same(t.get("sha256", ""), want) for t in client.store.setting("api_tokens") or [])

    assert old_token_ok(f"Bearer {full['token']}")
    assert not old_token_ok(f"Bearer {metrics['token']}")


def test_listing_revoking_and_using_a_metrics_token(client):
    full, metrics = _scoped(client, None, "full"), _scoped(client, "metrics", "prom")
    bot = _bot(metrics["token"])
    assert bot.get("/api/metrics").status_code not in (401, 403)
    assert client.store.setting("api_tokens_metrics")[0]["used"]               # last used is kept on the right row
    assert client.store.setting("api_tokens")[0]["used"] is None
    assert [t["name"] for t in client.c.get("/api/auth/tokens").json()["tokens"]] == ["full", "prom"]
    assert client.c.delete(f"/api/auth/tokens/{metrics['id']}").status_code == 200
    assert client.store.setting("api_tokens_metrics") == []
    assert _bot(metrics["token"]).get("/api/metrics").status_code == 401
    assert _bot(full["token"]).get("/api/events").status_code == 200
    assert client.c.delete(f"/api/auth/tokens/{metrics['id']}").status_code == 404


def test_reset_clears_the_metrics_tokens_too(client, tmp_path, monkeypatch):
    _scoped(client, "metrics")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert auth.main(["app.auth", "reset"]) == 0
    assert client.store.setting("api_tokens_metrics") is None


@pytest.mark.parametrize("scope", ["Metrics", "metrics ", "admin", "", ["full"], {}, 5, True, "metrics"])
def test_a_stored_scope_that_is_not_full_is_refused_not_read_as_full(client, scope):
    """In `api_tokens` only a missing, null or "full" scope is full. Anything else (even "metrics", which belongs in the
    other store) makes the token invalid: 401, never full access."""
    signed_in(client)
    row = {"id": "bb22", "name": "odd", "sha256": auth._sha("gk_odd"), "hint": "_odd", "created": 1.0, "used": None,
           "scope": scope}
    client.store.set_setting("api_tokens", [row])
    assert client.auth.token_scope("Bearer gk_odd") is None
    assert _bot("gk_odd").get("/api/events").status_code == 401
    assert "odd" not in [t["name"] for t in client.c.get("/api/auth/tokens").json()["tokens"]]


@pytest.mark.parametrize("scope", [None, "full"])
def test_a_stored_scope_of_none_or_full_is_full(client, scope):
    signed_in(client)
    row = {"id": "cc33", "name": "ok", "sha256": auth._sha("gk_ok"), "hint": "_ok_", "created": 1.0, "used": None,
           "scope": scope}
    client.store.set_setting("api_tokens", [row])
    assert client.auth.token_scope("Bearer gk_ok") == "full"


@pytest.mark.parametrize("scope", ["metrics ", "Metrics", "full", ["metrics"], 1])
def test_a_metrics_row_with_a_scope_other_than_none_is_refused(client, scope):
    signed_in(client)
    row = {"id": "dd44", "name": "m", "sha256": auth._sha("gk_m"), "hint": "_m__", "created": 1.0, "used": None,
           "scope": scope}
    client.store.set_setting("api_tokens_metrics", [row])
    assert client.auth.token_scope("Bearer gk_m") is None


def test_one_hash_in_two_rows_gets_the_narrower_scope(client):
    signed_in(client)
    row = {"id": "ee55", "name": "dup", "sha256": auth._sha("gk_dup"), "hint": "_dup", "created": 1.0, "used": None}
    client.store.set_setting("api_tokens", [row])
    client.store.set_setting("api_tokens_metrics", [{**row, "id": "ff66"}])
    assert client.auth.token_scope("Bearer gk_dup") == "metrics"
    assert _bot("gk_dup").get("/api/events").status_code == 403


def test_a_new_token_id_is_never_one_the_other_store_has(client, monkeypatch):
    signed_in(client)
    first = _scoped(client, "metrics", "m1")
    ids = iter([first["id"], first["id"], "aaaa1111"])                  # a collision, twice, then a free id
    monkeypatch.setattr(auth.secrets, "token_hex", lambda n: next(ids))
    second = _scoped(client, None, "f1")
    assert second["id"] == "aaaa1111"
    client.c.delete(f"/api/auth/tokens/{second['id']}")
    assert [t["name"] for t in client.c.get("/api/auth/tokens").json()["tokens"]] == ["m1"]    # the metrics one stayed


@pytest.mark.parametrize("junk", [None, "oops", 7, ["x"]])
def test_a_row_that_is_not_a_dict_is_skipped_not_a_500(client, junk):
    signed_in(client)
    good = _scoped(client, None, "good")
    metrics = _scoped(client, "metrics", "scrape")
    for key in ("api_tokens", "api_tokens_metrics"):
        client.store.set_setting(key, [junk] + client.store.setting(key))
    assert _bot(good["token"]).get("/api/events").status_code == 200                # the bearer gate still answers
    assert _bot(metrics["token"]).get("/api/metrics").status_code != 500
    assert _bot("gk_unknown").get("/api/events").status_code == 401
    assert client.auth.token_scope(f"Bearer {good['token']}") == "full"
    assert [t["name"] for t in client.c.get("/api/auth/tokens").json()["tokens"]] == ["good", "scrape"]
    assert client.c.delete(f"/api/auth/tokens/{metrics['id']}").status_code == 200   # revoking walks past the junk
    assert client.auth.token_scope(f"Bearer {metrics['token']}") is None


@pytest.mark.parametrize("bad_id", [[], {}, 7, None])
def test_a_row_with_an_unhashable_id_does_not_break_creating_a_token(client, bad_id):
    signed_in(client)
    for key in ("api_tokens", "api_tokens_metrics"):
        client.store.set_setting(key, [{"id": bad_id, "name": "odd"}] + (client.store.setting(key) or []))
    assert client.c.post("/api/auth/tokens", json={"name": "new"}).status_code == 200
    assert client.c.post("/api/auth/tokens", json={"name": "new2", "scope": "metrics"}).status_code == 200


def test_creating_a_metrics_token_says_so_in_the_event_log(client):
    _scoped(client, "metrics", "prom")
    _scoped(client, None, "ha")
    msgs = str(client.store.events(50))
    assert "API token 'prom' (metrics only) was created" in msgs
    assert "API token 'ha' was created" in msgs


METRICS_REFUSED = [
    ("GET", "/api/status"), ("GET", "/api/settings"), ("GET", "/api/events"), ("GET", "/api/auth/tokens"),
    ("GET", "/tiles/0/0/0.png"), ("GET", "/imagery/anything"), ("GET", "/api/stream"),
    ("POST", "/api/control/send"), ("POST", "/api/control/preview"), ("POST", "/api/video/token"),
    ("GET", "/api/metrics/x"), ("GET", "/api/metricsfoo"), ("GET", "/api/metrics/"), ("GET", "/api/Metrics"),
    ("POST", "/api/metrics"), ("PUT", "/api/metrics"), ("DELETE", "/api/metrics"), ("OPTIONS", "/api/metrics"),
]


@pytest.mark.parametrize("method, path", METRICS_REFUSED)
def test_a_metrics_token_is_refused_everywhere_but_the_metrics_read(client, controller, method, path):
    bot = _bot(_scoped(client, "metrics")["token"])
    r = bot.request(method, path, json={"cmd": "pause", "confirm": True} if method == "POST" else None,
                    headers={"X-Panel-Request": "1"})
    assert r.status_code == 403, (method, path, r.status_code)
    assert "metrics" in r.json()["detail"]
    assert controller.calls == []                                       # nothing reached the controller


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_a_metrics_token_passes_the_gate_on_the_metrics_read(client, method):
    bot = _bot(_scoped(client, "metrics")["token"])
    r = bot.request(method, "/api/metrics")
    assert r.status_code not in (401, 403)                              # 404 until the endpoint exists


def test_a_metrics_token_is_limited_even_beside_a_signed_in_cookie(client):
    token = _scoped(client, "metrics")["token"]                         # client.c also holds the session cookie
    r = client.c.get("/api/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_a_metrics_token_never_acts_as_a_token_or_a_user(client, controller):
    token = _scoped(client, "metrics")["token"]
    assert client.auth.who("GET", "/api/metrics", {"authorization": f"Bearer {token}"}, {}) == "metrics"
    _bot(token).post("/api/control/send", json={"cmd": "pause", "confirm": True}, headers={"X-Panel-Request": "1"})
    assert controller.calls == []
    odd = SimpleNamespace(state=SimpleNamespace(who="metrics"))
    assert main._actor(odd) == "token"                  # never reached; the strict actor if it were


def test_a_full_token_is_unchanged(client, controller):
    for scope in (None, "full"):
        bot = _bot(_scoped(client, scope, f"f{scope}")["token"])
        assert bot.get("/api/events").status_code == 200
        assert bot.get("/api/metrics").status_code not in (401, 403)
        assert bot.put("/api/settings/control_enabled", json=True).status_code == 403
        controller.calls.clear()
        assert bot.post("/api/control/send", json={"cmd": "pause", "confirm": True}).status_code == 200
        assert controller.calls == [("send", "token")]
