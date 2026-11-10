"""Sign-in through an OpenID Connect provider (Authentik, Keycloak, Authelia, Google, ...), modelled on Grafana's
generic OAuth sign-in and kept as small. A person who signs in this way gets the same session as the password gives
(app/auth.py), so the gate, the cross-site check, API tokens and MCP don't change.

Settings come from the environment, named after Grafana's `[auth.generic_oauth]`:
    OIDC_ISSUER            the provider; its /.well-known/openid-configuration is read (https only). Unset: OIDC off.
    OIDC_CLIENT_ID         the client registered at the provider
    OIDC_CLIENT_SECRET     its secret (empty for a public client: PKCE is always used)
    OIDC_NAME              the button label, "Sign in with <name>"            (Grafana: name)
    OIDC_SCOPES            default "openid profile email"                     (scopes)
    OIDC_ALLOWED_GROUPS    comma-separated; matched against the groups claim  (allowed_groups)
    OIDC_GROUPS_CLAIM      default "groups"                                   (groups_attribute_path, a plain claim)
    OIDC_ALLOWED_DOMAINS   comma-separated; a *verified* email's domain       (allowed_domains)
    OIDC_AUTO_LOGIN        "on": the sign-in page goes straight to the provider (auto_login)
    PANEL_PASSWORD=off     hides the password form, and turns off first-run setup (disable_login_form; app/auth.py)

One difference from Grafana, on purpose: there an empty allowlist lets every account at the provider in. Here OIDC
with no allowed groups or domains refuses to start, because the panel drives a machine with blades.

The flow is the authorization code flow with PKCE (S256), `state` and `nonce`. They wait in a short-lived HttpOnly
cookie signed with the panel's own key, so nothing is kept server-side between the two requests. The ID token's
signature is checked against the provider's JWKS (asymmetric algorithms only; `none` and HS* are refused), then
`iss`, `aud`, `azp`, `exp`, `iat` and `nonce`. Groups come from the ID token, or from userinfo when the token has no
such claim. There is one role: everyone let in is the admin, as with the password."""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote_plus, urlencode, urlsplit

import httpx
import jwt

log = logging.getLogger("oidc")

START = "/api/auth/oidc/start"
CALLBACK = "/api/auth/oidc/callback"
COOKIE = "gk_oidc"                    # the flow in progress: state, nonce, PKCE verifier, where to go after
COOKIE_PATH = "/api/auth/oidc"
FLOW_S = 600                          # a sign-in must come back from the provider within this
DISCOVERY_S = 3600                    # how long the provider's configuration is kept before it is read again
LEEWAY_S = 60                         # clock difference allowed on exp / iat
ASYMMETRIC = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"})


# What the sign-in page may be told. Only these codes go into its address, and web/login.js turns each into a fixed
# sentence, so nobody can put words of their own on the panel's real sign-in page.
CODES = frozenset({"unavailable", "provider", "denied", "expired", "token", "not_allowed"})


class OIDCError(Exception):
    """A sign-in that must not go on. `code` (one of CODES) is for the sign-in page; the message is for the log only.
    `spent` says the flow cookie was this sign-in's own (it matched), so it is used up."""

    def __init__(self, code: str, detail: str):
        if code not in CODES:
            raise ValueError(code)
        super().__init__(detail)
        self.code, self.spent = code, False


class NotAllowed(OIDCError):
    """The provider vouched for this account, but it is outside the allowed groups and domains."""

    def __init__(self, user: str, sub: str):
        super().__init__("not_allowed", f"{user} (subject {sub}) is not allowed to use this panel")
        self.user, self.sub = user, sub


def _on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("on", "1", "true", "yes")


def _list(name: str) -> frozenset[str]:
    return frozenset(x.strip() for x in os.environ.get(name, "").split(",") if x.strip())


