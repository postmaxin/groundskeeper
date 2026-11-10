"""Sign-in through an OpenID Connect provider (app/oidc.py), driven through the real app's routes and middleware.
The provider is a fake inside httpx's MockTransport: synthetic RSA keys, a token endpoint that checks PKCE and the
client's secret, real signed ID tokens. No network, no robot."""
import base64
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

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


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Provider:
    """Just enough of an OpenID provider. Tests change its fields to make it misbehave."""

    def __init__(self):
        self.key, self.kid = _rsa(), "key-1"
        self.published = [(self.key, self.kid)]          # what /jwks shows
        self.sign_with = None                            # (key, kid, alg) override for the ID token
        self.claims: dict = {}                           # overrides / additions to the ID token
        self.drop: set = set()                           # claims to leave out of the ID token
        self.userinfo: dict = {}
        self.codes: dict = {}
        self.jwks_reads = 0
        self.token_requests: list = []

    def config(self):
        return {"issuer": ISSUER, "authorization_endpoint": "https://idp.example.test/authorize",
                "token_endpoint": "https://idp.example.test/token", "jwks_uri": "https://idp.example.test/jwks",
                "userinfo_endpoint": "https://idp.example.test/userinfo",
                "id_token_signing_alg_values_supported": ["RS256"],
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

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == ISSUER + ".well-known/openid-configuration":
            return httpx.Response(200, json=self.config())
        if url == "https://idp.example.test/jwks":
            self.jwks_reads += 1
            keys = []
            for key, kid in self.published:
                jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
                keys.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
            return httpx.Response(200, json={"keys": keys})
        if url == "https://idp.example.test/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append({"form": form, "auth": request.headers.get("authorization")})
            want = "Basic " + base64.b64encode(f"{CLIENT}:{SECRET}".encode()).decode()
            q = self.codes.pop(form.get("code"), None)
            if request.headers.get("authorization") != want or q is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
            if challenge.decode() != q["code_challenge"] or form["redirect_uri"] != q["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at-1", "token_type": "Bearer",
                                             "id_token": self.id_token(q)})
        if url == "https://idp.example.test/userinfo":
            assert request.headers.get("authorization") == "Bearer at-1"
            return httpx.Response(200, json={"sub": "user-1", **self.userinfo})
        return httpx.Response(404)


OIDC_ENV = {"OIDC_ISSUER": ISSUER, "OIDC_CLIENT_ID": CLIENT, "OIDC_CLIENT_SECRET": SECRET, "OIDC_NAME": "Authentik",
            "OIDC_ALLOWED_GROUPS": "lawn admins"}


def _env(monkeypatch, **values):
    for k in ("PANEL_AUTH", "PANEL_PASSWORD", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_NAME",
              "OIDC_SCOPES", "OIDC_ALLOWED_GROUPS", "OIDC_GROUPS_CLAIM", "OIDC_ALLOWED_DOMAINS", "OIDC_AUTO_LOGIN"):
        monkeypatch.delenv(k, raising=False)
    for k, v in values.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def panel(tmp_path, monkeypatch):
    def make(**env):
        _env(monkeypatch, **{**OIDC_ENV, **env})
        store = Store(str(tmp_path))
        gate = auth.Auth(store)
        gate.announce()
        provider = Provider()
        settings = oidc.load(PANEL, auth.password_enabled(), auth.disabled())
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


def signed_in(p) -> bool:
    return p.c.get("/api/auth/status").json()["signed_in"]


def refused(r) -> str:
    assert r.status_code == 303 and r.headers["location"].startswith("/login.html?error="), r.headers
    return parse_qs(urlsplit(r.headers["location"]).query)["error"][0]


# -- the happy path --------------------------------------------------------------------------------------------------

def test_someone_in_an_allowed_group_signs_in_and_goes_where_they_were_headed(panel):
    p = panel()
    assert not signed_in(p)
    r = sign_in(p, "/#settings/access")
    assert r.status_code == 303 and r.headers["location"] == "/#settings/access"
    assert signed_in(p) and p.c.get("/api/auth/tokens").status_code == 200     # a person's session
    sessions = list(p.store.setting("auth_sessions").values())
    assert sessions[0]["user"] == "tess"
    assert any(e["detail"] == "tess signed in with Authentik" for e in p.store.events())
    assert oidc.COOKIE not in p.c.cookies                                       # the flow cookie is spent


def test_the_provider_gets_pkce_and_the_client_secret(panel):
    p = panel()
    sign_in(p)
    sent = p.idp.token_requests[0]
    assert sent["auth"].startswith("Basic ") and "client_secret" not in sent["form"]
    assert len(sent["form"]["code_verifier"]) >= 43 and sent["form"]["redirect_uri"] == PANEL + oidc.CALLBACK


def test_where_to_go_after_stays_in_this_panel(panel):
    p = panel()
    for bad in ("//evil.example", "https://evil.example/", "/\\evil.example", "evil"):
        p.c.cookies.clear()
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


def test_a_verified_email_in_an_allowed_domain_is_let_in(panel):
    p = panel(OIDC_ALLOWED_GROUPS="", OIDC_ALLOWED_DOMAINS="Example.org")
    p.idp.drop = {"groups"}
    assert sign_in(p).status_code == 303 and signed_in(p)


