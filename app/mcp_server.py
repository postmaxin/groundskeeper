"""MCP server: the panel for AI assistants (Claude Desktop, Claude Code, any MCP client), at /mcp (streamable HTTP).

Off until someone creates a token in Settings → AI assistant; every request then needs `Authorization: Bearer <token>`.
Only the token's SHA-256 is stored.

Tools come in three tiers, and every one goes through the same code as the UI (the REST handlers, called in-process,
or the Controller), so validation, interlocks and the audit log are the same:
  - read: status, runs, conditions, schedule, maintenance, events, settings, imagery, help pages;
  - setup: imagery choice among catalog sources, keys, alignment; polling; run rules (tighter only); yard cameras
    Frigate knows (add only); the HA notify service; schedule windows; maintenance. Each needs confirm=true and says
    what it would do without it. The assistant sets confirm itself, so it is a prompt to ask the user, not proof that
    they agreed: anything that could loosen safety, blind a check or send the yard somewhere new is not offered.
  - control: start a plan, pause, resume, stop, return to dock. Needs a third switch, "Assistant commands"
    (Settings → Safety, off by default), on top of the control switch and every interlock. An assistant can never
    override a check, and for its start and resume an unknown counts as a fail (control.check, actor "mcp").
Never exposed: the safety switches, guard auto-resume, loosening any run rule, auto-start windows, the robot, Frigate,
Home Assistant and weather connections, the presence entity, camera coverage, the webhook, trying or saving a new
imagery source (that sends the yard: a person does it in Settings), map writes, robot backups, the cloud sign-in,
manual drive.

In-process calls may only reach the (method, path) pairs in ROUTES, and every id a tool puts in a path must match
SAFE_ID, so no argument can steer a call to another endpoint.

Privacy: results lose keys that hold places or identities (PRIVATE_KEYS), lists of numbers that look like coordinates,
and, in text, everything of a URL after its host, long decimals, serial numbers and email addresses. So an assistant cannot pass the home location on to a web
search or another service. Imagery tools describe the yard by the regions of the catalog outlines that contain it.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import socket
import time
from urllib.parse import urlsplit
from typing import Any, Awaitable, Callable

import httpx
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import auth, version

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
SETUP = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
REACHES_OUT = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
CONTROL = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

TOKEN_SETTING = "mcp_token"
SWITCH = "mcp_control_enabled"
COMMANDS = ("start_plan", "pause", "resume", "stop", "cmd_recharge")
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Every REST call an assistant can cause. Ids are matched by SAFE_ID's character class.
_ID = r"[A-Za-z0-9_-]{1,64}"
ROUTES = [(m, re.compile(p, re.ASCII)) for m, p in [
    ("GET", r"/api/(status|plans|runs|conditions|schedule|maintenance|events|settings|help|imagery|imagery/sources|imagery/kinds|frigate/cameras|setup/status)"),
    ("GET", r"/api/runs/\d{1,9}"),
    ("GET", rf"/api/help/{_ID}"),
    ("GET", rf"/api/imagery/sources/{_ID}/preview\.jpg"),
    ("POST", r"/api/imagery/(discover|probe)"),
    ("POST", r"/api/integrations/(robot|frigate|home_assistant|weather)/test"),
    ("POST", rf"/api/maintenance/{_ID}/done"),
    ("POST", r"/api/notify/test"),
    ("PUT", r"/api/imagery/(source|key)"),
    ("PUT", r"/api/settings/(imagery_alignment|gates|yard_cameras|ha_notify_service)"),
    ("PUT", r"/api/integrations/panel"),
    ("PUT", r"/api/(schedule/windows|maintenance/items)"),
]]

# Integration fields an assistant may set: polling only. The robot, Frigate (yard clear), Home Assistant (presence)
# and the weather station (lawn, rain guard) feed the checks, so their connections stay with the user.
INTEGRATION_FIELDS = {"panel": {"poll_idle_s", "poll_active_s", "map_refresh_s", "raw_every_s", "raw_keep_days",
                                "run_idle_end_s"}}
SETTING_KEYS = ("gates", "yard_cameras", "ha_notify_service")
NOTIFY_SERVICE = re.compile(r"notify\.[a-z0-9_]{1,64}", re.ASCII)
YARD_LABELS = {"person", "dog", "cat", "bird", "horse", "sheep", "cow", "bear", "deer", "fox", "rabbit", "raccoon",
               "squirrel", "goat", "bicycle", "motorcycle", "car"}
MAX_LIST = 32

# Run rules an assistant may only make stricter. Booleans: the safe value. Numbers: up only. Guards: by rank.
GATE_SAFE_BOOL = {"require_dry": True, "presence_required": True, "yard_clear_required": True, "allow_damp": False}
GATE_UP_ONLY = ("min_battery_pct", "yard_clear_minutes", "phone_app_quiet_s", "guard_resume_after_min")
GUARD_RANK = {"ignore": 0, "notify": 1, "pause": 2, "dock": 3}
NEVER_GATES = ("guard_resume",)

# Keys whose values are a place or an identity: removed from everything an assistant reads.
PRIVATE_KEYS = {"lat", "lon", "lng", "latitude", "longitude", "bbox", "bounds", "center", "centre", "position", "pos",
                "home", "gps_ref", "ref", "ref_lat", "ref_lon", "where", "landmark", "yard_centre", "yard_meta", "station",
                "coordinates", "geometry", "points", "rows", "track", "polygon", "polygons", "camera_polygons", "ring",
                "rings", "extent", "iccid", "sn", "serial", "username", "email", "broker", "client_id", "token", "key",
                "keys", "password", "custom", "local_catalog", "data_dir", "env"}
_URL = re.compile(r"(https?://[^\s/?#\"'<>]+)[^\s\"'<>]*")
_LONG_DECIMAL = re.compile(r"-?\d{1,3}\.\d{4,}|-?\d{5,}\.\d+")
_SERIAL = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{12,24}\b|\b\d{15,22}\b")   # robot SN, ICCID
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Strings kept as they are: catalog addresses (public, never carry a place) and a draft entry the assistant pasted.
TEXT_KEEP_KEYS = {"url", "homepage", "signup", "entry"}

INSTRUCTIONS = """This is a self-hosted panel for one Yarbo robot mower: it records the robot, maps its runs and can
control it under interlocks. Use the read tools freely. Setup tools change the panel's own settings: tell the user
what you are about to change and call them with confirm=true only after they agree. Control tools move a robot with
spinning blades: only when the user asks for that specific command. Never try to get around a refusal.

