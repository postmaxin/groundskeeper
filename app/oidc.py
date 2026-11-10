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
from urllib.parse import urlencode, urlsplit

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


class OIDCError(Exception):
    """A sign-in that must not go on. The message is shown on the sign-in page, so it never holds a token."""


class NotAllowed(OIDCError):
    """The provider vouched for this person, but they are outside the allowed groups and domains."""

    def __init__(self, user: str):
        super().__init__(f"{user} is not allowed to use this panel")
        self.user = user


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
    return Settings(
        issuer=issuer, client_id=client_id, client_secret=os.environ.get("OIDC_CLIENT_SECRET", "").strip(),
        name=(os.environ.get("OIDC_NAME", "").strip() or "OpenID Connect")[:40],
        scopes=os.environ.get("OIDC_SCOPES", "").strip() or "openid profile email",
        allowed_groups=groups, groups_claim=os.environ.get("OIDC_GROUPS_CLAIM", "").strip() or "groups",
        allowed_domains=frozenset(d.lower().lstrip("@") for d in domains), auto_login=_on("OIDC_AUTO_LOGIN"),
        redirect_uri=f"{parts.scheme}://{parts.netloc}{CALLBACK}")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def safe_next(path) -> str:
    """Only a path in this panel: `//x`, `/\\x` or a full address would send the person somewhere else."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith(("//", "/\\")) or "\\" in path:
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
            log.warning("OIDC provider unreachable: %s", exc)
            raise OIDCError("the sign-in provider is not answering") from None
        if r.status_code != 200:
            log.warning("OIDC provider answered %s for %s", r.status_code, url)
            raise OIDCError("the sign-in provider refused a request")
        try:
            body = r.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise OIDCError("the sign-in provider sent something unexpected")
        return body

    async def config(self) -> dict:
        if self._config and time.time() - self._config_at < DISCOVERY_S:
            return self._config
        s = self.settings
        c = await self._get_json(s.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if str(c.get("issuer", "")).rstrip("/") != s.issuer.rstrip("/"):
            log.warning("OIDC discovery names issuer %r, not %r", c.get("issuer"), s.issuer)
            raise OIDCError("the sign-in provider's address does not match its own configuration")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(c.get(k), str) or not _https(c[k]):
                raise OIDCError(f"the sign-in provider's configuration has no https {k}")
        if c.get("userinfo_endpoint") is not None and not (isinstance(c["userinfo_endpoint"], str)
                                                           and _https(c["userinfo_endpoint"])):
            raise OIDCError("the sign-in provider's userinfo address is not https")
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
        raise OIDCError("the sign-in token was signed with a key the provider does not publish")

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
        """Check what the provider sent back. Returns {"user", "sub", "next"} for someone who may sign in."""
        if query.get("error"):
            text = "".join(ch for ch in str(query.get("error_description") or query["error"]) if ch.isprintable())
            raise OIDCError(f"the sign-in provider said: {text[:160]}")
        flow = self._open(cookie)
        if flow is None:
            raise OIDCError("this sign-in expired or was started in another browser; try again")
        if not hmac.compare_digest(str(query.get("state", "")).encode(), str(flow.get("state")).encode()):
            raise OIDCError("this sign-in does not match the one started here; try again")
        code = query.get("code")
        if not isinstance(code, str) or not code:
            raise OIDCError("the sign-in provider sent no code")
        tokens = await self._exchange(code, flow["verifier"])
        claims = await self._check_id_token(tokens.get("id_token"), flow["nonce"])
        info = claims
        if self.settings.groups_claim not in claims or (self.settings.allowed_domains and "email" not in claims):
            info = {**await self._userinfo(tokens.get("access_token"), claims["sub"]), **claims}
        user = next((str(info[k]) for k in ("preferred_username", "email", "sub") if info.get(k)), claims["sub"])
        user = "".join(ch for ch in user if ch.isprintable())[:60]
        if not self._allowed(info):
            raise NotAllowed(user)
        return {"user": user, "sub": str(claims["sub"]), "next": flow["next"]}

    async def _exchange(self, code: str, verifier: str) -> dict:
        s, c = self.settings, await self.config()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": s.redirect_uri,
                "code_verifier": verifier, "client_id": s.client_id}
        auth = None
        if s.client_secret:
            methods = c.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
            if "client_secret_basic" in methods:
                auth = (s.client_id, s.client_secret)
            else:
                data["client_secret"] = s.client_secret
        try:
            r = await self._http.post(c["token_endpoint"], data=data, auth=auth, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            log.warning("OIDC token endpoint unreachable: %s", exc)
            raise OIDCError("the sign-in provider is not answering") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code != 200 or not isinstance(body, dict) or not isinstance(body.get("id_token"), str):
            log.warning("OIDC token endpoint answered %s", r.status_code)
            raise OIDCError("the sign-in provider did not accept the sign-in")
        return body

    async def _check_id_token(self, token, nonce: str) -> dict:
        s, c = self.settings, await self.config()
        if not isinstance(token, str):
            raise OIDCError("the sign-in provider sent no ID token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise OIDCError("the sign-in token is not readable") from None
        alg = header.get("alg")
        allowed = ASYMMETRIC & set(c.get("id_token_signing_alg_values_supported") or ASYMMETRIC)
        if alg not in allowed:
            raise OIDCError("the sign-in token is not signed in a way this panel accepts")
        key = await self._key_for(header.get("kid"), alg)
        try:
            claims = jwt.decode(token, key=key.key, algorithms=[alg], audience=s.client_id, issuer=c["issuer"],
                                leeway=LEEWAY_S, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as exc:
            log.warning("OIDC ID token refused: %s", exc)
            raise OIDCError("the sign-in token did not check out") from None
        aud = claims["aud"]
        if ("azp" in claims or (isinstance(aud, list) and len(aud) > 1)) and claims.get("azp") != s.client_id:
            raise OIDCError("the sign-in token was issued to another application")
        if not hmac.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
            raise OIDCError("the sign-in token does not belong to this sign-in")
        return claims

    async def _userinfo(self, access_token, sub) -> dict:
        url = (await self.config()).get("userinfo_endpoint")
        if not url or not isinstance(access_token, str):
            return {}
        info = await self._get_json(url, headers={"Authorization": f"Bearer {access_token}"})
        if info.get("sub") != sub:                         # OIDC Core 5.3.2: userinfo must be about the same person
            raise OIDCError("the sign-in provider's user details are about someone else")
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