# -- who is turned away ----------------------------------------------------------------------------------------------

def test_someone_outside_the_allowed_groups_is_refused_and_it_is_noted(panel):
    p = panel()
    p.idp.claims = {"groups": ["neighbours"]}
    assert refused(sign_in(p)) == "tess is not allowed to use this panel"
    assert not signed_in(p)
    assert any("tess signed in with Authentik but is not allowed" in e["detail"] for e in p.store.events())


def test_an_unverified_email_is_not_enough(panel):
    p = panel(OIDC_ALLOWED_GROUPS="", OIDC_ALLOWED_DOMAINS="example.org")
    p.idp.drop = {"groups"}
    p.idp.claims = {"email_verified": False}
    assert "not allowed" in refused(sign_in(p)) and not signed_in(p)


def test_userinfo_about_someone_else_is_refused(panel):
    p = panel()
    p.idp.drop = {"groups"}
    p.idp.userinfo = {"sub": "user-2", "groups": ["lawn admins"]}
    assert "someone else" in refused(sign_in(p)) and not signed_in(p)


# -- what doesn't check out ------------------------------------------------------------------------------------------

def test_a_callback_with_another_state_is_refused(panel):
    p = panel()
    assert "does not match" in refused(sign_in(p, tamper=lambda b: {**b, "state": "forged"}))
    assert not signed_in(p)


def test_a_callback_without_the_flow_cookie_is_refused_and_not_logged_as_an_event(panel):
    p = panel()
    start = p.c.get("/api/auth/oidc/start", follow_redirects=False)
    back = p.idp.authorize(start.headers["location"])
    p.c.cookies.clear()
    assert "expired" in refused(p.c.get("/api/auth/oidc/callback?" + urlencode(back), follow_redirects=False))
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
    assert "expired" in refused(p.c.get("/api/auth/oidc/callback?" + urlencode(back), follow_redirects=False))
    p.c.cookies.clear()                                               # the genuine cookie, set the same way, works
    p.c.cookies.set(oidc.COOKIE, flow, domain="panel.test", path=oidc.COOKIE_PATH)
    assert p.c.get("/api/auth/oidc/callback?" + urlencode(back), follow_redirects=False).headers["location"] == "/"
    assert signed_in(p)


@pytest.mark.parametrize("change, why", [
    ({"nonce": "another"}, "does not belong"),
    ({"aud": "another-app"}, "did not check out"),
    ({"iss": "https://idp.example.test/application/o/other/"}, "did not check out"),
    ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "did not check out"),
    ({"aud": [CLIENT, "another-app"]}, "another application"),
    ({"azp": "another-app"}, "another application"),
])
def test_an_id_token_that_does_not_check_out_is_refused(panel, change, why):
    p = panel()
    p.idp.claims = change
    assert why in refused(sign_in(p)) and not signed_in(p)


def test_an_id_token_without_a_subject_is_refused(panel):
    p = panel()
    p.idp.drop = {"sub"}
    assert "did not check out" in refused(sign_in(p)) and not signed_in(p)


def test_unsigned_and_shared_secret_tokens_are_refused(panel):
    p = panel()
    p.idp.sign_with = (None, "key-1", "none")
    assert "not signed in a way" in refused(sign_in(p))
    p.c.cookies.clear()
    p.idp.sign_with = (SECRET + "-" * 32, "key-1", "HS256")      # signed with something the attacker may know
    assert "not signed in a way" in refused(sign_in(p))
    assert not signed_in(p)


def test_a_token_signed_by_a_key_the_provider_does_not_publish_is_refused(panel):
    p = panel()
    p.idp.sign_with = (_rsa(), "key-1", "RS256")                   # the right kid, the wrong key
    assert "did not check out" in refused(sign_in(p))
    p.c.cookies.clear()
    p.idp.sign_with = (_rsa(), "key-9", "RS256")
    assert "does not publish" in refused(sign_in(p))
    assert not signed_in(p)


def test_the_provider_s_own_refusal_is_shown_on_the_sign_in_page(panel):
    p = panel()
    said_no = {"error": "access_denied", "error_description": "You said no."}
    r = sign_in(p, tamper=lambda b: {**said_no, "state": b["state"]})
    assert refused(r) == "the sign-in provider said: You said no." and not signed_in(p)


def test_a_provider_whose_configuration_names_another_issuer_is_refused(panel):
    p = panel()
    p.idp.config = lambda: {**Provider.config(p.idp), "issuer": "https://elsewhere.example.test/"}
    assert "does not match" in refused(p.c.get("/api/auth/oidc/start", follow_redirects=False))


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
    assert p.c.post("/api/auth/setup", json={"code": "AAAA-BBBB-CCCC", "password": "correct horse"}).status_code == 403
    p.auth.set_password("correct horse")                          # set before it was turned off
    assert p.c.post("/api/auth/login", json={"password": "correct horse"}).status_code == 403
    sign_in(p)
    change = {"current": "correct horse", "password": "battery staple"}
    assert p.c.post("/api/auth/password", json=change).status_code == 403


def test_the_sign_in_page_learns_what_to_show(panel):
    st = panel(OIDC_AUTO_LOGIN="on", PANEL_PASSWORD="off").c.get("/api/auth/status").json()
    assert st["oidc"] == {"name": "Authentik", "auto_login": True} and st["password"] is False