Privacy: never put the user's address or coordinates into a web search, a map viewer or any other service. To find
aerial imagery, use imagery_where (it names the region, e.g. a state or country), then search the web by region,
following the help page imagery-finding. A new imagery address is checked with imagery_discover (no location is
sent); then give the user the address to add in Settings → Map imagery, where trying it at the yard is theirs.

Finding the most detailed map of the yard (the thing users ask for most):
  1. imagery_sources: every source that covers the yard, with native_m (metres of ground per pixel; smaller is
     sharper) and whether its yard sample found real imagery. Read the scale to the user in cm, not metres:
     0.076 m = 7.6 cm (very sharp: pavers, sprinkler heads, the mower), 0.15 m = 15 cm (sharp: beds and edges clear),
     0.3 m = 30 cm (soft edges, still usable), 0.6 m = 60 cm (USGS NAIP: a lawn edge is one blurry pixel),
     1 m+ (too coarse to draw zones on). ArcGIS services quote feet: 0.25 ft = 7.6 cm, 0.5 ft = 15 cm, 1 ft = 30 cm.
  2. imagery_sample on the two or three sharpest with ok samples: a blank, cloudy, or leaf-on sample loses to a
     slightly coarser clear one. Say which you looked at and what you saw.
  3. If the sharpest usable free source is coarser than about 30 cm, search by region (imagery_where) for a county
     or state orthoimagery ImageServer / WMS, which is often 7–15 cm; check candidates with imagery_discover and hand
     the user the address for Settings → Map imagery → Add a source. Keyed worldwide services (Esri, Mapbox, MapTiler)
     are another route: the user adds a key in Settings, then imagery_sources shows their sample too.
  4. Offer imagery_use (confirm after the user agrees) for the best one, or leave Automatic on if it already picked
     it. Mention imagery_align if the photo and the robot's map look shifted.

