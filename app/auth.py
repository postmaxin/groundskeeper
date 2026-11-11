"""Who may use the panel: one admin password for people (a session cookie) and named API tokens for programs
(`Authorization: Bearer gk_…`, e.g. a Home Assistant integration or a script).

Everything is locked except /health, the sign-in page and its assets: the API, the event stream, the yard imagery
and tiles (they show where the yard is). /mcp has its own token (app/mcp_server.py) and is left to it.

An API token reads, and may ask for robot commands (TOKEN_WRITES); it changes nothing else. A new route that writes is
closed to tokens until it is added there. Its commands run as actor "token" (app/control.py): nobody is watching, so
they get the strict checks and no override. Settings, safety switches and the map need a person's session.

First run: no password is set yet, so the panel prints a one-time setup code to its log (`docker logs ...`), and
the sign-in page asks for that code with the new password. Whoever reaches the panel first on the network cannot
claim it without access to the host. Forgotten password: `python -m app.auth reset` (inside the container) clears
it and the sessions; the next start prints a new code.

`PANEL_AUTH=off` turns all of this off, for a panel behind a reverse proxy that does its own sign-in. Never on a
panel others on the network can reach.

People can also sign in through an OpenID Connect provider (app/oidc.py); that gives the same session.
`PANEL_PASSWORD=off` then turns the password off: no password sign-in, no first-run setup code, no password change.

Stored in the settings table: `auth` {scrypt hash, salt, set}, `auth_sessions` {sha256(session): {created, seen,
agent, user and sub (OIDC sign-ins)}}, `api_tokens` [{id, name, sha256, hint, created, used, scope?}]. Only hashes; a session or
token is shown once."""
import hashlib
import hmac
import logging
import os
import secrets
import sys
import time
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("auth")

COOKIE = "gk_session"
SESSION_DAYS = 30
SEEN_EVERY_S = 600              # a session's "last seen" is written at most this often
MIN_PASSWORD = 8
MAX_FAILS = 5                   # sign-in failures from one address before it waits
LOCK_S = 60                     # ...first wait; doubles each time, up to LOCK_MAX_S
LOCK_MAX_S = 900
INTERNAL_HEADER = "x-gk-internal"
INTERNAL = secrets.token_urlsafe(32)   # the MCP server's in-process calls (never leaves this process)

# Paths anyone may load: the sign-in page and what it needs, and /health (for Docker and uptime checks).
OPEN_PATHS = {"/health", "/login.html", "/login.js", "/style.css", "/favicon.ico", "/manifest.json",
              "/api/auth/status", "/api/auth/login", "/api/auth/setup",
              "/api/auth/oidc/start", "/api/auth/oidc/callback"}     # OIDC: the way out to the provider and back
OPEN_PREFIXES = ("/brand/",)
# Locked: data. The static UI files (plain JS/CSS from this repo) carry no data and stay open; "/" redirects.
LOCKED_PREFIXES = ("/api/", "/tiles/", "/imagery/")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# The only changes an API token may ask for; everything else that is not a read needs a person. Default closed.
TOKEN_WRITES = {("POST", "/api/control/preview"), ("POST", "/api/control/send")}
# Token scopes. "full" (also a row with no scope, as every token made before scopes) is the behaviour above.
# "metrics" may only GET or HEAD exactly METRICS_PATH (compared whole: no prefix) and nothing else (metrics_may).
SCOPES = ("full", "metrics")
METRICS_PATH = "/api/metrics"
# Paths a signed, expiring link may open without a session: the camera snapshot in a phone notification, which the
# phone's notification service fetches with no cookie (sign_path).
SIGNED_PREFIXES = ("/api/frigate/snapshot/",)
SIGNED_TTL_S = 3600


def disabled() -> bool:
    return os.environ.get("PANEL_AUTH", "").strip().lower() in ("off", "0", "false", "no")


def password_enabled() -> bool:
    """PANEL_PASSWORD=off: people sign in through OIDC only (app/oidc.py refuses to start without it)."""
    return os.environ.get("PANEL_PASSWORD", "").strip().lower() not in ("off", "0", "false", "no")


