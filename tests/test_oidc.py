"""Sign-in through an OpenID Connect provider (app/oidc.py), driven through the real app's routes and middleware.
The provider is a fake inside httpx's MockTransport: synthetic RSA keys, a token endpoint that checks PKCE and the
client's secret, real signed ID tokens. No network, no robot."""
import asyncio
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote_plus, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from app import auth, main, oidc
from app.store import Store
from tests.test_auth import FakeRec

ISSUER = "https://idp.example.test/application/o/panel/"
CLIENT, SECRET = "panel-client", "panel-secret"
PANEL = "http://panel.test"
WEB = Path(__file__).resolve().parent.parent / "web"


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Provider:
    """Just enough of an OpenID provider. Tests change its fields to make it misbehave."""

    def __init__(self, secret=SECRET):
        self.secret = secret
        self.key, self.kid = _rsa(), "key-1"
        self.published = [(self.key, self.kid)]          # what /jwks shows; a kid of None publishes no kid
        self.sign_with = None                            # (key, kid, alg) override for the ID token
        self.algs = ["RS256"]                            # id_token_signing_alg_values_supported
        self.issuer = ISSUER                             # what discovery says the issuer is
        self.claims: dict = {}                           # overrides / additions to the ID token
        self.drop: set = set()                           # claims to leave out of the ID token
        self.userinfo: dict = {}
        self.userinfo_status = 200                       # 500: userinfo has a bad moment
        self.userinfo_reads = 0
        self.codes: dict = {}
        self.jwks_reads = 0
        self.token_requests: list = []

    def config(self):
        return {"issuer": self.issuer, "authorization_endpoint": "https://idp.example.test/authorize",
                "token_endpoint": "https://idp.example.test/token", "jwks_uri": "https://idp.example.test/jwks",
                "userinfo_endpoint": "https://idp.example.test/userinfo",
                "id_token_signing_alg_values_supported": self.algs,
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"]}

    def authorize(self, location: str) -> dict:
        """What the person's browser does at the provider: sign in, then go back with a code."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        assert q["client_id"] == CLIENT and q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = q
        return {"code": code, "state": q["state"]}

    def id_token(self, q: dict) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT, "sub": "user-1", "iat": now, "exp": now + 300, "nonce": q["nonce"],
                  "preferred_username": "tess", "email": "tess@example.org", "email_verified": True,
                  "groups": ["lawn admins"], **self.claims}
        for k in self.drop:
            claims.pop(k, None)
        key, kid, alg = self.sign_with or (self.key, self.kid, "RS256")
        return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid} if kid else None)

    def _client_ok(self, header) -> bool:
        """RFC 6749 2.3.1: id and secret each form-encoded, then base64 of id:secret."""
        if not header or not header.startswith("Basic "):
            return False
        cid, _, secret = base64.b64decode(header[6:]).decode().partition(":")
        return unquote_plus(cid) == CLIENT and unquote_plus(secret) == self.secret

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == ISSUER + ".well-known/openid-configuration":
            return httpx.Response(200, json=self.config())
        if url == "https://idp.example.test/jwks":
            self.jwks_reads += 1
            keys = []
            for key, kid in self.published:
                jwk = {**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "use": "sig", "alg": "RS256"}
                keys.append({**jwk, "kid": kid} if kid else jwk)
            return httpx.Response(200, json={"keys": keys})
        if url == "https://idp.example.test/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append({"form": form, "auth": request.headers.get("authorization")})
            q = self.codes.pop(form.get("code"), None)
            if not self._client_ok(request.headers.get("authorization")) or q is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
            if challenge.decode() != q["code_challenge"] or form["redirect_uri"] != q["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at-1", "token_type": "Bearer",
                                             "id_token": self.id_token(q)})
        if url == "https://idp.example.test/userinfo":
            assert request.headers.get("authorization") == "Bearer at-1"
            self.userinfo_reads += 1
            if self.userinfo_status != 200:
                return httpx.Response(self.userinfo_status)
            return httpx.Response(200, json={"sub": "user-1", **self.userinfo})
        return httpx.Response(404)


OIDC_ENV = {"OIDC_ISSUER": ISSUER, "OIDC_CLIENT_ID": CLIENT, "OIDC_CLIENT_SECRET": SECRET, "OIDC_NAME": "Authentik",
            "OIDC_ALLOWED_GROUPS": "lawn admins"}


def _env(monkeypatch, **values):
    for k in ("PANEL_AUTH", "PANEL_PASSWORD", "PANEL_URL", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET",
              "OIDC_NAME", "OIDC_SCOPES", "OIDC_ALLOWED_GROUPS", "OIDC_GROUPS_CLAIM", "OIDC_ALLOWED_DOMAINS",
              "OIDC_AUTO_LOGIN"):
        monkeypatch.delenv(k, raising=False)
    for k, v in values.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def panel(tmp_path, monkeypatch):
    def make(panel_url=PANEL, **env):
        _env(monkeypatch, **{**OIDC_ENV, **env})
        store = Store(str(tmp_path))
        gate = auth.Auth(store)
        gate.announce()
        provider = Provider(env.get("OIDC_CLIENT_SECRET", SECRET))
        settings = oidc.load(panel_url, auth.password_enabled(), auth.disabled())
        main.app.state.auth, main.app.state.rec = gate, FakeRec(store)
        main.app.state.oidc = oidc.OIDC(settings, gate._key,
                                        httpx.AsyncClient(transport=httpx.MockTransport(provider.handle)))
        return SimpleNamespace(c=TestClient(main.app, base_url=PANEL), auth=gate, store=store, idp=provider)
    old = {k: getattr(main.app.state, k, None) for k in ("auth", "rec", "oidc")}
    yield make
    for k, v in old.items():
        setattr(main.app.state, k, v)


def sign_in(p, next_path="/", tamper=None):
    """Start, sign in at the provider, come back. `tamper` may change what comes back."""
    start = p.c.get("/api/auth/oidc/start?" + urlencode({"next": next_path}), follow_redirects=False)
    assert start.status_code == 303, start.text
    back = p.idp.authorize(start.headers["location"])
    if tamper:
        back = tamper(back)
    return p.c.get("/api/auth/oidc/callback?" + urlencode(back), follow_redirects=False)


def cookie_attrs(header: str) -> set[str]:
    """The attribute names of one Set-Cookie header (Secure, HttpOnly, ...), lower-case; not its value."""
    return {part.split("=", 1)[0].strip().lower() for part in header.split(";")[1:]}


def signed_in(p) -> bool:
    return p.c.get("/api/auth/status").json()["signed_in"]


def refused(r) -> str:
    """The code the sign-in page is sent back with. Only a bare code from oidc.CODES may be in its address."""
    assert r.status_code == 303 and r.headers["location"].startswith("/login.html?error="), r.headers
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert set(q) == {"error"} and q["error"][0] in oidc.CODES, q
    return q["error"][0]


# -- the happy path --------------------------------------------------------------------------------------------------

def test_someone_in_an_allowed_group_signs_in_and_goes_where_they_were_headed(panel):
    p = panel()
    assert not signed_in(p)
    r = sign_in(p, "/#settings/access")
    assert r.status_code == 303 and r.headers["location"] == "/#settings/access"
    assert signed_in(p) and p.c.get("/api/auth/tokens").status_code == 200     # a person's session
    session = next(iter(p.store.setting("auth_sessions").values()))
    assert session["user"] == "tess" and session["sub"] == "user-1"
    assert any(e["detail"] == "tess signed in with Authentik (subject user-1)" for e in p.store.events())
    assert oidc.COOKIE not in p.c.cookies                                       # the flow cookie is spent


def test_the_provider_gets_pkce_and_the_client_secret(panel):
    p = panel()
    sign_in(p)
    sent = p.idp.token_requests[0]
    assert sent["auth"].startswith("Basic ") and "client_secret" not in sent["form"]
    assert len(sent["form"]["code_verifier"]) >= 43 and sent["form"]["redirect_uri"] == PANEL + oidc.CALLBACK


def test_a_client_secret_with_reserved_characters_is_form_encoded(panel):
    p = panel(OIDC_CLIENT_SECRET="p%ss:w+rd")
    assert sign_in(p).headers["location"] == "/" and signed_in(p)
    raw = base64.b64decode(p.idp.token_requests[0]["auth"][6:]).decode()
    assert raw == "panel-client:p%25ss%3Aw%2Brd"


@pytest.mark.parametrize("url, panel", [("https://panel.test:443", "https://panel.test"),
                                        ("http://Panel.Test:80/", "http://panel.test"),
                                        ("https://panel.test:8443", "https://panel.test:8443")])
def test_panel_url_is_normalised(monkeypatch, url, panel):
    _env(monkeypatch, **OIDC_ENV)
    assert oidc.load(url, True, False).panel == panel


def test_a_panel_url_with_credentials_is_refused(monkeypatch):
    _env(monkeypatch, **OIDC_ENV)
    with pytest.raises(ValueError, match="user name or password"):
        oidc.load("https://u:p@panel.test", True, False)


@pytest.mark.parametrize("url", ["http://panel.test/gk/", "http://panel.test/gk", "http://panel.test/?x=1"])
def test_a_panel_url_with_a_path_is_refused(monkeypatch, url):
    _env(monkeypatch, **OIDC_ENV)
    with pytest.raises(ValueError, match="no path"):
        oidc.load(url, True, False)
    assert oidc.load("http://panel.test/", True, False).redirect_uri == PANEL + oidc.CALLBACK


@pytest.mark.parametrize("bad", ["//evil.example", "https://evil.example/", "/\\evil.example", "evil",
                                 "/\t/evil.example", "/\n/evil.example", "/　/evil.example"])
def test_where_to_go_after_stays_in_this_panel(panel, bad):
    p = panel()
    assert sign_in(p, bad).headers["location"] == "/"


def test_groups_left_out_of_the_id_token_are_read_from_userinfo(panel):
    p = panel()
    p.idp.drop = {"groups"}
    p.idp.userinfo = {"groups": ["lawn admins"]}
    assert sign_in(p).status_code == 303 and signed_in(p)


def test_a_rotated_signing_key_is_fetched_once_more(panel):
    p = panel()
    sign_in(p)
    reads = p.idp.jwks_reads
    p.c.cookies.clear()
    new = _rsa()
    p.idp.published, p.idp.key, p.idp.kid = [(new, "key-2")], new, "key-2"
    assert sign_in(p).status_code == 303 and signed_in(p)
    assert p.idp.jwks_reads == reads + 1


def test_a_provider_that_names_no_key_id_works_with_its_one_key(panel):
    p = panel()
    p.idp.published, p.idp.kid = [(p.idp.key, None)], None
    assert sign_in(p).headers["location"] == "/" and signed_in(p)


def test_a_verified_email_in_an_allowed_domain_is_let_in(panel):
    p = panel(OIDC_ALLOWED_GROUPS="", OIDC_ALLOWED_DOMAINS="Example.org")
    p.idp.drop = {"groups"}
    assert sign_in(p).status_code == 303 and signed_in(p)


# -- who is turned away ----------------------------------------------------------------------------------------------

def test_someone_outside_the_allowed_groups_is_refused_and_it_is_noted_once_a_minute(panel):
    p = panel()
    p.idp.claims = {"groups": ["neighbours"], "preferred_username": "the owner"}
    assert refused(sign_in(p)) == "not_allowed" and not signed_in(p)
    p.c.cookies.clear()
    assert refused(sign_in(p)) == "not_allowed"
    noted = [e["detail"] for e in p.store.events() if "not allowed tried to sign in" in e["detail"]]
    assert noted == ["an account at Authentik that is not allowed tried to sign in (subject user-1; "
                     "it calls itself 'the owner')"]


def test_a_verified_flag_for_another_address_does_not_vouch_for_this_one(panel):
    p = panel(OIDC_ALLOWED_GROUPS="", OIDC_ALLOWED_DOMAINS="example.org")
    p.idp.drop = {"groups", "email_verified"}                         # the ID token's address, no flag
    p.idp.userinfo = {"email": "someone@elsewhere.example", "email_verified": True}
    assert refused(sign_in(p)) == "not_allowed" and not signed_in(p)


@pytest.mark.parametrize("env, drop", [
    ({"OIDC_ALLOWED_GROUPS": "", "OIDC_ALLOWED_DOMAINS": "example.org"}, {"groups"}),   # the token's email settles it
    ({"OIDC_ALLOWED_DOMAINS": "example.org"}, {"email", "email_verified"}),            # the token's group settles it
])
def test_userinfo_is_not_read_when_the_id_token_already_settles_it(panel, env, drop):
    p = panel(**env)
    p.idp.drop = drop
    p.idp.userinfo_status = 500
    assert sign_in(p).headers["location"] == "/" and signed_in(p)
    assert p.idp.userinfo_reads == 0


def test_userinfo_is_read_when_the_token_lacks_what_decides(panel):
    p = panel()
    p.idp.drop = {"groups"}
    p.idp.userinfo_status = 500
    assert refused(sign_in(p)) == "provider" and not signed_in(p)
    assert p.idp.userinfo_reads == 1


@pytest.mark.parametrize("token, info", [
    ({"groups": [], "email": "tess@other.example"}, {"groups": ["lawn admins"]}),           # an empty list in the token is the answer
    ({"groups": None, "email": "tess@other.example"}, {"groups": ["lawn admins"]}),
    ({"groups": {"lawn admins": True}, "email": "tess@other.example"}, {"groups": ["lawn admins"]}),
    ({"groups": [], "email_verified": None}, {"email": "tess@example.org", "email_verified": True}),
])
def test_what_the_token_says_is_not_overruled_by_userinfo(panel, token, info):
    p = panel(OIDC_ALLOWED_DOMAINS="example.org")
    p.idp.claims, p.idp.userinfo = token, info
    assert refused(sign_in(p)) == "not_allowed" and not signed_in(p)
    assert p.idp.userinfo_reads == 0


def test_a_password_session_is_secure_on_the_https_panel_url_host_only(panel, monkeypatch):
    p = panel(panel_url="https://panel.test")
    monkeypatch.setattr(main.app.state, "cfg", SimpleNamespace(panel_url="https://panel.test"), raising=False)
    p.auth.set_password("correct horse")
    r = p.c.post("/api/auth/login", json={"password": "correct horse"})       # TLS proxy without X-Forwarded-Proto
    assert r.status_code == 200 and "secure" in cookie_attrs(r.headers["set-cookie"])
    lan = p.c.post("/api/auth/login", json={"password": "correct horse"}, headers={"host": "192.0.2.10:8425"})
    assert lan.status_code == 200 and "secure" not in cookie_attrs(lan.headers["set-cookie"])


def test_userinfo_can_confirm_the_same_address(panel):
    p = panel(OIDC_ALLOWED_GROUPS="", OIDC_ALLOWED_DOMAINS="example.org")
    p.idp.drop = {"groups", "email_verified"}
    p.idp.userinfo = {"email": "tess@example.org", "email_verified": True}
    assert sign_in(p).headers["location"] == "/" and signed_in(p)


def test_an_email_only_in_userinfo_comes_with_its_own_flag(panel):
    p = panel(OIDC_ALLOWED_GROUPS="", OIDC_ALLOWED_DOMAINS="example.org")
    p.idp.drop = {"groups", "email", "email_verified"}
    p.idp.userinfo = {"email": "tess@example.org", "email_verified": True}
    assert sign_in(p).headers["location"] == "/" and signed_in(p)
    p.c.cookies.clear()
    p.idp.claims = {"email_verified": True}                          # a flag in the token, the address elsewhere
    p.idp.drop = {"groups", "email"}
    p.idp.userinfo = {"email": "tess@example.org", "email_verified": False}
    assert refused(sign_in(p)) == "not_allowed"


def test_the_sign_in_button_stays_hidden_until_it_is_wanted():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.login \.btn\[hidden\][^{]*\{\s*display:\s*none", css)


@pytest.mark.parametrize("verified", [False, "true", None])
def test_only_an_email_the_provider_verified_counts(panel, verified):
    p = panel(OIDC_ALLOWED_GROUPS="", OIDC_ALLOWED_DOMAINS="example.org")
    p.idp.drop = {"groups"} | ({"email_verified"} if verified is None else set())
    if verified is not None:
        p.idp.claims = {"email_verified": verified}
    assert refused(sign_in(p)) == "not_allowed" and not signed_in(p)


def test_userinfo_about_someone_else_is_refused(panel):
    p = panel()
    p.idp.drop = {"groups"}
    p.idp.userinfo = {"sub": "user-2", "groups": ["lawn admins"]}
    assert refused(sign_in(p)) == "token" and not signed_in(p)


# -- what doesn't check out ------------------------------------------------------------------------------------------

def test_a_callback_with_another_state_is_refused_and_leaves_the_real_sign_in_alone(panel):
    p = panel()
    start = p.c.get("/api/auth/oidc/start", follow_redirects=False)
    back = p.idp.authorize(start.headers["location"])
    stray = p.c.get("/api/auth/oidc/callback?" + urlencode({**back, "state": "forged"}), follow_redirects=False)
    assert refused(stray) == "expired" and "set-cookie" not in stray.headers
    r = p.c.get("/api/auth/oidc/callback?" + urlencode(back), follow_redirects=False)
    assert r.headers["location"] == "/" and signed_in(p)


def test_a_provider_error_without_this_browser_s_sign_in_is_not_believed(panel):
    p = panel()
    p.c.get("/api/auth/oidc/start", follow_redirects=False)                        # a sign-in in progress
    lure = {"error": "server_error", "error_description": "Panel moved: sign in at https://evil.example"}
    r = p.c.get("/api/auth/oidc/callback?" + urlencode(lure), follow_redirects=False)
    assert refused(r) == "expired" and "set-cookie" not in r.headers               # the real flow survives
    assert "evil" not in r.headers["location"]


def test_the_provider_s_own_refusal_comes_back_as_a_code(panel):
    p = panel()
    said_no = {"error": "access_denied", "error_description": "Panel moved: sign in at https://evil.example"}
    r = sign_in(p, tamper=lambda b: {**said_no, "state": b["state"]})
    assert refused(r) == "denied" and not signed_in(p) and "evil" not in r.headers["location"]


def test_a_callback_without_the_flow_cookie_is_refused_and_not_logged_as_an_event(panel):
    p = panel()
    start = p.c.get("/api/auth/oidc/start", follow_redirects=False)
    back = p.idp.authorize(start.headers["location"])
    p.c.cookies.clear()
    assert refused(p.c.get("/api/auth/oidc/callback?" + urlencode(back), follow_redirects=False)) == "expired"
    assert not signed_in(p) and not any("Authentik" in e["detail"] for e in p.store.events())


def test_a_forged_flow_cookie_is_refused(panel):
    p = panel()
    start = p.c.get("/api/auth/oidc/start", follow_redirects=False)
    back = p.idp.authorize(start.headers["location"])
    flow = p.c.cookies.get(oidc.COOKIE)
    payload = json.loads(base64.urlsafe_b64decode(flow.split(".")[0] + "=="))
    payload["next"] = "//evil.example"
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode() + "." + flow.split(".")[1]
    p.c.cookies.clear()
    p.c.cookies.set(oidc.COOKIE, forged, domain="panel.test", path=oidc.COOKIE_PATH)
    assert refused(p.c.get("/api/auth/oidc/callback?" + urlencode(back), follow_redirects=False)) == "expired"
    p.c.cookies.clear()                                               # the genuine cookie, set the same way, works
    p.c.cookies.set(oidc.COOKIE, flow, domain="panel.test", path=oidc.COOKIE_PATH)
    assert p.c.get("/api/auth/oidc/callback?" + urlencode(back), follow_redirects=False).headers["location"] == "/"
    assert signed_in(p)


@pytest.mark.parametrize("change", [
    {"nonce": "another"},
    {"aud": "another-app"},
    {"iss": "https://idp.example.test/application/o/other/"},
    {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200},
    {"aud": [CLIENT, "another-app"]},
    {"azp": "another-app"},
])
def test_an_id_token_that_does_not_check_out_is_refused(panel, change):
    p = panel()
    p.idp.claims = change
    assert refused(sign_in(p)) == "token" and not signed_in(p)


def test_an_id_token_without_a_subject_is_refused(panel):
    p = panel()
    p.idp.drop = {"sub"}
    assert refused(sign_in(p)) == "token" and not signed_in(p)


def test_unsigned_and_shared_secret_tokens_are_refused_even_when_the_provider_offers_them(panel):
    p = panel()
    p.idp.algs = ["RS256", "HS256", "none"]                            # the panel's own list decides, not this
    p.idp.sign_with = (None, "key-1", "none")
    assert refused(sign_in(p)) == "token"
    p.c.cookies.clear()
    p.idp.sign_with = (SECRET + "-" * 32, "key-1", "HS256")           # signed with something the attacker may know
    assert refused(sign_in(p)) == "token"
    assert not signed_in(p)


def test_a_token_signed_by_a_key_the_provider_does_not_publish_is_refused(panel):
    p = panel()
    p.idp.sign_with = (_rsa(), "key-1", "RS256")                      # the right kid, the wrong key
    assert refused(sign_in(p)) == "token"
    p.c.cookies.clear()
    p.idp.sign_with = (_rsa(), "key-9", "RS256")
    assert refused(sign_in(p)) == "token"
    assert not signed_in(p)


def test_a_provider_whose_configuration_names_another_issuer_is_refused(panel):
    p = panel()
    p.idp.issuer = "https://elsewhere.example.test/"
    assert refused(p.c.get("/api/auth/oidc/start", follow_redirects=False)) == "provider"


# -- the panel's own address -----------------------------------------------------------------------------------------

def test_sign_in_started_at_another_address_moves_to_panel_url_first(panel):
    p = panel(panel_url="https://gk.example.test")
    r = p.c.get("/api/auth/oidc/start?next=/runs", follow_redirects=False)        # opened as http://panel.test
    assert r.status_code == 303
    assert r.headers["location"] == "https://gk.example.test/api/auth/oidc/start?next=%2Fruns&moved=1"
    assert oidc.COOKIE not in p.c.cookies


def test_a_proxy_that_rewrites_host_does_not_make_it_loop(panel):
    p = panel(panel_url="https://gk.example.test")
    r = p.c.get("/api/auth/oidc/start?next=/runs&moved=1", follow_redirects=False)
    assert r.headers["location"].startswith("https://idp.example.test/authorize?")


def test_an_https_panel_url_makes_the_cookies_secure_behind_a_quiet_proxy(panel):
    p = panel(panel_url="https://panel.test")                    # TLS ends at a proxy that sends no X-Forwarded-Proto
    start = p.c.get("/api/auth/oidc/start?moved=1", follow_redirects=False)       # after the one move to https
    assert "secure" in cookie_attrs(start.headers["set-cookie"])
    back = p.idp.authorize(start.headers["location"])
    flow = start.cookies.get(oidc.COOKIE)
    p.c.cookies.set(oidc.COOKIE, flow, domain="panel.test", path=oidc.COOKIE_PATH)   # the client won't resend it on http
    done = p.c.get("/api/auth/oidc/callback?" + urlencode(back), follow_redirects=False)
    assert done.headers["location"] == "/"
    session = [c for c in done.headers.get_list("set-cookie") if c.startswith(auth.COOKIE + "=")]
    assert session and "secure" in cookie_attrs(session[0])


def test_plain_http_on_the_panel_s_own_name_moves_to_https_first(panel):
    p = panel(panel_url="https://panel.test")
    r = p.c.get("/api/auth/oidc/start?next=/runs", follow_redirects=False)          # http://panel.test, no proxy
    assert r.headers["location"] == "https://panel.test/api/auth/oidc/start?next=%2Fruns&moved=1"
    assert "set-cookie" not in r.headers
    proxied = p.c.get("/api/auth/oidc/start", headers={"x-forwarded-proto": "https"}, follow_redirects=False)
    assert proxied.headers["location"].startswith("https://idp.example.test/authorize?")


def test_an_http_panel_url_leaves_it_to_the_request(panel):
    p = panel()
    assert "secure" not in cookie_attrs(p.c.get("/api/auth/oidc/start", follow_redirects=False).headers["set-cookie"])


def test_behind_a_proxy_the_forwarded_host_counts(panel):
    p = panel(panel_url="https://gk.example.test")
    proxy = {"x-forwarded-host": "gk.example.test", "x-forwarded-proto": "https"}
    r = p.c.get("/api/auth/oidc/start", headers=proxy, follow_redirects=False)
    assert r.headers["location"].startswith("https://idp.example.test/authorize?")


# -- settings --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("env, why", [
    ({"OIDC_ALLOWED_GROUPS": ""}, "every account at the provider"),
    ({"OIDC_ISSUER": "http://idp.example.test/"}, "https://"),
    ({"OIDC_CLIENT_ID": ""}, "OIDC_CLIENT_ID"),
])
def test_settings_that_would_let_anyone_in_stop_the_panel(monkeypatch, env, why):
    _env(monkeypatch, **{**OIDC_ENV, **env})
    with pytest.raises(ValueError, match=why):
        oidc.load(PANEL, True, False)


def test_the_panel_does_not_start_with_them(monkeypatch, tmp_path):
    _env(monkeypatch, **{**OIDC_ENV, "OIDC_ALLOWED_GROUPS": "", "PANEL_URL": PANEL, "DATA_DIR": str(tmp_path)})

    async def start():
        async with main.lifespan(main.app):
            pass
    with pytest.raises(ValueError, match="every account at the provider"):
        asyncio.run(start())
    assert not list(tmp_path.iterdir())                                # it stopped before opening the store


def test_oidc_needs_the_panel_s_address(monkeypatch):
    _env(monkeypatch, **OIDC_ENV)
    with pytest.raises(ValueError, match="PANEL_URL"):
        oidc.load(None, True, False)


def test_the_password_off_without_oidc_stops_the_panel(monkeypatch):
    _env(monkeypatch, PANEL_PASSWORD="off")
    with pytest.raises(ValueError, match="nobody could sign in"):
        oidc.load(PANEL, False, False)
    assert oidc.load(PANEL, False, True) is None                  # PANEL_AUTH=off: a proxy signs people in


def test_without_oidc_nothing_changes(panel, monkeypatch):
    _env(monkeypatch)
    assert oidc.load(PANEL, True, False) is None
    p = panel()
    main.app.state.oidc = None
    assert p.c.get("/api/auth/oidc/start", follow_redirects=False).status_code == 404
    assert p.c.get("/api/auth/status").json()["oidc"] is None


def test_with_the_password_off_there_is_no_setup_code_and_no_password_sign_in(panel):
    p = panel(PANEL_PASSWORD="off")
    assert p.auth.setup_code is None
    off = "password sign-in is off on this panel"
    r = p.c.post("/api/auth/setup", json={"code": "AAAA-BBBB-CCCC", "password": "correct horse"})
    assert r.status_code == 403 and r.json()["detail"] == off
    p.auth.set_password("correct horse")                          # set before it was turned off
    r = p.c.post("/api/auth/login", json={"password": "correct horse"})
    assert r.status_code == 403 and r.json()["detail"] == off
    assert sign_in(p).headers["location"] == "/" and signed_in(p)
    r = p.c.post("/api/auth/password", json={"current": "correct horse", "password": "battery staple"})
    assert r.status_code == 403 and r.json()["detail"] == off


def test_the_sign_in_page_learns_what_to_show(panel):
    st = panel(OIDC_AUTO_LOGIN="on", PANEL_PASSWORD="off").c.get("/api/auth/status").json()
    assert st["oidc"] == {"name": "Authentik", "auto_login": True} and st["password"] is False


# -- the sign-in page ------------------------------------------------------------------------------------------------

def test_the_sign_in_page_has_a_fixed_sentence_for_every_code_and_nothing_else():
    js = (WEB / "login.js").read_text(encoding="utf-8")
    block = re.search(r"const REASONS = \{(.*?)\};", js, re.S).group(1)
    assert set(re.findall(r"^\s*(\w+):", block, re.M)) == set(oidc.CODES)
    assert 'err(params.get("error"))' not in js                         # never the address's own text


def test_signing_out_does_not_bounce_straight_back_in():
    assert 'location.href = "/login.html?signed_out=1"' in (WEB / "settings.js").read_text(encoding="utf-8")
    assert '!params.has("signed_out")' in (WEB / "login.js").read_text(encoding="utf-8")