To set the panel up with the user, follow setup_status: its steps are in order, each says whether it is done and
what is missing, and the help page setup explains each one. Steps that connect the robot, the cloud, Frigate or
Home Assistant are the user's to do in Settings → Set up; guide them there.

Start with status and the help pages (help_pages, help_page) to learn the panel."""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(store) -> str:
    """A fresh token (returned once) replaces any earlier one."""
    token = "ypm_" + secrets.token_urlsafe(32)
    store.set_setting(TOKEN_SETTING, {"sha256": token_hash(token), "created": time.time(), "hint": token[-4:]})
    store.event("mcp_token", "an AI assistant token was created (Settings → AI assistant)")
    return token


def token_status(store) -> dict:
    t = store.setting(TOKEN_SETTING) or {}
    return {"enabled": bool(t.get("sha256")), "created": t.get("created"), "hint": t.get("hint"),
            "control": bool(store.setting(SWITCH, False))}


def authorised(store, header: str | None) -> bool | None:
    """None: MCP is off (no token). Otherwise whether the Authorization header carries the token."""
    want = (store.setting(TOKEN_SETTING) or {}).get("sha256")
    if not want:
        return None
    if not header or not header.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(token_hash(header[7:].strip()), want)


def clean_text(text: str) -> str:
    """Text with URLs cut to their host (error messages quote request URLs, which carry the yard's box or tile),
    and without coordinate-like decimals, serial numbers or email addresses."""
    text = _URL.sub(r"\1/…", text)
    text = _EMAIL.sub("…@…", text)
    text = _SERIAL.sub("…", text)
    return _LONG_DECIMAL.sub("…", text)


def _looks_like_coordinates(items: list) -> bool:
    """[lon, lat], [w, s, e, n], [[lon, lat], ...]: two or more numbers, at least two with 4+ decimals."""
    if items and all(isinstance(v, list) for v in items):
        return any(_looks_like_coordinates(v) for v in items)
    if len(items) < 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in items):
        return False
    return sum(1 for v in items if isinstance(v, float) and abs(v - round(v, 3)) > 1e-9) >= 2


def scrub(value: Any, keep_text: bool = False) -> Any:
    """Everything an assistant reads, without places or identities. keep_text leaves strings as they are (a draft
    entry the assistant pasted itself and must be able to send back)."""
    if isinstance(value, dict):
        return {k: scrub(v, keep_text or k in TEXT_KEEP_KEYS) for k, v in value.items()
                if str(k).lower() not in PRIVATE_KEYS and not (isinstance(v, list) and _looks_like_coordinates(v))}
    if isinstance(value, list):
        return [scrub(v, keep_text) for v in value if not (isinstance(v, list) and _looks_like_coordinates(v))]
    if isinstance(value, str) and not keep_text:
        return clean_text(value)
    return value


def safe_id(value: Any, what: str = "id") -> str:
    value = str(value)
    if not SAFE_ID.fullmatch(value):
        raise ToolError(f"not a valid {what}: letters, digits, _ and - only")
    return value


def allowed(method: str, path: str) -> bool:
    return any(m == method and p.fullmatch(path) for m, p in ROUTES)


def tighter_gates(current: dict, change: dict) -> list[str]:
    """What in `change` would loosen a run rule (empty when it only tightens or keeps)."""
    bad = []
    for k, v in change.items():
        cur = current.get(k)
        if k in NEVER_GATES:
            bad.append(f"{k} is the user's own switch")
        elif k in GATE_SAFE_BOOL:
            if v != GATE_SAFE_BOOL[k] and v != cur:
                bad.append(f"{k} may only be set to {GATE_SAFE_BOOL[k]}")
        elif k in GATE_UP_ONLY:
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) \
                    or (isinstance(cur, (int, float)) and v < cur):
                bad.append(f"{k} may only go up (now {cur})")
        elif k == "guard_after_dark" and v == "pause":
            bad.append("guard_after_dark may be dock, notify or ignore (a pause would leave the robot out all night)")
        elif k.startswith("guard_"):
            if GUARD_RANK.get(v, -1) < GUARD_RANK.get(cur, 0):
                bad.append(f"{k} may not go from {cur} to {v}")
        elif k == "yard_labels":
            if not isinstance(v, list) or not set(cur or []) <= set(v) or not set(v) <= YARD_LABELS \
                    or len(v) > MAX_LIST:
                bad.append(f"yard_labels may only gain labels from: {', '.join(sorted(YARD_LABELS))}")
        else:
            bad.append(f"{k} is not a run rule an assistant can change")
    return bad


async def public_host(url: str) -> None:
    """Refuse an address on this network: discovery reaches out from the panel's server, and must not probe the LAN."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        host = None
    if not host or urlsplit(url).scheme not in ("http", "https"):
        raise ToolError("an http(s) address")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        raise ToolError(f"{host} does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ToolError(f"{host} is on a private network; add a local source in Settings → Map imagery yourself")


def _needs_confirm(what: str, **details) -> dict:
    return {"done": False, "needs_confirm": True,
            "would": what, **({"details": details} if details else {}),
            "next": "Tell the user what this changes; call again with confirm=true if they agree."}


Api = Callable[..., Awaitable[Any]]


class PanelMCP:
    """The MCP server (mcp 2.x MCPServer). `api(method, path, json=None, params=None, raw=False)` calls the panel's REST handlers;
    `state()` returns app.state (store, ops). Both are injectable for tests; the route check applies to either."""

    def __init__(self, app=None, api: Api | None = None, state: Callable[[], Any] | None = None):
        self.app = app
        self._call = api or self._asgi
        self._state = state or (lambda: app.state)
        # No subscriptions: the panel publishes no resources, and a standing stream would outlive a revoked token.
        self.mcp = MCPServer("groundskeeper", instructions=INSTRUCTIONS, version=version.__version__, subscriptions=False)
        self._register()

    # -- plumbing ----------------------------------------------------------------------------------------------------
    async def _asgi(self, method: str, path: str, json: Any = None, params: dict | None = None, raw: bool = False):
        transport = httpx.ASGITransport(app=self.app)
        # The panel's sign-in gate lets this in-process call through; the ROUTES allowlist above is its limit.
        headers = {auth.INTERNAL_HEADER: auth.INTERNAL}
        async with httpx.AsyncClient(transport=transport, base_url="http://panel", timeout=180, headers=headers) as c:
            r = await c.request(method, path, json=json, params=params)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail") or r.json().get("error")
            except ValueError:
                detail = r.text[:300]
            raise ToolError(clean_text(f"{r.status_code}: {detail}"))
        if raw:
            return r.content
        return r.json() if r.content else None

    async def _api(self, method: str, path: str, **kw):
        """The one way to the REST layer: only allow-listed routes, and the path must be exactly what was asked."""
        if not allowed(method, path) or httpx.URL("http://panel" + path).raw_path.decode() != path:
            raise ToolError(f"{method} {path} is not open to assistants")
        return await self._call(method, path, **kw)

    async def api(self, method: str, path: str, **kw):
        return scrub(await self._api(method, path, **kw))

    @property
    def store(self):
        return self._state().rec.store

    # -- tools -------------------------------------------------------------------------------------------------------
    def _register(self) -> None:  # noqa: C901 - one place lists every tool
        t = self.mcp.tool

        # read ------------------------------------------------------------------------------------------------------
        @t(annotations=READ)
        async def status() -> dict:
            """The robot and the panel now: state, battery, positioning, link, the open run, guards, lawn."""
            return await self.api("GET", "/api/status")

        @t(annotations=READ)
        async def setup_status() -> dict:
            """The first-run steps in order (robot, cloud, AI assistant, imagery, cameras, Home Assistant, weather,
            safety, notifications): whether each is done, what is missing, and which are optional."""
            return await self.api("GET", "/api/setup/status")

        @t(annotations=READ)
        async def readiness(plan_id: int | None = None) -> dict:
            """Would a start go out right now? Runs every interlock for start_plan without sending anything. Without
            plan_id the first plan on the robot is used."""
            if plan_id is None:
                items = ((await self.api("GET", "/api/plans")) or {}).get("plans") or []
                if not items:
                    return {"would_send": False, "reason": "there are no plans on the robot (make one in the Yarbo app)"}
                plan_id = items[0].get("id")
            out = await self._state().ops.controller.preview("start_plan", {"plan_id": plan_id}, actor="mcp")
            if not self.store.setting(SWITCH, False):
                out = {**out, "would_send": False, "reason": "assistant commands are off (Settings → Safety)"}
            return scrub(out)

        @t(annotations=READ)
        async def plans() -> Any:
            """The plans saved on the robot (the panel cannot create plans yet)."""
            return await self.api("GET", "/api/plans")

        @t(annotations=READ)
        async def runs(limit: int = 20) -> dict:
            """Recent runs, newest first: duration, distance, RTK share, coverage, gaps, end reason."""
            return await self.api("GET", "/api/runs", params={"limit": max(1, min(int(limit), 200))})

        @t(annotations=READ)
        async def run(run_id: int) -> dict:
            """One run's details and statistics."""
            return await self.api("GET", f"/api/runs/{int(run_id)}")

        @t(annotations=READ)
        async def lawn_conditions() -> dict:
            """Dry / Damp / Wet with the rules behind it and the expected dry-by time."""
            return await self.api("GET", "/api/conditions")

        @t(annotations=READ)
        async def schedule() -> dict:
            """Schedule windows, what is coming up, a pending confirmation, and each window's recent outcomes."""
            return await self.api("GET", "/api/schedule")

        @t(annotations=READ)
        async def maintenance() -> dict:
            """Maintenance items with their counters, intervals and due state."""
            return await self.api("GET", "/api/maintenance")

        @t(annotations=READ)
        async def events(limit: int = 100) -> dict:
            """The event log, newest first (commands sent or refused, runs, map, panel)."""
            return await self.api("GET", "/api/events", params={"limit": max(1, min(int(limit), 1000))})

        @t(annotations=READ)
        async def settings() -> dict:
            """The panel's settings: run rules (gates), lawn rules, cameras, integrations (secrets are never shown)."""
            return await self.api("GET", "/api/settings")

        @t(annotations=READ)
        async def help_pages() -> dict:
            """The panel's own help pages (the same ones the user sees under Help)."""
            return await self.api("GET", "/api/help")

        @t(annotations=READ)
        async def help_page(page_id: str) -> dict:
            """One help page as markdown. Ids come from help_pages; imagery-finding is the guide to hunting down the
            most detailed imagery for a place (where to search, what the cm numbers mean, traps); mcp is what this
            server may and may not do."""
            return await self._api("GET", f"/api/help/{safe_id(page_id, 'page id')}")      # repo files: no places

        # imagery ---------------------------------------------------------------------------------------------------
        @t(annotations=READ)
        async def imagery() -> dict:
            """The imagery in use: source, mode (auto or the user's choice), resolution, credit, alignment."""
            return await self.api("GET", "/api/imagery")

        @t(annotations=READ)
        async def imagery_sources() -> dict:
            """Every catalog source that covers the yard, with what its yard sample found (ok, blank, error),
            resolution, licence and whether it needs a key; then the rest of the catalog. native_m is metres of
            ground per pixel, smaller is sharper: 0.076 = 7.6 cm (very sharp), 0.15 = 15 cm (sharp), 0.3 = 30 cm
            (soft edges), 0.6 = 60 cm (blurry). Tell the user in cm. Sorting by native_m among sources whose probe
            is ok gives the most detailed usable map; see the server instructions for the full method."""
            return await self.api("GET", "/api/imagery/sources")

        @t(annotations=READ)
        async def imagery_where() -> dict:
            """Which regions the yard is in, by the catalog outlines that contain it (e.g. "Maryland",
            "United States (lower 48)"), without coordinates. Use these names to search the web for a sharper
            county or state orthoimagery service (often 7–15 cm) when the free sources here are 30 cm or coarser."""
            data = await self._api("GET", "/api/imagery/sources")
            here = [s for s in (data or {}).get("sources", []) if s.get("covers")]
            regions = sorted({s.get("region") for s in here if s.get("region") and s.get("region") != "Worldwide"})
            free = [s for s in here if not s.get("needs_key")]
            return {"regions": regions,
                    "countries": sorted({s.get("country") for s in here if s.get("country") not in (None, "XX")}),
                    "free_sources_here": [s.get("id") for s in free],
                    "note": None if regions else "No regional source covers the yard; ask the user which country "
                                                 "and state or province they are in."}

        @t(annotations=READ)
        async def imagery_sample(source_id: str) -> Image:
            """The 40 m sample of the yard a source returned (JPEG), to judge sharpness and season. Only sources
            already sampled (see imagery_sources). Look at the two or three sharpest before recommending one: a
            clear 15 cm sample beats a cloudy or leaf-on 7.6 cm one."""
            data = await self._api("GET", f"/api/imagery/sources/{safe_id(source_id, 'source id')}/preview.jpg",
                                   raw=True)
            if not data.startswith(b"\xff\xd8\xff"):
                raise ToolError("no sample for that source")
            return Image(data=data, format="jpeg")

        @t(annotations=READ)
        async def imagery_kinds() -> Any:
            """The protocols a source can use (tiles, wms, arcgis_image, arcgis_map) and each one's fields."""
            return await self.api("GET", "/api/imagery/kinds")

        @t(annotations=REACHES_OUT)
        async def imagery_discover(url: str) -> dict:
            """Ask an imagery address on the internet what it serves and draft a catalog entry. Contacts only that
            address and never sends a location. To use what it finds, give the user the address: they paste it into
            Settings → Map imagery → Add a source, where trying it at the yard is their own step."""
            await public_host(url)
            return await self.api("POST", "/api/imagery/discover", json={"url": url})

        @t(annotations=SETUP)
        async def imagery_use(source_id: str | None = None, automatic: bool = False, confirm: bool = False) -> dict:
            """Choose the imagery: a source id (the user's choice) or automatic=true (the sharpest free source with
            real imagery at the yard). The yard image is then fetched from that source."""
            if not automatic and not source_id:
                raise ToolError("give source_id, or automatic=true")
            body = {"mode": "auto"} if automatic else {"provider": safe_id(source_id, "source id")}
            if not confirm:
                return _needs_confirm("use automatic imagery" if automatic else f"use the imagery source {source_id}")
            return await self.api("PUT", "/api/imagery/source", json=body)

        @t(annotations=SETUP)
        async def imagery_set_key(source_id: str, key: str | None, confirm: bool = False) -> dict:
            """Store (or clear, with key=null) the user's API key for a keyed source; it is then sampled at the
            yard. Keys stay on the server and are never shown again."""
            source_id = safe_id(source_id, "source id")
            if not confirm:
                return _needs_confirm(f"{'store' if key else 'clear'} the key for {source_id}"
                                      + (" and sample the yard from it" if key else ""))
            return await self.api("PUT", "/api/imagery/key", json={"provider": source_id, "key": key})

        @t(annotations=SETUP)
        async def imagery_check_again(confirm: bool = False) -> dict:
            """Sample every source that covers the yard again (and re-pick in automatic mode)."""
            if not confirm:
                return _needs_confirm("sample every imagery source that covers the yard again")
            return await self.api("POST", "/api/imagery/probe", json={})

        @t(annotations=SETUP)
        async def imagery_align(east_m: float = 0.0, north_m: float = 0.0, rotate_deg: float = 0.0, scale: float = 1.0,
                                confirm: bool = False) -> dict:
            """Shift, rotate and scale the imagery under the robot's map (kept per source; the robot's map never
            moves). Limits: ±50 m, ±20°, scale 0.95–1.05."""
            body = {"east_m": east_m, "north_m": north_m, "rotate_deg": rotate_deg, "scale": scale}
            if not confirm:
                return _needs_confirm("move the imagery under the map", **body)
            return await self.api("PUT", "/api/settings/imagery_alignment", json=body)

        # setup -----------------------------------------------------------------------------------------------------
        @t(annotations=SETUP)
        async def set_integration(group: str, values: dict, confirm: bool = False) -> dict:
            """Set the panel's polling: panel {poll_idle_s, poll_active_s, map_refresh_s, raw_every_s, raw_keep_days,
            run_idle_end_s}. The robot, Frigate, Home Assistant and weather connections feed the safety checks, so
            the user sets them in Settings."""
            fields = INTEGRATION_FIELDS.get(group)
            if fields is None:
                raise ToolError(f"{group!r} is set by the user in Settings; assistants may set: {', '.join(INTEGRATION_FIELDS)}")
            extra = set(values) - fields
            if extra:
                raise ToolError(f"not settable here: {', '.join(sorted(extra))}")
            if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0
                   for v in values.values()):
                raise ToolError("each value a positive number")
            if not confirm:
                shown = {k: ("•••" if k == "token" else v) for k, v in values.items()}
                return _needs_confirm(f"change the {group} integration", **shown)
            return await self.api("PUT", f"/api/integrations/{group}", json=values)

        @t(annotations=READ)
        async def test_integration(group: str) -> dict:
            """Test an integration's connection with its saved values (robot, frigate, home_assistant, weather)."""
            if group not in ("robot", "frigate", "home_assistant", "weather"):
                raise ToolError("one of robot, frigate, home_assistant, weather")
            return await self.api("POST", f"/api/integrations/{group}/test", json={})

        @t(annotations=SETUP)
        async def set_setting(key: str, value: Any, confirm: bool = False) -> dict:
            """Change one panel setting: gates (run rules: stricter only); yard_cameras (add cameras Frigate has);
            ha_notify_service (a notify.* service). The safety switches, guard auto-resume, lawn rules, camera
            coverage, presence and anything that loosens a check stay with the user."""
            if key not in SETTING_KEYS:
                raise ToolError(f"{key!r} is set by the user in Settings; assistants may set: {', '.join(SETTING_KEYS)}")
            current = (await self._api("GET", "/api/settings")) or {}
            if key == "gates":
                if not isinstance(value, dict):
                    raise ToolError("gates: an object of rule: value")
                bad = tighter_gates(current.get("gates") or {}, value)
                if bad:
                    raise ToolError("assistants may only make run rules stricter: " + "; ".join(bad))
            elif key == "yard_cameras":
                known = set(((await self._api("GET", "/api/frigate/cameras")) or {}).get("cameras") or [])
                if not isinstance(value, list) or len(value) > MAX_LIST \
                        or not set(current.get("yard_cameras") or []) <= set(value) or not set(value) <= known:
                    raise ToolError("assistants may only add cameras Frigate has, never remove one")
            elif key == "ha_notify_service":
                if not isinstance(value, str) or not NOTIFY_SERVICE.fullmatch(value):
                    raise ToolError("a notify service like notify.mobile_app_phone")
            if not confirm:
                return _needs_confirm(f"change the setting {key}", value=scrub(value))
            return await self.api("PUT", f"/api/settings/{key}", json=value)

        @t(annotations=SETUP)
        async def set_schedule_windows(windows: list[dict], confirm: bool = False) -> dict:
            """Replace the schedule windows (days, start, end, plan_id, head, enabled). Each start still needs the
            user's confirmation. Auto-start windows are the user's: they may be kept exactly as they are, or left out
            to remove them, never made or changed."""
            current = {w.get("id"): w for w in ((await self._api("GET", "/api/schedule")) or {}).get("windows", [])}
            ids = [w.get("id") for w in windows if w.get("id") is not None]
            if len(ids) != len(set(ids)):
                raise ToolError("each window id at most once")
            for w in windows:
                was = current.get(w.get("id")) or {}
                if (w.get("auto_start") or was.get("auto_start")) and w != was:
                    raise ToolError("auto-start windows skip the user's confirmation; only the user makes or changes them")
            if not confirm:
                return _needs_confirm(f"replace the schedule with {len(windows)} window(s)", windows=windows)
            return await self.api("PUT", "/api/schedule/windows", json=windows)

        @t(annotations=SETUP)
        async def set_maintenance_items(items: list[dict], confirm: bool = False) -> dict:
            """Replace the maintenance items (name, unit, interval, note); see maintenance for the current list. When
            each was last done stays as recorded (use maintenance_done)."""
            have = {i.get("id"): i for i in ((await self._api("GET", "/api/maintenance")) or {}).get("items", [])}
            items = [{**i, **({k: have[i["id"]][k] for k in ("done_at", "baseline", "key") if k in have[i["id"]]}
                              if i.get("id") in have else {})} for i in items]
            if not confirm:
                return _needs_confirm(f"replace the maintenance list with {len(items)} item(s)")
            return await self.api("PUT", "/api/maintenance/items", json=items)

        @t(annotations=SETUP)
        async def maintenance_done(item_id: str, confirm: bool = False) -> dict:
            """Mark a maintenance item done now (its count starts again)."""
            item_id = safe_id(item_id, "item id")
            if not confirm:
                return _needs_confirm(f"mark maintenance item {item_id} done")
            return await self.api("POST", f"/api/maintenance/{item_id}/done", json={})

        @t(annotations=REACHES_OUT)
        async def send_test_notification(confirm: bool = False) -> dict:
            """Send a test alert through the configured notification channels."""
            if not confirm:
                return _needs_confirm("send a test notification to the user's phone or webhook")
            return await self.api("POST", "/api/notify/test", json={})

        # control ---------------------------------------------------------------------------------------------------
        @t(annotations=CONTROL)
        async def robot_command(command: str, plan_id: int | None = None, confirm: bool = False) -> dict:
            """Send one command to the robot: start_plan (with plan_id), pause, resume, stop, or cmd_recharge
            (return to dock). Only when the user asks for it. Needs confirm=true, the "Assistant commands" switch and
            the control switch (Settings → Safety), and every interlock; none can be overridden, and a resume faces
            the full start checks. Use readiness first to see what would block a start."""
            if command not in COMMANDS:
                raise ToolError(f"{command!r} is not a command; one of {', '.join(COMMANDS)}")
            args = {"plan_id": plan_id} if command == "start_plan" else ({"cmd": 2} if command == "cmd_recharge" else {})
            if not self.store.setting(SWITCH, False):
                self.store.log_control("mcp", "cloud", command, json.dumps(args),
                                       {"sent": False, "cmd": command, "refused": "assistant commands are off"})
                raise ToolError("assistant commands are off: the user switches them on in Settings → Safety")
            if not confirm:
                return _needs_confirm(f"send {command}" + (f" for plan {plan_id}" if plan_id is not None else "")
                                      + " to the robot")
            return scrub(await self._state().ops.controller.send(command, args, actor="mcp", wait="background"))

    # -- the HTTP endpoint -------------------------------------------------------------------------------------------
    def routes(self):
        """The /mcp route, behind the token check. Call once; mount before any catch-all route."""
        inner = self.mcp.streamable_http_app(
            stateless_http=True, json_response=True, streamable_http_path="/mcp",
            # The bearer token is the protection; the panel is reached by its LAN address, not a fixed host name.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
        store = lambda: self.store                                   # noqa: E731
        routes = []
        for r in inner.routes:
            r.app = _Guarded(r.app, store)
            routes.append(r)
        return routes


class _Guarded:
    """ASGI wrapper: 404 while MCP is off (no token), 401 without the right bearer token, 405 for anything but POST.
    The server is stateless JSON: a GET would only open a standing event stream, which a later revoke would not close
    (the token is checked when a request starts)."""

    def __init__(self, app, store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1") or None
            ok = authorised(self.store(), header)
            if ok is True and scope.get("method") != "POST":
                await send({"type": "http.response.start", "status": 405,
                            "headers": [(b"content-type", b"application/json"), (b"allow", b"POST")]})
                await send({"type": "http.response.body", "body": json.dumps({"detail": "POST only"}).encode()})
                return
            if ok is not True:
                code, msg = (404, "MCP is off: create a token in Settings → AI assistant") if ok is None \
                    else (401, "a bearer token is required (Settings → AI assistant)")
                headers = [(b"content-type", b"application/json")] + ([(b"www-authenticate", b"Bearer")] if code == 401 else [])
                await send({"type": "http.response.start", "status": code, "headers": headers})
                await send({"type": "http.response.body", "body": json.dumps({"detail": msg}).encode()})
                return
        await self.app(scope, receive, send)