def _same(a, b) -> bool:
    """Constant-time compare that never raises: non-ASCII text or a non-string is simply not equal."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode(), b.encode())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _scrypt(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def needs_auth(path: str) -> bool:
    if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES) or path == "/mcp" or path.startswith("/mcp/"):
        return False
    return path == "/" or path.startswith(LOCKED_PREFIXES)


class Auth:
    def __init__(self, store):
        self.store = store
        self.setup_code: str | None = None
        self._fails: dict[str, tuple[int, float, float]] = {}   # address -> (fails, locked until, last wait)

    # -- password ----------------------------------------------------------------------------------------------------
    def has_password(self) -> bool:
        return bool((self.store.setting("auth") or {}).get("hash"))

    def announce(self) -> None:
        """At start: with no password yet, make the one-time setup code and print it where the owner will look."""
        if disabled():
            log.warning("PANEL_AUTH=off: anyone who can reach this panel can use it, including robot control")
            return
        if not password_enabled():
            log.info("PANEL_PASSWORD=off: password sign-in and first-run setup are off; people sign in with OIDC")
            return
        if self.has_password():
            return
        self.setup_code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        log.warning("No admin password yet. Open the panel and use this setup code to set one: %s", self.setup_code)

    def set_password(self, password: str) -> None:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            raise ValueError(f"use at least {MIN_PASSWORD} characters")
        salt = secrets.token_bytes(16)
        self.store.set_setting("auth", {"hash": _scrypt(password, salt), "salt": salt.hex(), "set": time.time()})

    def check_password(self, password: str) -> bool:
        a = self.store.setting("auth") or {}
        if not password_enabled() or not a.get("hash") or not isinstance(password, str):
            return False
        return _same(_scrypt(password, bytes.fromhex(a["salt"])), a["hash"])

    def setup(self, code: str, password: str) -> str:
        """First run: the code from the log plus a new password. Returns a session."""
        if not password_enabled():
            raise PermissionError("password sign-in is off on this panel")
        if self.has_password():
            raise PermissionError("a password is already set; sign in instead")
        if not self.setup_code or not isinstance(code, str) or not _same(code.strip().upper(), self.setup_code):
            raise PermissionError("wrong setup code (it is in the panel's log)")
        self.set_password(password)
        self.setup_code = None
        self.store.event("auth", "the admin password was set")
        return self.new_session("setup")

    # -- sign-in throttle --------------------------------------------------------------------------------------------
    def locked_for(self, addr: str) -> float:
        _, until, _ = self._fails.get(addr, (0, 0.0, 0.0))
        return max(0.0, until - time.time())

    def failed(self, addr: str) -> None:
        now = time.time()
        if len(self._fails) > 1000:               # forget addresses whose last wait is long over
            self._fails = {k: v for k, v in self._fails.items() if v[1] + LOCK_MAX_S > now}
        n, until, wait = self._fails.get(addr, (0, 0.0, 0.0))
        n += 1
        if n >= MAX_FAILS:
            wait = min(LOCK_MAX_S, wait * 2 if wait else LOCK_S)
            until, n = time.time() + wait, 0
        self._fails[addr] = (n, until, wait)

    def succeeded(self, addr: str) -> None:
        self._fails.pop(addr, None)

    # -- sessions ----------------------------------------------------------------------------------------------------
    def _sessions(self) -> dict:
        return self.store.setting("auth_sessions") or {}

    def new_session(self, agent: str = "", user: str | None = None, sub: str | None = None) -> str:
        """A person's session. `user` and `sub` are who an OIDC provider said signed in: `sub` is the provider's
        stable id for the account, `user` its display name (the account may choose it). The password has no name."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        live = {k: v for k, v in self._sessions().items() if now - v.get("created", 0) < SESSION_DAYS * 86400}
        live[_sha(token)] = {"created": now, "seen": now, "agent": (agent or "")[:120]}
        if user:
            live[_sha(token)]["user"] = user[:60]
        if sub:
            live[_sha(token)]["sub"] = sub[:120]
        self.store.set_setting("auth_sessions", live)
        return token

    def session_ok(self, token: str | None) -> bool:
        if not token:
            return False
        sessions = self._sessions()
        s = sessions.get(_sha(token)) if isinstance(token, str) else None
        now = time.time()
        if not s or now - s.get("created", 0) > SESSION_DAYS * 86400:      # as long as the cookie, never extended
            return False
        if now - s.get("seen", 0) > SEEN_EVERY_S:
            s["seen"] = now
            self.store.set_setting("auth_sessions", sessions)
        return True

    def end_session(self, token: str | None) -> None:
        sessions = self._sessions()
        if token and sessions.pop(_sha(token), None) is not None:
            self.store.set_setting("auth_sessions", sessions)

    def end_all_sessions(self) -> None:
        self.store.set_setting("auth_sessions", {})

    # -- API tokens --------------------------------------------------------------------------------------------------
    def tokens(self) -> list[dict]:
        return [{**{k: t.get(k) for k in ("id", "name", "hint", "created", "used")}, "scope": _scope(t)}
                for t in self.store.setting("api_tokens") or []]

    def new_token(self, name: str, scope: str = "full") -> dict:
        name = (name or "").strip()[:60]
        if not name:
            raise ValueError("give the token a name (what will use it)")
        if not isinstance(scope, str) or scope not in SCOPES:
            raise ValueError("scope must be 'full' or 'metrics'")
        token = "gk_" + secrets.token_urlsafe(32)
        rows = self.store.setting("api_tokens") or []
        row = {"id": secrets.token_hex(4), "name": name, "sha256": _sha(token), "hint": token[-4:],
               "created": time.time(), "used": None, "scope": scope}
        self.store.set_setting("api_tokens", rows + [row])
        self.store.event("auth", f"API token '{name}'{' (metrics only)' if scope == 'metrics' else ''} was created")
        return {"token": token, **{k: row[k] for k in ("id", "name", "hint", "created", "scope")}}

    def revoke_token(self, token_id: str) -> bool:
        rows = self.store.setting("api_tokens") or []
        keep = [t for t in rows if t.get("id") != token_id]
        if len(keep) == len(rows):
            return False
        self.store.set_setting("api_tokens", keep)
        self.store.event("auth", f"API token '{next(t['name'] for t in rows if t.get('id') == token_id)}' was revoked")
        return True

    def token_ok(self, header: str | None) -> bool:
        return self.token_scope(header) is not None

    def token_scope(self, header: str | None) -> str | None:
        """The scope ('full' or 'metrics') of the API token in this Authorization header; None if it is not one."""
        if not header or not header.lower().startswith("bearer "):
            return None
        want = _sha(header[7:].strip())
        rows = self.store.setting("api_tokens") or []
        for t in rows:
            if _same(t.get("sha256", ""), want):
                now = time.time()
                if not t.get("used") or now - t["used"] > SEEN_EVERY_S:
                    t["used"] = now
                    self.store.set_setting("api_tokens", rows)
                return _scope(t)
        return None

    # -- signed links ------------------------------------------------------------------------------------------------
    def _key(self) -> bytes:
        key = self.store.setting("auth_key")
        if not key:
            key = secrets.token_hex(32)
            self.store.set_setting("auth_key", key)
        return bytes.fromhex(key)

    def _sig(self, path: str, exp: int) -> str:
        return hmac.new(self._key(), f"{path}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]

    def sign_path(self, path: str, ttl: int = SIGNED_TTL_S) -> str:
        """`path?exp=...&sig=...`: opens this one path, without a session, until it expires. Only SIGNED_PREFIXES."""
        if not path.startswith(SIGNED_PREFIXES) or "?" in path:
            raise ValueError(f"{path} cannot be signed")
        exp = int(time.time() + ttl)
        return f"{path}?exp={exp}&sig={self._sig(path, exp)}"

    def signed_ok(self, path: str, query: str) -> bool:
        if not path.startswith(SIGNED_PREFIXES):
            return False
        q = parse_qs(query or "")
        try:
            exp = int((q.get("exp") or [""])[0])
        except ValueError:
            return False
        return exp > time.time() and _same((q.get("sig") or [""])[0], self._sig(path, exp))

    # -- the gate ----------------------------------------------------------------------------------------------------
    def who(self, method: str, path: str, headers, cookies, query: str = "") -> str | None:
        """'internal', 'token', 'metrics' (a metrics-only token), 'session', 'signed' or 'open' when the request may go
        on; None when it must sign in. Only 'token' may ask for robot commands: nothing treats 'metrics' as one."""
        if disabled() or not needs_auth(path):
            return "open"
        if _same(headers.get(INTERNAL_HEADER), INTERNAL):
            return "internal"
        scope = self.token_scope(headers.get("authorization"))
        if scope is not None:
            return "metrics" if scope == "metrics" else "token"
        if self.session_ok(cookies.get(COOKIE)):
            return "session"
        if method in ("GET", "HEAD") and self.signed_ok(path, query):
            return "signed"
        return None


def _scope(row: dict) -> str:
    """A row's scope. Anything but exactly 'metrics' is full, so a row from before scopes behaves as it always did."""
    return "metrics" if row.get("scope") == "metrics" else "full"


def token_may(method: str, path: str) -> bool:
    """May a full API token make this request? Any read, and the short list of writes."""
    return method in SAFE_METHODS or (method, path) in TOKEN_WRITES


def metrics_may(method: str, path: str) -> bool:
    """May a metrics-only token make this request? Only GET or HEAD on exactly /api/metrics."""
    return method in ("GET", "HEAD") and path == METRICS_PATH


def same_origin(method: str, headers, panel_url: str | None = None) -> bool:
    """A cookie-signed change must come from the panel's own pages: a browser always sends Origin on a cross-site
    POST/PUT/DELETE, and it has to name this host: the Host header, the first X-Forwarded-Host (a reverse proxy),
    or PANEL_URL (a proxy that rewrites Host and says nothing). SameSite=Lax already keeps the cookie off most."""
    if method in SAFE_METHODS:
        return True
    origin = headers.get("origin")
    if not origin:
        return True
    host = urlsplit(origin).netloc.lower()
    ok = {(headers.get("host") or "").lower(), (headers.get("x-forwarded-host") or "").split(",")[0].strip().lower()}
    if panel_url:
        ok.add(urlsplit(panel_url).netloc.lower())
    return bool(host) and host in ok


def main(argv: list[str]) -> int:
    """`python -m app.auth reset`: forget the password, every session and every API token."""
    if argv[1:] != ["reset"]:
        print("usage: python -m app.auth reset", file=sys.stderr)
        return 2
    from .config import load
    from .store import Store
    store = Store(load().data_dir)
    for key in ("auth", "auth_sessions", "api_tokens"):
        store.delete_setting(key)
    store.event("auth", "the password, sessions and API tokens were reset from the command line")
    print("Reset. Restart the panel; its log will show a new setup code.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