def _https(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(parts.netloc)


@dataclass(frozen=True)
class Settings:
    issuer: str
    client_id: str
    client_secret: str
    name: str
    scopes: str
    allowed_groups: frozenset[str]
    groups_claim: str
    allowed_domains: frozenset[str]
    auto_login: bool
    panel: str                         # PANEL_URL, scheme and host only: where sign-in starts and comes back
    redirect_uri: str


def load(panel_url: str | None, password_on: bool, auth_off: bool) -> Settings | None:
    """The OIDC settings from the environment, None when OIDC is off. Raises ValueError for settings the panel must
    not start with: they would let anyone at the provider in, or leave nobody able to sign in."""
    issuer = os.environ.get("OIDC_ISSUER", "").strip()
    if not issuer:
        if not password_on and not auth_off:
            raise ValueError("PANEL_PASSWORD=off needs OIDC_ISSUER: with neither, nobody could sign in")
        return None
    if not _https(issuer):
        raise ValueError("OIDC_ISSUER must be an https:// address")
    client_id = os.environ.get("OIDC_CLIENT_ID", "").strip()
    if not client_id:
        raise ValueError("OIDC_ISSUER is set but OIDC_CLIENT_ID is not")
    groups, domains = _list("OIDC_ALLOWED_GROUPS"), _list("OIDC_ALLOWED_DOMAINS")
    if not groups and not domains:
        raise ValueError("OIDC needs OIDC_ALLOWED_GROUPS or OIDC_ALLOWED_DOMAINS: without one, every account at the "
                         "provider could use this panel")
    parts = urlsplit(panel_url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("OIDC needs PANEL_URL, the address people open, for the provider to send them back to")
    if parts.path.strip("/") or parts.query or parts.fragment:
        # The panel serves itself from the root (/login.js, /api/...), so it can't live under a path prefix.
        raise ValueError("PANEL_URL must be the panel's own address with no path, e.g. https://panel.example.com")
    if parts.scheme == "http":
        log.warning("PANEL_URL is http://: the sign-in code and its PKCE verifier cross the network unencrypted")
    panel = f"{parts.scheme}://{parts.netloc}"
    return Settings(
        issuer=issuer, client_id=client_id, client_secret=os.environ.get("OIDC_CLIENT_SECRET", "").strip(),
        name=(os.environ.get("OIDC_NAME", "").strip() or "OpenID Connect")[:40],
        scopes=os.environ.get("OIDC_SCOPES", "").strip() or "openid profile email",
        allowed_groups=groups, groups_claim=os.environ.get("OIDC_GROUPS_CLAIM", "").strip() or "groups",
        allowed_domains=frozenset(d.lower().lstrip("@") for d in domains), auto_login=_on("OIDC_AUTO_LOGIN"),
        panel=panel, redirect_uri=panel + CALLBACK)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def safe_next(path) -> str:
    """Only a path in this panel: `//x`, `/\\x`, a full address, or one with spaces or control characters (which a
    browser may drop, leaving `//`) would send the person somewhere else."""
    if (not isinstance(path, str) or not path.startswith("/") or path.startswith(("//", "/\\")) or "\\" in path
            or any(ch.isspace() or not ch.isprintable() for ch in path)):
        return "/"
    return path[:500]


def _names(value) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {v for v in value if isinstance(v, str)}
    return set()


class OIDC:
    def __init__(self, settings: Settings, key: Callable[[], bytes], http: httpx.AsyncClient | None = None):
        self.settings = settings
        self._key = key                                   # the panel's signing key (Auth._key)
        self._http = http or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self._config: dict | None = None
        self._config_at = 0.0
        self._jwks: dict | None = None

    # -- the provider ------------------------------------------------------------------------------------------------
    async def _get_json(self, url: str, **kw) -> dict:
        try:
            r = await self._http.get(url, **kw)
        except httpx.HTTPError as exc:
            raise OIDCError("unavailable", f"provider unreachable: {exc}") from None
        if r.status_code != 200:
            raise OIDCError("provider", f"provider answered {r.status_code} for {url}")
        try:
            body = r.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise OIDCError("provider", f"provider sent no JSON object for {url}")
        return body

    async def config(self) -> dict:
        if self._config and time.time() - self._config_at < DISCOVERY_S:
            return self._config
        s = self.settings
        c = await self._get_json(s.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if str(c.get("issuer", "")).rstrip("/") != s.issuer.rstrip("/"):
            raise OIDCError("provider", f"discovery names issuer {c.get('issuer')!r}, not {s.issuer!r}")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(c.get(k), str) or not _https(c[k]):
                raise OIDCError("provider", f"discovery has no https {k}")
        if c.get("userinfo_endpoint") is not None and not (isinstance(c["userinfo_endpoint"], str)
                                                           and _https(c["userinfo_endpoint"])):
            raise OIDCError("provider", "discovery's userinfo_endpoint is not https")
        self._config, self._config_at, self._jwks = c, time.time(), None
        return c

    async def _key_for(self, kid: str | None, alg: str) -> jwt.PyJWK:
        for fresh in (False, True):                       # a kid we don't know may be a rotated key: read once more
            if fresh or self._jwks is None:
                self._jwks = await self._get_json((await self.config())["jwks_uri"])
            try:
                keys = jwt.PyJWKSet.from_dict(self._jwks).keys
            except jwt.PyJWTError:
                keys = []
            match = [k for k in keys if (kid is None or k.key_id == kid) and k.public_key_use in (None, "sig")]
            if kid is None and len(match) > 1:
                match = [k for k in match if k.algorithm_name == alg]
            if len(match) == 1:
                return match[0]
        raise OIDCError("token", f"ID token signed with a key the provider does not publish (kid {kid!r})")

    # -- the flow cookie ---------------------------------------------------------------------------------------------
    def _seal(self, flow: dict) -> str:
        payload = _b64(json.dumps(flow, separators=(",", ":")).encode())
        return payload + "." + hmac.new(self._key(), b"oidc|" + payload.encode(), hashlib.sha256).hexdigest()

    def _open(self, cookie) -> dict | None:
        if not isinstance(cookie, str) or "." not in cookie:
            return None
        payload, sig = cookie.rsplit(".", 1)
        want = hmac.new(self._key(), b"oidc|" + payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig.encode(), want.encode()):
            return None
        try:
            flow = json.loads(_unb64(payload))
        except ValueError:
            return None
        if not isinstance(flow, dict) or not isinstance(flow.get("exp"), (int, float)) or flow["exp"] < time.time():
            return None
        return flow

    # -- the two steps -----------------------------------------------------------------------------------------------
    async def begin(self, next_path: str | None) -> tuple[str, str]:
        """The provider's sign-in address, and the cookie that the callback needs to finish."""
        s, c = self.settings, await self.config()
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        query = urlencode({"response_type": "code", "client_id": s.client_id, "redirect_uri": s.redirect_uri,
                           "scope": s.scopes, "state": state, "nonce": nonce, "code_challenge": challenge,
                           "code_challenge_method": "S256"})
        endpoint = c["authorization_endpoint"]
        url = endpoint + ("&" if urlsplit(endpoint).query else "?") + query
        flow = {"state": state, "nonce": nonce, "verifier": verifier, "next": safe_next(next_path),
                "exp": int(time.time() + FLOW_S)}
        return url, self._seal(flow)

    async def finish(self, query: dict, cookie) -> dict:
        """Check what the provider sent back. Returns {"user", "sub", "next"} for someone who may sign in.
        Nothing is believed, not even an error, until the flow cookie and `state` show it is this browser's sign-in."""
        flow = self._open(cookie)
        if flow is None:
            raise OIDCError("expired", "no valid flow cookie (expired, another browser, or forged)")
        if not hmac.compare_digest(str(query.get("state", "")).encode(), str(flow.get("state")).encode()):
            raise OIDCError("expired", "state does not match this browser's sign-in")
        try:
            return await self._finish(query, flow)
        except OIDCError as exc:
            exc.spent = True
            raise

    async def _finish(self, query: dict, flow: dict) -> dict:
        if query.get("error"):
            text = "".join(ch for ch in str(query.get("error_description") or query["error"]) if ch.isprintable())
            raise OIDCError("denied" if query["error"] == "access_denied" else "provider",
                            f"provider said: {text[:160]}")
        code = query.get("code")
        if not isinstance(code, str) or not code:
            raise OIDCError("provider", "no code in the callback")
        tokens = await self._exchange(code, flow["verifier"])
        claims = await self._check_id_token(tokens.get("id_token"), flow["nonce"])
        s = self.settings
        info = self._merge(claims, {})
        # Userinfo only when the ID token alone doesn't let them in and lacks a claim an allowlist needs: a sign-in
        # the token already settles must not fail because userinfo had a bad moment.
        if not self._allowed(info) and (
                (s.allowed_groups and s.groups_claim not in claims)
                or (s.allowed_domains and ("email" not in claims or "email_verified" not in claims))):
            info = self._merge(claims, await self._userinfo(tokens.get("access_token"), claims["sub"]))
        user =next((str(info[k]) for k in ("preferred_username", "email", "sub") if info.get(k)), claims["sub"])
        user = "".join(ch for ch in user if ch.isprintable())[:60]
        if not self._allowed(info):
            raise NotAllowed(user, str(claims["sub"])[:120])
        return {"user": user, "sub": str(claims["sub"])[:120], "next": flow["next"]}

    async def _exchange(self, code: str, verifier: str) -> dict:
        s, c = self.settings, await self.config()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": s.redirect_uri,
                "code_verifier": verifier, "client_id": s.client_id}
        headers = {"Accept": "application/json"}
        if s.client_secret:
            methods = c.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
            if "client_secret_basic" in methods:      # RFC 6749 2.3.1: each part form-encoded before base64
                pair = f"{quote_plus(s.client_id)}:{quote_plus(s.client_secret)}"
                headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
            else:
                data["client_secret"] = s.client_secret
        try:
            r = await self._http.post(c["token_endpoint"], data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise OIDCError("unavailable", f"token endpoint unreachable: {exc}") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code != 200 or not isinstance(body, dict) or not isinstance(body.get("id_token"), str):
            raise OIDCError("provider", f"token endpoint answered {r.status_code}")
        return body

    async def _check_id_token(self, token, nonce: str) -> dict:
        s, c = self.settings, await self.config()
        if not isinstance(token, str):
            raise OIDCError("provider", "no ID token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise OIDCError("token", "ID token header unreadable") from None
        alg = header.get("alg")
        allowed = ASYMMETRIC & set(c.get("id_token_signing_alg_values_supported") or ASYMMETRIC)
        if alg not in allowed:
            raise OIDCError("token", f"ID token algorithm {alg!r} is not accepted")
        key = await self._key_for(header.get("kid"), alg)
        try:
            claims = jwt.decode(token, key=key.key, algorithms=[alg], audience=s.client_id, issuer=c["issuer"],
                                leeway=LEEWAY_S, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as exc:
            raise OIDCError("token", f"ID token refused: {exc}") from None
        aud = claims["aud"]
        if ("azp" in claims or (isinstance(aud, list) and len(aud) > 1)) and claims.get("azp") != s.client_id:
            raise OIDCError("token", "ID token azp names another client")
        if not hmac.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
            raise OIDCError("token", "ID token nonce does not match this sign-in")
        return claims

    async def _userinfo(self, access_token, sub) -> dict:
        url = (await self.config()).get("userinfo_endpoint")
        if not url or not isinstance(access_token, str):
            return {}
        info = await self._get_json(url, headers={"Authorization": f"Bearer {access_token}"})
        if info.get("sub") != sub:                         # OIDC Core 5.3.2: userinfo must be about the same person
            raise OIDCError("token", "userinfo sub differs from the ID token's")
        return info

    @staticmethod
    def _merge(claims: dict, extra: dict) -> dict:
        """The ID token's claims over userinfo's. The email and its verified flag are one pair, always from the same
        place: a flag about another address must never vouch for this one. Userinfo may only fill in the flag for
        the very same address."""
        info = {**extra, **claims}
        if "email" in claims:
            info["email_verified"] = claims.get("email_verified")
            if info["email_verified"] is None and extra.get("email") == claims["email"]:
                info["email_verified"] = extra.get("email_verified")
        else:
            info["email"], info["email_verified"] = extra.get("email"), extra.get("email_verified")
        return info

    def _allowed(self, info: dict) -> bool:
        s = self.settings
        if s.allowed_groups and _names(info.get(s.groups_claim)) & s.allowed_groups:
            return True
        email = info.get("email")
        if s.allowed_domains and info.get("email_verified") is True and isinstance(email, str) and "@" in email:
            return email.rsplit("@", 1)[1].lower() in s.allowed_domains
        return False

    async def close(self) -> None:
        await self._http.aclose()
