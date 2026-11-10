"""HTTP API and static UI.

Only three endpoints can change the robot. /api/control/send and /api/schedule/pending/confirm go through
app.control.Controller (catalogue, master switch off by default, interlocks, audit log). /api/studio/draft/apply
goes through app.mapwrite.MapWriter (its own switch, off by default; docked and idle; only commands captured from
the Yarbo app; read back and rolled back on any mismatch; audit log). Everything else reads, or changes this
panel's own settings."""
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import areasettings
from . import auth as auth_mod
from . import oidc as oidc_mod
from . import backups as backups_mod
from . import corrections
from . import robotscan
from . import config, control, coverage, geo, insights, mapstudio, mapwrite, telemetry
from . import ha as ha_mod
from . import help as help_mod
from . import video as video_mod
from . import weather as weather_mod
from . import why as why_mod
from .version import version_info
from .conditions import ConditionsConfig
from .elevation import Elevation
from . import planned as planned_mod
from .imagery import DEFAULT_SOURCE, Imagery, ImageryError
from .imagery import share
from . import tidy
from .imagery.catalog import custom_entry
from .imagery.discover import discover
from . import mcp_server
from .ops import Ops
from .recorder import Recorder
from .runtime import GROUPS, Runtime
from .store import Store
from .transport_local import discover_sn

log = logging.getLogger("yarbo_panel")
WEB = Path(__file__).resolve().parent.parent / "web"


def _ui_version() -> str:
    """A hash of every file the browser loads. It changes with a redeploy that changes the UI, and the status stream
    carries it, so a page that is already open can tell it is out of date and reload itself."""
    h = hashlib.sha1()
    for p in sorted(WEB.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(WEB)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


UI_VERSION = _ui_version()
_LOCAL_ASSET = re.compile(r'((?:src|href)=")(/[^"?#]+\.(?:js|css))(")')


def stamp(html: str) -> str:
    """Every local script and stylesheet gets ?v=<UI_VERSION> (so no browser cache outlives a redeploy), and the page
    says which version it is."""
    html = _LOCAL_ASSET.sub(lambda m: f"{m.group(1)}{m.group(2)}?v={UI_VERSION}{m.group(3)}", html)
    return html.replace("<head>", f'<head>\n<meta name="ui-version" content="{UI_VERSION}">', 1)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    env = config.load()
    # Before anything starts: OIDC settings that would let anyone in, or nobody, stop the panel here (app/oidc.py).
    oidc_settings = oidc_mod.load(env.panel_url, auth_mod.password_enabled(), auth_mod.disabled())
    store = Store(env.data_dir)
    if env.demo:                                 # DEMO=1: a made-up yard and its history (app/demo.py), once
        from . import demo
        demo.prepare(store)
    cfg = Runtime(env, store)                    # Settings → integrations over the environment
    sn = cfg.yarbo_sn or None                    # else the recorder reads it from the robot's heartbeat
    imagery = Imagery(env.data_dir)
    imagery.adopt_custom((store.setting("imagery_source") or {}).get("custom"))
    imagery.configure(store.setting("imagery_source"), store.setting("imagery_keys"), store.setting("imagery_context") or {})
    recorder = Recorder(cfg, store, imagery, sn)
    ops = Ops(cfg, recorder)
    app.state.cfg, app.state.store, app.state.imagery, app.state.rec, app.state.ops = cfg, store, imagery, recorder, ops
    app.state.elevation = Elevation(env.data_dir)
    app.state.auth = auth_mod.Auth(store)
    app.state.auth.announce()
    app.state.oidc = oidc_mod.OIDC(oidc_settings, app.state.auth._key) if oidc_settings else None
    task = asyncio.create_task(recorder.run(), name="recorder")
    ops_task = asyncio.create_task(ops.start(), name="ops")
    upkeep_task = asyncio.create_task(imagery_upkeep(app), name="imagery-upkeep")
    try:
        async with panel_mcp.mcp.session_manager.run():
            yield
    finally:
        task.cancel()
        ops_task.cancel()
        upkeep_task.cancel()
        await ops.close()
        if recorder.transport:
            recorder.transport.stop()
        await imagery.close()
        await recorder.ha.close()
        if app.state.oidc:
            await app.state.oidc.close()


app = FastAPI(title="Groundskeeper", lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
              openapi_url="/api/openapi.json")       # under /api/, so behind sign-in like the docs page
panel_mcp = mcp_server.PanelMCP(app)          # /mcp for AI assistants; off until a token is made (app/mcp_server.py)
app.router.routes[0:0] = panel_mcp.routes()


@app.middleware("http")
async def signed_in(request: Request, call_next):
    """Everything with data needs a session, an API token or the MCP server's in-process call (app/auth.py)."""
    gate = getattr(request.app.state, "auth", None)
    path = request.url.path
    if gate is None:                              # startup did not finish: fail closed
        if auth_mod.disabled() or not auth_mod.needs_auth(path):
            return await call_next(request)
        return JSONResponse({"detail": "starting"}, status_code=503)
    who = gate.who(request.method, path, request.headers, request.cookies, request.url.query)
    if who is None:
        if path == "/":
            return RedirectResponse("/login.html", status_code=303)
        # The header tells the pages this 401 is the sign-in gate, not some other refusal (a wrong cloud password).
        return JSONResponse({"detail": "sign in first"}, status_code=401, headers={"X-GK-Auth": "required"})
    panel_url = getattr(getattr(request.app.state, "cfg", None), "panel_url", None)
    if who == "session" and not auth_mod.same_origin(request.method, request.headers, panel_url):
        return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    if who == "token" and not auth_mod.token_may(request.method, path):
        return JSONResponse({"detail": "an API token cannot change this: sign in with the password"}, status_code=403)
    request.state.who = who
    return await call_next(request)


@app.middleware("http")
async def revalidate_ui_files(request: Request, call_next):
    """The UI is plain files; make browsers revalidate them (ETag) so a redeploy shows up on the next load."""
    response = await call_next(request)
    path = request.url.path
    if not path.startswith(("/api/", "/tiles/", "/imagery/", "/vendor/")):
        response.headers["Cache-Control"] = "no-cache"
    return response


def rec(request: Request) -> Recorder:
    return request.app.state.rec


@app.get("/health")
async def health(request: Request):
    r = rec(request)
    s = r.status()
    # Open to anyone (Docker's health check, uptime monitors, scripts/deploy.sh's "not mid-mow" check): no data.
    return {"ok": True, **version_info(), "local_connected": r.local_status()["connected"],
            "snapshot_age_s": s["snapshot_age_s"], "busy": bool(s.get("run") or (s.get("plan") or {}).get("active"))}


# -- sign-in (app/auth.py) -------------------------------------------------------------------------------------------
def _auth(request: Request) -> "auth_mod.Auth":
    return request.app.state.auth


def _secure(request: Request, panel: str | None = None) -> bool:
    """Should a cookie be Secure? `panel`: the OIDC flow's PANEL_URL. Sign-in with OIDC always ends on that address,
    so an https one is proof enough even behind a proxy that doesn't say X-Forwarded-Proto."""
    return (request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            or bool(panel and panel.startswith("https://")))


def _set_session(resp, request: Request, token: str, panel: str | None = None):
    resp.set_cookie(auth_mod.COOKIE, token, max_age=auth_mod.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=_secure(request, panel), path="/")
    return resp


def _with_session(body: dict, request: Request, token: str) -> JSONResponse:
    return _set_session(JSONResponse(body), request, token)


def _password_on() -> None:
    if not auth_mod.password_enabled():
        raise HTTPException(403, "password sign-in is off on this panel")


def _addr(request: Request) -> str:
    return request.client.host if request.client else "?"


@app.get("/api/auth/status")
async def auth_status(request: Request):
    a = _auth(request)
    o = getattr(request.app.state, "oidc", None)
    return {"disabled": auth_mod.disabled(), "password_set": a.has_password(),
            "signed_in": auth_mod.disabled() or a.session_ok(request.cookies.get(auth_mod.COOKIE)),
            "min_password": auth_mod.MIN_PASSWORD, "password": auth_mod.password_enabled(),
            "oidc": {"name": o.settings.name, "auto_login": o.settings.auto_login} if o else None}


@app.post("/api/auth/setup")
async def auth_setup(request: Request, body: dict = Body(...)):
    """First run: the setup code from the panel's log and a new password."""
    _password_on()
    a, addr = _auth(request), _addr(request)
    if (wait := a.locked_for(addr)) > 0:
        raise HTTPException(429, f"too many tries; wait {int(wait) + 1} s")
    try:
        token = a.setup(body.get("code"), body.get("password"))
    except PermissionError as exc:
        a.failed(addr)
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    a.succeeded(addr)
    return _with_session({"ok": True}, request, token)


@app.post("/api/auth/login")
async def auth_login(request: Request, body: dict = Body(...)):
    _password_on()
    a, addr = _auth(request), _addr(request)
    if (wait := a.locked_for(addr)) > 0:
        raise HTTPException(429, f"too many tries; wait {int(wait) + 1} s")
    if not a.check_password(body.get("password")):
        a.failed(addr)
        await asyncio.sleep(0.5)
        raise HTTPException(403, "wrong password")
    a.succeeded(addr)
    return _with_session({"ok": True}, request, a.new_session(request.headers.get("user-agent", "")))


def _back_to_sign_in(code: str, spent: bool = False) -> RedirectResponse:
    """To the sign-in page with a code from oidc.CODES only (web/login.js shows a fixed sentence for each). The flow
    cookie is cleared only when it was this sign-in's own: a stray callback must not cancel one in progress."""
    resp = RedirectResponse("/login.html?" + urlencode({"error": code}), status_code=303)
    if spent:
        resp.delete_cookie(oidc_mod.COOKIE, path=oidc_mod.COOKIE_PATH)
    return resp


_REFUSED_EVERY_S = 60                 # at most one "not allowed" event a minute: any account at the provider can try


@app.get("/api/auth/oidc/start")
async def auth_oidc_start(request: Request, next: str = "/", moved: str = ""):
    """Off to the OIDC provider (app/oidc.py), with the flow's state, nonce and PKCE verifier in a signed cookie."""
    o = getattr(request.app.state, "oidc", None)
    if o is None:
        raise HTTPException(404, "OIDC sign-in is not set up on this panel")
    # The provider sends people back to PANEL_URL, so the flow cookie has to be set there. Opened at another address
    # (an IP, another name), start again at PANEL_URL. Once only: a proxy that rewrites Host must not loop.
    here = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    there = urlsplit(o.settings.panel).netloc
    # ...and over https when PANEL_URL is https: the flow cookie is then Secure, and a browser drops a Secure cookie
    # set on a plain-http answer, so the sign-in would come back "expired".
    wrong_scheme = o.settings.panel.startswith("https://") and not _secure(request)
    if not moved and (here.lower() != there.lower() or wrong_scheme):
        query = urlencode({"next": oidc_mod.safe_next(next), "moved": "1"})
        return RedirectResponse(f"{o.settings.panel}{oidc_mod.START}?{query}", status_code=303)
    try:
        url, flow = await o.begin(next)
    except oidc_mod.OIDCError as exc:
        log.warning("OIDC sign-in could not start: %s", exc)
        return _back_to_sign_in(exc.code)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(oidc_mod.COOKIE, flow, max_age=oidc_mod.FLOW_S, httponly=True, samesite="lax",
                    secure=_secure(request, o.settings.panel), path=oidc_mod.COOKIE_PATH)
    return resp


@app.get("/api/auth/oidc/callback")
async def auth_oidc_callback(request: Request):
    """Back from the provider: check everything it sent, then the same session the password gives."""
    o = getattr(request.app.state, "oidc", None)
    if o is None:
        raise HTTPException(404, "OIDC sign-in is not set up on this panel")
    a = _auth(request)
    try:
        who = await o.finish(dict(request.query_params), request.cookies.get(oidc_mod.COOKIE))
    except oidc_mod.OIDCError as exc:
        # A callback that isn't this browser's own sign-in (no flow, wrong state) is anyone's to send: log it quietly.
        log.log(logging.WARNING if exc.spent else logging.INFO, "OIDC sign-in refused: %s", exc)
        now = time.time()
        if isinstance(exc, oidc_mod.NotAllowed) and now - getattr(o, "refused_event_at", 0.0) > _REFUSED_EVERY_S:
            o.refused_event_at = now     # the name is the account's own choice, so it is labelled as such
            a.store.event("auth", f"an account at {o.settings.name} that is not allowed tried to sign in "
                                  f"(subject {exc.sub}; it calls itself {exc.user!r})")
        return _back_to_sign_in(exc.code, exc.spent)
    token = a.new_session(request.headers.get("user-agent", ""), user=who["user"], sub=who["sub"])
    a.store.event("auth", f"{who['user']} signed in with {o.settings.name} (subject {who['sub']})")
    resp = _set_session(RedirectResponse(who["next"], status_code=303), request, token, o.settings.panel)
    resp.delete_cookie(oidc_mod.COOKIE, path=oidc_mod.COOKIE_PATH)
    return resp


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    _auth(request).end_session(request.cookies.get(auth_mod.COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth_mod.COOKIE, path="/")
    return resp


def _person(request: Request) -> None:
    """Password and token changes need a signed-in person: an API token cannot mint tokens or change the password."""
    if auth_mod.disabled():
        return
    if not _auth(request).session_ok(request.cookies.get(auth_mod.COOKIE)):
        raise HTTPException(403, "sign in to change this")


@app.post("/api/auth/password")
async def auth_password(request: Request, body: dict = Body(...)):
    """Change the password (needs the current one). Every other session is signed out."""
    _person(request)
    _password_on()
    a, addr = _auth(request), _addr(request)
    if (wait := a.locked_for(addr)) > 0:
        raise HTTPException(429, f"too many tries; wait {int(wait) + 1} s")
    if not a.check_password(body.get("current")):
        a.failed(addr)
        raise HTTPException(403, "the current password is wrong")
    a.succeeded(addr)
    try:
        a.set_password(body.get("password") or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    a.end_all_sessions()
    a.store.event("auth", "the admin password was changed; other sessions were signed out")
    return _with_session({"ok": True}, request, a.new_session(request.headers.get("user-agent", "")))


@app.post("/api/auth/signout_all")
async def auth_signout_all(request: Request):
    _person(request)
    _auth(request).end_all_sessions()
    _auth(request).store.event("auth", "every session was signed out")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth_mod.COOKIE, path="/")
    return resp


@app.get("/api/auth/tokens")
async def auth_tokens(request: Request):
    return {"tokens": _auth(request).tokens()}


@app.post("/api/auth/tokens")
async def auth_token_create(request: Request, body: dict = Body(...)):
    """A named API token for a program (Home Assistant, a script). Shown once; only its hash is kept."""
    _person(request)
    try:
        return _auth(request).new_token(body.get("name") or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/auth/tokens/{token_id}")
async def auth_token_revoke(request: Request, token_id: str):
    _person(request)
    if not _auth(request).revoke_token(token_id):
        raise HTTPException(404, "no such token")
    return {"tokens": _auth(request).tokens()}


def ops(request: Request) -> Ops:
    return request.app.state.ops


def full_status(request: Request) -> dict:
    s = rec(request).status()
    s["ops"] = ops(request).status()
    s["wifi"] = ops(request).network.bars()       # the top bar's Wi-Fi bars: no network name
    s["ui"] = UI_VERSION
    return s


@app.get("/api/status")
async def status(request: Request):
    return full_status(request)


@app.get("/api/ha/summary")
async def ha_summary(request: Request):
    """One small read for a Home Assistant integration (#12): state, battery, positioning, lawn, last run, the next
    schedule window and whether a start would go out now. No position, map or camera data. The start check runs the
    interlocks for the next window's plan (else the robot's first plan) as the caller, so a token gets the strict ones;
    nothing is sent."""
    r, o = rec(request), ops(request)
    upcoming = o.scheduler.upcoming(datetime.now())
    nxt = ha_mod.next_window(upcoming)
    plan_id = nxt["plan_id"] if nxt and nxt["plan_id"] is not None else next(
        (p.get("id") for p in r.plans or []), None)
    try:
        start = await _ha_start_check(request, plan_id)
    except Exception:
        log.exception("start check for the HA summary failed")
        start = None
    last = next((x for x in r.store.runs(5) if x.get("ended")), None)      # the last finished run, not one going now
    return ha_mod.summary(r.status(), o.status(), last, upcoming, r.plans, start, version_info())


HA_START_CHECK_S = 30   # how long a summary's start check is reused: it asks Frigate and presence, and HA polls often


async def _ha_start_check(request: Request, plan_id) -> dict:
    """The start_plan preview for the summary, reused for HA_START_CHECK_S per caller kind (a person and a token get
    different checks). A failed check is not kept."""
    key, now = (_actor(request), plan_id), time.monotonic()
    cache = getattr(request.app.state, "ha_start_cache", None) or {}
    hit = cache.get(key)
    if hit and now - hit[0] < HA_START_CHECK_S:
        return hit[1]
    start = {**await ops(request).controller.preview("start_plan", {"plan_id": plan_id}, actor=_actor(request)),
             "plan_id": plan_id}
    request.app.state.ha_start_cache = {**{k: v for k, v in cache.items() if now - v[0] < HA_START_CHECK_S},
                                        key: (now, start)}
    return start


@app.get("/api/stream")
async def stream(request: Request):
    r = rec(request)

    async def events():
        # The full status every 1.5 s; between them, each new position sample as a small pose event (~1 a second
        # while the phone app is open), already in the robot's pose order, so the 3D robot and Follow use every one.
        seq, next_status = r.pose_seq, 0.0
        while not await request.is_disconnected():
            now = time.monotonic()
            if now >= next_status:
                next_status = now + 1.5
                yield f"event: status\ndata: {json.dumps(full_status(request))}\n\n"
            poses = r.poses_since(seq)
            if poses:
                seq = poses[-1]["seq"]
                yield f"event: pose\ndata: {json.dumps([{k: v for k, v in p.items() if k != 'seq'} for p in poses])}\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/map")
async def map_geojson(request: Request):
    r = rec(request)
    if not r.map_geojson:
        raise HTTPException(503, "map not fetched yet")
    return {"geojson": r.map_geojson, "bounds": geo.bounds(r.map_geojson), "fetched": r.map_fetched or None,
            "versions": r.store.versions("map")}


@app.get("/api/map/raw")
async def map_raw(request: Request):
    r = rec(request)
    if r.map_raw is None:
        raise HTTPException(503, "map not fetched yet")
    return r.map_raw


@app.get("/api/plans")
async def plans(request: Request, fresh: bool = False):
    """The robot's plans; fresh=true reads them from the robot now (otherwise as of the last map refresh)."""
    r = rec(request)
    if fresh:
        try:
            await r.reload_plans()
        except Exception as exc:
            raise HTTPException(503, f"the robot did not answer ({str(exc) or type(exc).__name__})")
    areas = [{"id": a["id"], "name": a.get("name") or f"Area {a['id']}"}
             for a in (r.map_raw or {}).get("areas") or [] if isinstance(a.get("id"), int)]
    return {"plans": r.plans, "areas": areas, "blocked": _pathway_write_block(request)}


async def _save_plan(request: Request, body: dict, pid: int | None):
    why = _pathway_write_block(request)
    if why:
        raise HTTPException(409, why)
    r = rec(request)
    area_ids = [a.get("id") for a in (r.map_raw or {}).get("areas") or []]
    try:
        return await mapwrite.write_plan(ops(request).map_writer, r.reload_plans, area_ids, body, pid,
                                         check=lambda: _pathway_write_block(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except mapwrite.WriteRefused as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/plans/{pid}/preview")
async def plan_preview(request: Request, pid: int):
    """The robot's planned route for each of the plan's areas, in the plan's order, with their saved mow settings and
    time estimates (the area preview, ~10 s per area; the robot only computes). Driving between areas is not in it:
    the app's own plan preview (preview_plan_path {id, percent}) is captured but its reply not studied yet."""
    r = rec(request)
    try:
        plans = await r.reload_plans()
    except Exception:
        plans = r.plans
    plan = next((p for p in plans or [] if p.get("id") == pid), None)
    if plan is None:
        raise HTTPException(404, "no such plan on the robot")
    try:
        records = await r.read_area_records()
    except Exception as exc:
        raise HTTPException(503, f"could not read the areas from the robot ({str(exc) or type(exc).__name__})")
    areas, features, missing = [], [], []
    for n, aid in enumerate(plan.get("areaIds") or []):
        record = records.get(aid)
        if record is None:
            missing.append({"id": aid, "why": "not on the robot"})
            continue
        try:
            data = await r.preview_area_path(areasettings.preview_payload(record))
        except Exception as exc:
            missing.append({"id": aid, "name": record.get("name"), "why": f"no answer ({str(exc) or type(exc).__name__})"})
            continue
        if not data or not data.get("path"):
            missing.append({"id": aid, "name": record.get("name"), "why": "no route"})
            continue
        s = areasettings.route_summary(data, record["ref"])
        s["route"]["properties"] = {"area_id": aid, "name": record.get("name"), "order": n}
        features.append(s["route"])
        areas.append({"id": aid, "name": record.get("name"), "length_m": s["length_m"], "total_time_s": s["total_time_s"]})
    if not features:
        raise HTTPException(502, "the robot returned no route for this plan's areas")
    times = [a["total_time_s"] for a in areas]
    return {"id": pid, "name": plan.get("name"), "areas": areas, "missing": missing,
            "route": {"type": "FeatureCollection", "features": features},
            "length_m": round(sum(a["length_m"] for a in areas), 1),
            "total_time_s": round(sum(times), 1) if all(t is not None for t in times) else None}


@app.post("/api/plans")
async def create_plan(request: Request, body: dict = Body(...)):
    """A new plan (save_plan, captured 2026-10-10): {name, areaIds, enable_self_order?}."""
    return await _save_plan(request, body, None)


@app.put("/api/plans/{pid}")
async def update_plan(request: Request, pid: int, body: dict = Body(...)):
    """Rename a plan or change its areas in place: same id, so schedule windows keep pointing at it."""
    return await _save_plan(request, body, pid)


@app.delete("/api/plans/{pid}")
async def remove_plan(request: Request, pid: int, force: bool = False):
    """Delete a plan (del_plan, captured 2026-10-10). Refused while a schedule window starts it, unless force=true."""
    why = _pathway_write_block(request)
    if why:
        raise HTTPException(409, why)
    used = [w.get("name") or f"{w.get('start')}–{w.get('end')}" for w in ops(request).scheduler.windows()
            if w.get("plan_id") == pid]
    if used and not force:
        raise HTTPException(409, f"schedule window{'s' if len(used) > 1 else ''} {', '.join(used)} start{'' if len(used) > 1 else 's'} "
                                 "this plan; change them first or delete anyway")
    try:
        return await mapwrite.delete_plan(ops(request).map_writer, rec(request).reload_plans, pid,
                                          check=lambda: _pathway_write_block(request))
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except mapwrite.WriteRefused as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/cfd")
async def cfd(request: Request):
    return rec(request).cfd or {}


@app.get("/api/snapshot")
async def snapshot(request: Request):
    r = rec(request)
    return {"age_s": round(time.time() - r.snapshot_at, 1) if r.snapshot_at else None, "data": r.snapshot_copy()}


@app.get("/api/runs")
async def runs(request: Request, limit: int = 100):
    rows = rec(request).store.runs(limit)
    for r in rows:
        r["end_why"] = why_mod.from_end(r.get("end_reason")) if r.get("ended") else None
    return {"runs": rows, "open_run": rec(request).run_id}


def _segments(points: list[dict]) -> dict:
    """Split a track into LineStrings of constant RTK status, so the map can colour by fix quality."""
    features, current, status = [], [], None
    for p in points:
        coord = [p["lon"], p["lat"]]
        s = p.get("rtk_status")
        if current and s != status:
            current.append(coord)
            if len(current) >= 2:
                features.append({"type": "Feature", "properties": {"rtk": status},
                                 "geometry": {"type": "LineString", "coordinates": current}})
            current = []
        current.append(coord)
        status = s
    if len(current) >= 2:
        features.append({"type": "Feature", "properties": {"rtk": status},
                         "geometry": {"type": "LineString", "coordinates": current}})
    return {"type": "FeatureCollection", "features": features}


@app.get("/api/runs/{run_id}")
async def run_detail(request: Request, run_id: int):
    store = rec(request).store
    run = store.run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    points = store.track(run_id=run_id)
    prev = store.previous_run(run_id)
    joinable = None
    if (prev and prev.get("ended") and run.get("ended") and rec(request).run_id not in (run_id, prev["id"])
            and prev.get("head_type") == run.get("head_type")):
        joinable = {"id": prev["id"], "gap_s": round(run["started"] - prev["ended"]),
                    "same_plan": bool(run.get("plan_session")) and run.get("plan_session") == prev.get("plan_session")}
    return {"run": run, "track": _segments(points), "points": len(points), "join_previous": joinable,
            "end_why": why_mod.from_end(run.get("end_reason")) if run.get("ended") else None}


@app.post("/api/runs/{run_id}/join-previous")
async def join_previous(request: Request, run_id: int):
    """One plan session recorded as two runs (e.g. a resume before plan tracking existed): fold this run into the
    run before it. Both must be finished."""
    r = rec(request)
    run, prev = r.store.run(run_id), r.store.previous_run(run_id)
    if not run or not prev:
        raise HTTPException(404, "no such run, or no run before it")
    if not run.get("ended") or not prev.get("ended") or r.run_id in (run_id, prev["id"]):
        raise HTTPException(409, "a run that is still recording cannot be joined")
    same_plan = bool(run.get("plan_session")) and run.get("plan_session") == prev.get("plan_session")
    if run.get("head_type") != prev.get("head_type"):
        raise HTTPException(409, "the two runs used different heads")
    if not same_plan and run["started"] - prev["ended"] > 3 * 3600:
        raise HTTPException(409, "more than 3 hours apart and not the same plan")
    merged = r.store.merge_runs(prev["id"], run_id)
    r.store.event("run_joined", f"run {run_id} joined onto run {prev['id']} (by hand)")
    await r.analyze_run(prev["id"], force=True)
    return {"run": merged}


@app.get("/api/track/points")
async def track_points(request: Request, since: float = 0, hours: float = 24):
    """Live map track: rows after `since` (the client keeps what it has and asks only for the new ones), never
    older than `hours`."""
    r = rec(request)
    since = max(since, time.time() - hours * 3600)
    rows = r.store.track_rows(since)
    context = r.store.track_rows_before(since, EDGE_CONTEXT) if rows else []
    labels = await asyncio.to_thread(r.edge_labels, [*context, *rows], lambda row: (row[4], row[5]),
                                     lambda row: row[6], r.run_id is not None) if rows else []
    edges = labels[len(context):]
    return {"fields": ["ts", "lon", "lat", "rtk_status", "edge"],
            "rows": [[*row[:4], e] for row, e in zip(rows, edges)],
            # Labels near the end of the last batch were provisional (they look a few samples ahead): these replace them.
            "relabel": [[row[0], e] for row, e in zip(context, labels[:len(context)])][-EDGE_RELABEL:]}


@app.get("/api/signal")
async def signal(request: Request, metric: str = "halow", days: float = 30, network: str | None = None):
    """A signal per grid cell across the runs of the last `days`. metric=wifi is one network from the Wi-Fi survey
    (`network`, else the first of `networks`: a saved one, else the one heard best)."""
    if metric not in coverage.SIGNAL_METRICS:
        raise HTTPException(400, f"metric: one of {', '.join(coverage.SIGNAL_METRICS)}")
    meta = {k: v for k, v in coverage.SIGNAL_METRICS[metric].items() if k in ("field", "label", "unit", "lo", "hi")}
    if metric == "wifi":
        return {"metric": metric, **meta, **await rec(request).wifi_cells(days, network)}
    return {"metric": metric, **meta, "cells": await rec(request).signal_cells(metric, days)}


@app.get("/api/problems")
async def problem_areas(request: Request, days: float = 60, run_id: int | None = None):
    """Where the robot has real trouble, by cause (app/problems.py): cells, ranked spots and pinned events."""
    return await rec(request).problem_map(days, run_id)


@app.get("/api/insights")
async def insights_view(request: Request, days: float = 365):
    """One row per mow (coverage, pace, energy, problems), totals, cadence and problems that keep coming back."""
    r = rec(request)
    now = time.time()
    since = now - days * 86400
    runs = [x for x in r.store.runs(1000) if x.get("ended") and x["started"] >= since
            and (x.get("distance_m") or 0) >= insights.MIN_RUN_M]
    rows = []
    for run in sorted(runs, key=lambda x: x["started"]):
        analysis = await r.analyze_run(run["id"])           # the cached one, unless it is of an older version
        prob = await r.problem_map(days, run["id"])
        rows.append(insights.mow_row(run, analysis, prob))
    everything = await r.problem_map(days, run_ids={m["id"] for m in rows})
    return {"days": days, "mows": rows, "totals": insights.totals(rows, now),
            "recurring": insights.recurring(everything["cells"]), "min_run_m": insights.MIN_RUN_M}


@app.get("/api/series")
async def series(request: Request, metric: str, hours: float = 24):
    try:
        return {"metric": metric, "points": rec(request).store.series(metric, time.time() - hours * 3600)}
    except ValueError:
        raise HTTPException(400, f"unknown metric {metric!r}")


@app.get("/api/events")
async def events(request: Request, limit: int = 200):
    return {"events": rec(request).store.events(limit)}


@app.get("/api/replies")
async def replies(request: Request):
    """The latest reply to every read request seen on the broker, including other clients' requests."""
    return dict(sorted(rec(request).replies.items()))


@app.get("/api/commands")
async def commands(request: Request):
    """Commands other clients have published on the local broker (we only ever listen to these)."""
    return {"commands": rec(request).store.commands_seen()}


# -- phase 1b: runs analysis --------------------------------------------------------------------------------------
POINT_FIELDS = ["ts", "lon", "lat", "x", "y", "rtk_status", "sats", "halow", "battery", "blade_cur", "pitch", "roll",
                "speed", "slip", "edge", "heading"]
EDGE_CONTEXT = 40   # rows before an incremental batch, so an edge lap is judged whole
EDGE_RELABEL = 20   # ...of which the last ones are sent again with their final labels


@app.get("/api/runs/{run_id}/points")
async def run_points(request: Request, run_id: int, since: float = 0):
    """Compact per-sample rows for map layers and replay. slip is experimental (wheel vs pose distance)."""
    r = rec(request)
    store = r.store
    run = store.run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    rows, prev = [], (store.run_point_before(run_id, since) if since else None)   # slip needs the previous row
    points = [p for p in store.run_points(run_id, since) if p.get("lon") is not None]
    context = store.run_points_before(run_id, since, EDGE_CONTEXT) if since and points else []
    labels = await asyncio.to_thread(r.edge_labels, [*context, *points], lambda p: (p.get("x"), p.get("y")),
                                     lambda p: p.get("planning"), not run.get("ended")) if points else []
    edges = labels[len(context):]
    for p, edge in zip(points, edges):
        blade = [v for v in (p.get("blade_cur_l"), p.get("blade_cur_r")) if v is not None]
        speeds = [abs(v) for v in (p.get("speed_l"), p.get("speed_r")) if v is not None]
        slip = None
        if prev and None not in (p.get("odo_l"), p.get("odo_r"), prev.get("odo_l"), prev.get("odo_r"),
                                 p.get("x"), prev.get("x")):
            wheel = ((p["odo_l"] - prev["odo_l"]) + (p["odo_r"] - prev["odo_r"])) / 2
            pose = ((p["x"] - prev["x"]) ** 2 + (p["y"] - prev["y"]) ** 2) ** 0.5
            if wheel > 0.05:
                slip = round(max(0.0, min(1.0, 1 - pose / wheel)), 3)
        rows.append([p["ts"], p["lon"], p["lat"], p.get("x"), p.get("y"), p.get("rtk_status"), p.get("sats"),
                     p.get("halow"), p.get("battery"), max(blade) if blade else None, p.get("pitch"), p.get("roll"),
                     sum(speeds) / len(speeds) if speeds else None, slip, edge, telemetry.heading_deg(p.get("phi"))])
        prev = p
    return {"fields": POINT_FIELDS, "rows": rows,
            "relabel": [[p["ts"], e] for p, e in zip(context, labels[:len(context)])][-EDGE_RELABEL:]}


# -- pathway mowing settings ------------------------------------------------------------------------------------
def _robot_pathway(r, pid: int) -> dict:
    p = next((f for f in (r.map_raw or {}).get("pathways") or [] if f.get("id") == pid), None)
    if p is None:
        raise HTTPException(404, "no such pathway on the robot (a pathway that only exists in the draft has no settings yet)")
    return p


def _pathway_write_block(request: Request) -> str | None:
    r, o = rec(request), ops(request)
    if not r.store.setting("map_write_enabled", False):
        return "map writing is switched off (Settings → Safety)"
    age = time.time() - r.snapshot_at if r.snapshot_at else None
    if age is None or age > 120:
        return "no recent data from the robot"
    active = r.plan_active()
    if r.run_id or active:
        return "a plan is running; change settings while the robot is idle"
    if active is None:
        return "the robot's plan state is unknown"
    if o.cloud.state != "connected":
        return f"the Yarbo cloud is {o.cloud.state}"
    return o.controller.start_settling()


def _robot_area(r, aid: int) -> dict:
    a = next((f for f in (r.map_raw or {}).get("areas") or [] if f.get("id") == aid), None)
    if a is None:
        raise HTTPException(404, "no such area on the robot")
    return a


@app.get("/api/areas/{aid}/settings")
async def area_settings(request: Request, aid: int):
    """An area's mowing settings as the robot has them, the fields the editor knows, and why saving is off (if it is)."""
    r = rec(request)
    a = _robot_area(r, aid)
    data, err = await areasettings.read_settings(r.read_area_params, aid)
    if data is None:
        raise HTTPException(503, f"the robot did not return this area's settings ({err})")
    labels = a.get("labels")
    if labels is None:                                   # get_map leaves them out; the area's own record has them
        with contextlib.suppress(Exception):
            labels = ((await r.read_area_records()).get(aid) or {}).get("labels")
    return {"id": aid, "name": a.get("name") or "", "settings": data, "defaults": data.get("id") == 0,
            "labels": [areasettings.AREA_LABELS.get(x, f"label {x}") for x in labels or []],
            "fields": areasettings.AREA_FIELDS,
            "blocked": _pathway_write_block(request) or areasettings.read_problem(data)}


@app.post("/api/areas/{aid}/preview")
async def area_preview(request: Request, aid: int, body: dict = Body(None)):
    """The robot's planned mowing route for this area with its saved mow settings, and its time estimate. The robot
    only computes (the app asks for the same while editing); nothing changes. {source: "draft"} uses the area's outline
    in the Studio draft instead of the one on the robot."""
    r = rec(request)
    _robot_area(r, aid)
    try:
        record = (await r.read_area_records()).get(aid)
    except Exception as exc:
        raise HTTPException(503, f"could not read the area from the robot ({str(exc) or type(exc).__name__})")
    if record is None:
        raise HTTPException(404, "the robot has no record of this area")
    outline, source = None, "robot"
    if (body or {}).get("source") == "draft":
        d = r.store.setting("map_draft")
        a = mapstudio.find(d["map"], "areas", aid) if d else None
        if a is None:
            raise HTTPException(409, "the draft has no such area")
        if not mapwrite._same_ref(a.get("ref"), record.get("ref")):
            raise HTTPException(409, "the draft's area is in another frame than the robot's; reload the draft")
        outline, source = a.get("range"), "draft"
    try:
        data = await r.preview_area_path(areasettings.preview_payload(record, outline))
    except Exception as exc:
        raise HTTPException(504, f"the robot did not answer ({str(exc) or type(exc).__name__})")
    if not data or not data.get("path"):
        raise HTTPException(502, "the robot returned no route for this outline")
    return {"id": aid, "source": source, **areasettings.route_summary(data, record["ref"])}


@app.put("/api/areas/{aid}/settings")
async def put_area_settings(request: Request, aid: int, body: dict = Body(...)):
    """{changes: {"edge_circles": 3, "first_clean_params.plan_speed": 0.5}} (stored units: metres, m/s, degrees)."""
    r = rec(request)
    _robot_area(r, aid)
    why = _pathway_write_block(request)
    if why:
        raise HTTPException(409, why)
    changes = body.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(400, "send {changes: {field: value}}")
    try:
        return await areasettings.write(ops(request).map_writer, aid, changes, r.read_area_params, _gps_ref(request),
                                        check=lambda: _pathway_write_block(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except mapwrite.WriteRefused as exc:
        raise HTTPException(409, str(exc))


# -- the 3D view's ground (app/elevation.py) -----------------------------------------------------------------------
async def _terrain(request: Request, source: str = "lidar") -> dict:
    """One terrain source ready for the ground: the lidar (fetched once, kept) or the robot's heights."""
    r = rec(request)
    if not r.map_geojson:
        raise HTTPException(503, "the robot's map has not been read yet")
    el = request.app.state.elevation
    bounds = geo.bounds(r.map_geojson)
    try:
        if source == "robot":
            pts, runs = r.store.height_points()
            area = sum(row.get("area_m2") or 0 for row in mapstudio.summary(r.map_raw or {}, r.gps_ref) if row["kind"] == "areas") \
                if r.gps_ref else 0
            rings = [f["geometry"]["coordinates"][0] for f in r.map_geojson.get("features") or []
                     if f.get("properties", {}).get("kind") == "areas" and f.get("geometry", {}).get("type") == "Polygon"]
            return await el.ensure_robot(pts, bounds, area or 1500.0, runs, rings)
        return await el.ensure(bounds)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"elevation unavailable ({str(exc) or type(exc).__name__})")


async def _ground(request: Request) -> dict:
    """The 3D ground from whichever terrain sources are ready: the robot's heights (404 until a mow has logged
    enough) and the lidar (502 offline or outside USGS coverage). Only both failing is an error."""
    ready, why = {}, []
    for source in ("lidar", "robot"):
        try:
            await _terrain(request, source=source)
            ready[source] = True
        except HTTPException as exc:
            if exc.status_code == 503:                       # the robot's map is not read yet: nothing to place
                raise
            why.append(f"{source}: {exc.detail}")
    if not ready:
        raise HTTPException(404, "no terrain for the 3D view yet (" + "; ".join(why) + ")")
    return await request.app.state.elevation.ground(ready.get("lidar", False), ready.get("robot", False))


@app.get("/api/elevation/ground")
async def elevation_ground(request: Request):
    """The map's 3D view: the robot's heights over the lawns and the lidar around them as one ground. Its version (for
    the tile URLs), the bounds the tiles cover, the zooms they exist at, the relief and which sources went in."""
    return await _ground(request)


@app.get("/api/elevation/ground/{z}/{x}/{y}.png")
async def elevation_ground_tile(request: Request, z: int, x: int, y: int, v: str | None = None):
    """One Terrarium-encoded tile of the 3D ground (MapLibre raster-dem). `v` is the ground's version the page read;
    if the ground has changed since, the tile is of the new one and the browser is told not to keep it under the old
    address (the page asks for the new ground when a run ends)."""
    if not (0 <= z <= 24 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(400, "no such tile")
    el = request.app.state.elevation
    if el.g["z"] is None:                                    # a restart since the page read /api/elevation/ground
        await _ground(request)
    keep = el.is_current(v)
    data = await el.ground_tile(z, x, y)
    return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=86400" if keep else "no-store"})


@app.get("/api/pathways/{pid}/settings")
async def pathway_settings(request: Request, pid: int):
    r = rec(request)
    p = _robot_pathway(r, pid)
    try:
        settings = await r.read_pathway_params(pid)
    except Exception as exc:
        raise HTTPException(503, f"the robot did not answer ({str(exc) or type(exc).__name__})")
    if settings is None:
        raise HTTPException(503, "the robot did not return this pathway's settings")
    locks = {}
    obs = mapwrite.obstacle_lock(settings)
    if obs:
        locks["pathway_obs_perception_sw"] = obs
    return {"id": pid, "name": p.get("name") or "", "settings": settings, "fields": mapwrite.PATHWAY_FIELDS,
            "locks": locks, "blocked": _pathway_write_block(request) or mapwrite.read_problem(settings)}


def _robot_deadend(r, did: int) -> dict:
    d = next((f for f in (r.map_raw or {}).get("deadends") or [] if f.get("id") == did), None)
    if d is None:
        raise HTTPException(404, "no such dead end on the robot (one that only exists in the draft has no settings yet)")
    return d


@app.get("/api/deadends/{did}/settings")
async def deadend_settings(request: Request, did: int):
    r = rec(request)
    d = _robot_deadend(r, did)
    try:
        settings = await r.read_deadend_params(did)
    except Exception as exc:
        raise HTTPException(503, f"the robot did not answer ({str(exc) or type(exc).__name__})")
    if settings is None:
        raise HTTPException(503, "the robot did not return this dead end's settings")
    missing = [k for k in mapwrite.DEADEND_READ_KEYS if k not in settings]
    return {"id": did, "name": d.get("name") or "", "settings": settings, "fields": mapwrite.DEADEND_FIELDS, "locks": {},
            "defaults": settings.get("id") == 0,
            "blocked": _pathway_write_block(request) or (f"the robot's settings lack {', '.join(missing)}" if missing else None)}


@app.put("/api/deadends/{did}/settings")
async def put_deadend_settings(request: Request, did: int, body: dict = Body(...)):
    _robot_deadend(rec(request), did)
    why = _pathway_write_block(request)
    if why:
        raise HTTPException(409, why)
    changes = body.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(400, "send {changes: {field: value}}")
    try:
        return await mapwrite.write_deadend_params(ops(request).map_writer, did, changes,
                                                   check=lambda: _pathway_write_block(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except mapwrite.WriteRefused as exc:
        raise HTTPException(409, str(exc))


@app.put("/api/pathways/{pid}/settings")
async def put_pathway_settings(request: Request, pid: int, body: dict = Body(...)):
    _robot_pathway(rec(request), pid)
    why = _pathway_write_block(request)
    if why:
        raise HTTPException(409, why)
    changes = body.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(400, "send {changes: {field: value}}")
    try:
        return await mapwrite.write_pathway_params(ops(request).map_writer, pid, changes,
                                                   check=lambda: _pathway_write_block(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except mapwrite.WriteRefused as exc:
        raise HTTPException(409, str(exc))


def _shape(v, depth: int = 0):
    """What a value looks like, without its contents: type, length, the keys of a dict or of a list's first item."""
    if isinstance(v, dict):
        return {k: _shape(x, depth + 1) for k, x in v.items()} if depth < 3 else f"dict({len(v)})"
    if isinstance(v, list):
        first = v[0] if v else None
        return {"list": len(v), **({"of": _shape(first, depth + 1)} if isinstance(first, (dict, list)) else
                                   {"item": type(first).__name__} if v else {})}
    return type(v).__name__


@app.get("/api/runs/{run_id}/plan-feedback")
async def run_plan_feedback(request: Request, run_id: int, raw: bool = False):
    """The robot's get_plan_feedback reply kept for this run (the latest kept: its first, a later one with paths or
    of another plan session, or a joined piece's). Its shape by
    default; raw=true for the whole reply. Read only; for working out whether the paths are the robot's own plan
    (an open question: planned vs driven). Not on the assistant's route list."""
    got = rec(request).store.latest_version(f"plan_feedback:{run_id}")
    if not got:
        raise HTTPException(404, "no plan feedback was kept for this run")
    out = {"run_id": run_id, "at": got.get("ts")}
    return {**out, "data": got.get("data")} if raw else {**out, "shape": _shape(got.get("data"))}


@app.get("/api/runs/{run_id}/planned")
async def run_planned_get(request: Request, run_id: int):
    """The last planned-vs-driven comparison worked out for this run (POST makes one)."""
    got = rec(request).store.latest_version(f"planned:{run_id}")
    if not got:
        raise HTTPException(404, "not compared yet")
    return got


@app.post("/api/runs/{run_id}/planned")
async def run_planned(request: Request, run_id: int):
    """Compare the track a run drove with the robot's plan: its own plan where it was kept while the run went (every
    pass of each area, from where this run took it up: planned_mod.robot_plan), else the robot plans the area again
    (its route preview: it only computes; today's outlines and settings, `changed_since` says whether they changed
    after the run; ~10 s per area)."""
    r = rec(request)
    run = r.store.run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    fb = r.store.latest_version(f"plan_feedback:{run_id}")
    kept = planned_mod.robot_plan(r.store, run)
    area_ids = kept["area_ids"] or [a for a in ((fb or {}).get("data") or {}).get("areaIds") or [] if isinstance(a, int)]
    if not area_ids:                                             # older runs: the areas it actually mowed
        cov = await r.analyze_run(run_id)
        area_ids = [a["id"] for a in (cov or {}).get("areas") or [] if (a.get("covered_m2") or 0) >= 20]
    if not area_ids:
        raise HTTPException(409, "this run mowed no area to compare with")
    # The robot's own plan for an area, kept while the run went (get_plan_feedback): every pass (edge and fill), each
    # from where the robot stood on it when this run began, passes it had already finished left out. Used when it can
    # be placed on the area; otherwise the robot plans the area again now (its route preview).
    on_map = {a.get("id"): a for a in (r.map_raw or {}).get("areas") or []}
    own: dict = {}
    for part in kept["parts"]:
        aid = part["area_id"]
        if aid not in area_ids or aid not in on_map:
            continue
        coords = planned_mod.place_robot_path(part["path"], on_map[aid], r.gps_ref)
        if not coords:
            continue
        pts = [geo.to_local(r.gps_ref, *c) for c in coords] if r.gps_ref else []
        steps = [geo.distance_m(*a, *b) for a, b in zip(pts, pts[1:])]
        finished = (aid, part["type"]) in kept["done"]
        start = len(coords) if finished else max(0, part["start_index"] - 1)    # the leg it was on counts
        own.setdefault(aid, []).append({
            "type": part["type"], "kind": planned_mod.PASS_KINDS.get(part["type"], "pass"), "source": part["source"],
            "coords": coords[start:], "length_m": round(sum(steps[start:]), 1),
            "done_before_m": round(sum(steps[:start]), 1)})
    records = {}
    if any(a not in own for a in area_ids):
        try:
            records = await r.read_area_records()
        except Exception as exc:
            raise HTTPException(503, f"could not read the areas from the robot ({str(exc) or type(exc).__name__})")
    areas, routes, missing = [], [], []
    for aid in area_ids:
        if aid in own:
            passes = own[aid]
            areas.append({"id": aid, "name": on_map[aid].get("name"),
                          "length_m": round(sum(q["length_m"] for q in passes), 1),
                          "done_before_m": round(sum(q["done_before_m"] for q in passes), 1), "total_time_s": None,
                          "source": passes[0]["source"] if len({q["source"] for q in passes}) == 1 else
                          "the robot's own plan, partly kept in an earlier run of the plan",
                          "passes": [{k: v for k, v in q.items() if k != "coords"} for q in passes]})
            routes.extend((aid, q["type"], q["coords"]) for q in passes if len(q["coords"]) >= 2)
            continue
        rec_ = records.get(aid)
        if rec_ is None:
            missing.append(aid)
            continue
        try:
            data = await r.preview_area_path(areasettings.preview_payload(rec_))
        except Exception as exc:
            raise HTTPException(504, f"the robot did not plan area {aid} ({str(exc) or type(exc).__name__})")
        if not data or not data.get("path"):
            missing.append(aid)
            continue
        summ = areasettings.route_summary(data, rec_["ref"])
        areas.append({"id": aid, "name": rec_.get("name"), "length_m": summ["length_m"], "total_time_s": summ["total_time_s"],
                      "source": "planned again today"})
        routes.append((aid, None, summ["route"]["geometry"]["coordinates"]))
    if not routes:
        raise HTTPException(409, "the robot planned none of this run's areas")
    track = r.store.run_points(run_id)
    result = await asyncio.to_thread(planned_mod.compare, [c for _, _, c in routes], track)
    end = run.get("ended") or time.time()
    changed = [k for k in ["map", *(f"area_params:{a}" for a in area_ids if a not in own)]
               if any(v["fetched"] > end + 60 for v in r.store.versions_like(k))]
    out = {"run": run_id, "at": time.time(), "areas": areas, "missing": missing,
           "route": {"type": "FeatureCollection", "features": [
               {"type": "Feature", "properties": {"id": aid, "type": ptype},
                "geometry": {"type": "LineString", "coordinates": c}} for aid, ptype, c in routes]},
           "planned_length_m": round(sum(a["length_m"] for a in areas), 1),
           "done_before_m": round(sum(a.get("done_before_m") or 0 for a in areas), 1),   # of the plan, by earlier runs
           "planned_time_s": (round(sum(a["total_time_s"] for a in areas), 1)        # none when a part has no time
                              if all(a["total_time_s"] is not None for a in areas) else None),
           "driven_m": run.get("distance_m"), "duration_s": (run["ended"] - run["started"]) if run.get("ended") else None,
           "basis": ("the robot's own plan" if len(own) == len(areas) else
                     "planned today" if not own else "the robot's own plan where it was kept, planned today elsewhere")
                    + ("" if not fb else ", for the plan's areas"),
           "changed_since": [c.replace("area_params:", "settings of area ") for c in changed],
           **result}
    r.store.save_version(f"planned:{run_id}", out)
    return out


@app.get("/api/runs/{run_id}/coverage")
async def run_coverage(request: Request, run_id: int, refresh: bool = False):
    result = await rec(request).analyze_run(run_id, force=refresh)
    if result is None:
        raise HTTPException(404, "run not found, or the map is not loaded yet")
    return result


# -- phase 1b: conditions, weather, health ------------------------------------------------------------------------
@app.get("/api/conditions")
async def conditions(request: Request):
    r = rec(request)
    return r.evaluate_conditions() or {"state": None, "reason": "no weather data yet",
                                       "tempest": r.tempest.status() if r.tempest else None,
                                       "ha_weather": r.ha_weather.status()}


@app.get("/api/setup/status")
async def setup_status(request: Request):
    """Which first-run steps are done (Settings → Set up). Read from the state the panel already keeps; nothing is
    stored for the wizard. `first_run` (no robot address or no map yet) is when the panel opens the wizard itself."""
    r, o, cfg, store = rec(request), ops(request), request.app.state.cfg, rec(request).store
    local = r.local_status()
    cloud = o.cloud.status().get("state")
    g = control.gates(store)
    yard = store.setting("yard_cameras", []) or []
    polys = store.setting("camera_polygons", {}) or {}
    ws = weather_mod.config(store.setting("weather_sources"))
    heard = bool(r.tempest and r.tempest.last_obs_at)
    ha_feeds = [k for k in ("rain", "temp", "humidity") if ws["ha"].get(k)] + (["soil"] if ws["ha"].get("soil") else [])
    ch = o.notifier.channels()
    src = (r.yard or {}).get("source") if isinstance(r.yard, dict) else None

    def step(id_, title, done, detail, optional=False):
        return {"id": id_, "title": title, "done": bool(done), "detail": detail, "optional": optional}

    steps = [
        step("robot", "Robot", local.get("connected") and r.map_geojson,
             "connected, map read" if local.get("connected") and r.map_geojson else "connected, waiting for the map"
             if local.get("connected") else "no address yet" if not cfg.yarbo_host else "not found at its address"),
        step("cloud", "Yarbo cloud", cloud == "connected", f"cloud {cloud or 'signed out'} (optional: a fallback route)", True),
        step("imagery", "Map imagery", src, (src or {}).get("name", "picked") if src else "no imagery yet"),
        step("cameras", "Cameras", o.frigate.enabled and yard and all(c in polys for c in yard),
             "Frigate not set up" if not o.frigate.enabled else f"{len(yard)} yard cameras, {sum(1 for c in yard if c in polys)} with coverage", True),
        step("ha", "Home Assistant", r.ha.enabled and not r.ha.error,
             "not set up" if not r.ha.enabled else r.ha.error or ("connected, presence set" if store.setting("presence_entity") else "connected"), True),
        step("weather", "Weather", heard or ha_feeds,
             ", ".join(x for x in ["Tempest heard" if heard else "", f"HA: {', '.join(ha_feeds)}" if ha_feeds else ""] if x) or "no weather source"),
        step("safety", "Safety", g.get("yard_clear_required") and not g.get("guard_resume"),
             f"control {'ON' if o.controller.enabled() else 'off'} · yard must be clear {'on' if g.get('yard_clear_required') else 'OFF'}"
             f" · guard resume {'ON' if g.get('guard_resume') else 'off'}"),
        step("alerts", "Notifications", ch.get("ha") or ch.get("webhook"),
             " + ".join(x for x in ["HA app" if ch.get("ha") else "", "webhook" if ch.get("webhook") else ""] if x) or "Events only"),
        step("assistant", "AI assistant", mcp_server.token_status(store)["enabled"], "token made" if mcp_server.token_status(store)["enabled"] else "no token", True),
    ]
    need = [s for s in steps if not s["optional"]]
    return {"first_run": not getattr(cfg, "demo", False) and (not cfg.yarbo_host or not r.map_geojson), "steps": steps,
            "done": sum(s["done"] for s in steps), "required_done": all(s["done"] for s in need)}


@app.get("/api/ha/sensors")
async def ha_sensors(request: Request):
    """The HA entities that could feed each Lawn-conditions input, with their values now, so Settings offers a list
    instead of typed ids (and `presence`: the entities that can say whether anyone is home). One read of HA's
    states, from the saved Home Assistant connection only."""
    states = await _ha_read(rec(request).ha, "states")
    return {**weather_mod.candidates(states), "presence": ha_mod.presence_candidates(states)}


@app.get("/api/ha/notify_services")
async def ha_notify_services(request: Request):
    """HA's notify services, phones first, so Settings → Notifications offers a list instead of a typed name."""
    return {"services": ha_mod.notify_candidates(await _ha_read(rec(request).ha, "services"))}


async def _ha_read(ha, what: str) -> list:
    """GET /api/<what> (states or services) from the saved Home Assistant connection; a list, or an HTTP error in words."""
    if not ha.enabled:
        raise HTTPException(409, "Home Assistant is not connected (Settings → Home Assistant)")
    try:
        res = await ha._client.get(f"{ha.url}/api/{what}")
        if res.status_code == 401:
            raise HTTPException(502, "Home Assistant rejected the token")
        res.raise_for_status()
        data = res.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"could not read Home Assistant's {what}: {ha_mod.net_error(exc, ha.url)}")
    return data if isinstance(data, list) else []


@app.get("/api/weather/series")
async def weather_series(request: Request, metric: str, hours: float = 24):
    try:
        return {"metric": metric, "points": rec(request).store.weather_series(metric, time.time() - hours * 3600)}
    except ValueError:
        raise HTTPException(400, f"unknown metric {metric!r}")


def _correction_gaps(store, since: float) -> dict:
    found = corrections.gaps(store.correction_rows(since), time.time())
    return {"correction_gaps": found[::-1][:20], "correction_gap_count": len(found)}   # newest first


@app.get("/api/health")
async def health_view(request: Request, hours: float = 24):
    r = rec(request)
    since = time.time() - hours * 3600
    rtk = r.store.series("rtk_status", since, max_points=100000)
    share = {}
    for _, v in rtk:
        label = telemetry.rtk_label(v)
        share[label] = share.get(label, 0) + 1
    total = sum(share.values()) or 1
    return {
        "hours": hours,
        "rtk_share_pct": {k: round(100 * v / total, 1) for k, v in sorted(share.items(), key=lambda kv: -kv[1])},
        **_correction_gaps(r.store, since),
        "error_codes_seen": r.store.error_codes_seen(),
        "status_dictionary": {k: {"meaning": v[0], "action": v[1]} for k, v in telemetry.STATUS_DICTIONARY.items()},
        "firmware_history": r.store.firmware_history(),
        "tempest": r.tempest.status() if r.tempest else None,
        "ha": r.ha.status(),
    }


# -- settings (this panel only) ----------------------------------------------------------------------------------
SETTINGS_DEFAULTS = {
    "imagery_alignment": {"east_m": 0.0, "north_m": 0.0, "rotate_deg": 0.0, "opacity": 1.0},
    "deck_m": None,
    "control_enabled": False,
    "map_write_enabled": False,
    "mcp_control_enabled": False,
    "yard_cameras": [],
    "camera_polygons": {},
    "presence_entity": None,
    "ha_notify_service": None,
    "webhook_url": None,
    "wifi_survey": "off",
    "robot_video_enabled": False,    # §13: the viewer's browser joins Agora; people only (POST /api/video/token)
    "robot_route": "auto",           # app/link.py: local first with the cloud as a fallback, or one route only
}


@app.get("/api/settings")
async def settings(request: Request):
    store = rec(request).store
    out = {k: store.setting(k, v) for k, v in SETTINGS_DEFAULTS.items()}
    out["conditions"] = rec(request).conditions_config().to_dict()
    out["gates"] = control.gates(store)
    out["integrations"] = request.app.state.cfg.describe()
    out["mcp"] = mcp_server.token_status(store)
    from . import weather as weather_mod
    out["weather_sources"] = weather_mod.config(store.setting("weather_sources"))
    return out


def _from_panel(request: Request) -> None:
    """The token endpoints need a header a cross-site form or image cannot send, so another page cannot rotate or
    revoke the token behind the user's back."""
    if request.headers.get("x-panel-request") != "1":
        raise HTTPException(403, "send X-Panel-Request: 1 (the panel's own pages do)")


@app.post("/api/mcp/token")
async def mcp_token_create(request: Request):
    """A new token for AI assistants (replaces any earlier one). Shown once: only its hash is kept."""
    _from_panel(request)
    return {"token": mcp_server.new_token(rec(request).store), "path": "/mcp"}


@app.delete("/api/mcp/token")
async def mcp_token_revoke(request: Request):
    _from_panel(request)
    store = rec(request).store
    store.set_setting(mcp_server.TOKEN_SETTING, None)
    store.set_setting(mcp_server.SWITCH, False)
    store.event("mcp_token", "the AI assistant token was revoked; MCP is off")
    return mcp_server.token_status(store)


# Settings → This panel: how this instance runs (nothing secret).
@app.get("/api/about")
async def about(request: Request):
    cfg, r = request.app.state.cfg, rec(request)
    return {**version_info(), "data_dir": cfg.data_dir, "port": cfg.port, "demo": cfg.demo,
            "uptime_s": round(time.time() - r.started_at), "stats": r.store.counts()}


# -- help: user docs and the changelog, as markdown --------------------------------------------------------------
@app.get("/api/help")
async def help_index():
    return {**version_info(), "pages": help_mod.pages()}


@app.get("/api/help/{page_id}")
async def help_page(page_id: str):
    p = help_mod.page(page_id)
    if p is None:
        raise HTTPException(404, f"no help page {page_id!r}")
    return p


# -- integrations: the user's values over the environment, applied live -------------------------------------------
@app.put("/api/integrations/{group}")
async def put_integration(request: Request, group: str, value: dict = Body(...)):
    if group not in GROUPS:
        raise HTTPException(404, f"unknown group {group!r}")
    cfg, r, o = request.app.state.cfg, rec(request), ops(request)
    before = cfg.snapshot(group)
    try:
        changed = cfg.set(group, value)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    note = ""
    if changed:
        r.store.event("settings_changed", f"{group}: {', '.join(changed)}")      # names only, never values
        try:
            if group == "robot":
                task = asyncio.ensure_future(r.apply_robot())
                task.add_done_callback(lambda t: log.warning("robot reconnect failed: %s", t.exception())
                                       if not t.cancelled() and t.exception() else None)
                try:
                    await asyncio.wait_for(asyncio.shield(task), 25)
                except asyncio.TimeoutError:
                    note = "still looking for the robot; the Robot section updates when it answers"
            elif group == "frigate":
                await o.apply_frigate()
            elif group == "home_assistant":
                await o.apply_home_assistant()
            elif group == "weather":
                await r.apply_weather()
            elif group == "panel":
                r.apply_panel()
        except Exception as exc:
            cfg.restore(group, before)                   # not kept: it could not be put into effect
            raise HTTPException(502, f"could not apply the change, so it was not kept: {exc}")
    return {"group": group, "changed": changed, "note": note, "fields": cfg.describe()[group]}


_SCAN_LOCK = asyncio.Lock()


@app.post("/api/robot/discover")
async def robot_discover(request: Request, body: dict = Body(None)):
    """Look for the robot on the local network (Settings → Robot → Find it on my network): the panel's own /24, or a
    home-network (RFC 1918) /24 the user names. Connects and subscribes only; nothing is sent to anything it finds.
    One at a time; about 20 s."""
    cfg = request.app.state.cfg
    if cfg.demo:
        return {"network": "demo", "brokers": 1, "found": [{"host": "demo", "port": 1883, "sn": rec(request).sn}], "note": None}
    if _SCAN_LOCK.locked():
        raise HTTPException(409, "a search is already running")
    network = (body or {}).get("network")
    network = None if network in (None, "") else network
    async with _SCAN_LOCK:
        try:
            return await robotscan.scan(int(cfg.yarbo_port or 1883), network)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


@app.post("/api/integrations/{group}/test")
async def test_integration(request: Request, group: str, body: dict = Body(None)):
    """Try an integration with the values given (or the ones in effect) without saving anything. Only ever contacts
    the address the user typed or already set."""
    body, cfg = body or {}, request.app.state.cfg
    try:
        if group == "robot" and cfg.demo:
            return {"ok": True, "detail": f"demo robot {rec(request).sn} (synthetic, no network)", "sn": rec(request).sn}
        if group == "robot":
            host = (body.get("host") or cfg.yarbo_host or "").strip()
            port = int(body.get("port") or cfg.yarbo_port)
            if not host:
                raise HTTPException(400, "no robot address")
            sn = await asyncio.to_thread(discover_sn, host, port, 12)
            return {"ok": True, "detail": f"robot {sn} is broadcasting on {host}:{port}", "sn": sn}
        if group == "frigate":
            url = (body.get("url") or cfg.frigate_url or "").rstrip("/")
            if not url:
                raise HTTPException(400, "no Frigate address")
            async with httpx.AsyncClient(timeout=8) as http:
                res = await http.get(f"{url}/api/config")
                res.raise_for_status()
                cams = sorted((res.json().get("cameras") or {}).keys())
            return {"ok": True, "detail": f"{len(cams)} cameras", "cameras": cams}
        if group == "home_assistant":
            url = (body.get("url") or cfg.ha_url or "").rstrip("/")
            token = body.get("token") or cfg.ha_token
            if not url or not token:
                raise HTTPException(400, "an address and a token are both needed")
            if url != (cfg.ha_url or "").rstrip("/") and not body.get("token"):
                raise HTTPException(400, "paste the token as well to test a different address: the saved one only goes to the saved address")
            async with httpx.AsyncClient(timeout=8) as http:
                res = await http.get(f"{url}/api/", headers={"Authorization": f"Bearer {token}"})
            if res.status_code == 401:
                return {"ok": False, "detail": "Home Assistant rejected the token"}
            res.raise_for_status()
            return {"ok": True, "detail": (res.json() or {}).get("message", "API running")}
        if group == "weather":
            t = rec(request).tempest
            return {"ok": bool(t and t.last_obs_at), "detail": "listener off" if not t
                    else f"{t.packets} packets, last observation {round(time.time() - t.last_obs_at)} s ago" if t.last_obs_at
                    else f"listening on UDP {t.port}, nothing heard yet{': ' + t.error if t.error else ''}"}
    except HTTPException:
        raise
    except Exception as exc:
        return {"ok": False, "detail": ha_mod.net_error(exc, body.get("url") or (cfg.ha_url if group == "home_assistant" else cfg.frigate_url))}
    raise HTTPException(404, f"nothing to test for {group!r}")



# Lawn-condition thresholds: (lowest, highest). The robot's rain-sensor calibration is any number, or null for unset.
CONDITION_LIMITS = {"lookback_h": (1, 48), "tau_h": (0.25, 24), "wet_mm": (0, 50), "damp_mm": (0, 50),
                    "dew_spread_c": (0, 10), "dry_after_sunrise_h": (0, 12), "raining_window_s": (60, 3600),
                    "soil_wet_pct": (1, 100), "soil_damp_pct": (1, 100), "soil_max_age_h": (0.25, 24)}


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _gate_text(v) -> str:
    return "on" if v is True else "off" if v is False else ", ".join(v) if isinstance(v, list) else str(v)


@app.put("/api/settings/{key}")
async def put_setting(request: Request, key: str, value: Any = Body(None)):
    r = rec(request)
    if key == "imagery_alignment":
        if not isinstance(value, dict):
            raise HTTPException(400, "expected an object")
        clean = {k: float(value.get(k, d)) for k, d in ALIGN_DEFAULT.items()}
        if abs(clean["east_m"]) > 50 or abs(clean["north_m"]) > 50 or abs(clean["rotate_deg"]) > 20 \
                or not 0.95 <= clean["scale"] <= 1.05:
            raise HTTPException(400, "alignment out of range (±50 m, ±20°, scale 0.95–1.05)")
        clean["opacity"] = max(0.0, min(1.0, clean["opacity"]))
        al = _alignments(r.store)
        al[request.app.state.imagery.source_id] = clean     # each source keeps its own alignment
        r.store.set_setting("imagery_alignments", al)
        return clean
    if key == "conditions":
        if not isinstance(value, dict):
            raise HTTPException(400, "expected an object")
        for k, v in value.items():
            if k == "robot_rain_wet_below":
                if v is not None and not _is_number(v):
                    raise HTTPException(400, "robot_rain_wet_below: a number, or null for not calibrated")
            elif k not in CONDITION_LIMITS:
                raise HTTPException(400, f"unknown condition setting {k!r}")
            elif not _is_number(v) or not CONDITION_LIMITS[k][0] <= v <= CONDITION_LIMITS[k][1]:
                raise HTTPException(400, f"{k} must be a number between {CONDITION_LIMITS[k][0]} and {CONDITION_LIMITS[k][1]}")
        merged = {**r.conditions_config().to_dict(), **value}
        if merged["damp_mm"] > merged["wet_mm"]:
            raise HTTPException(400, "damp_mm must be at or below wet_mm")
        if merged["soil_damp_pct"] > merged["soil_wet_pct"]:
            raise HTTPException(400, "soil damp must be at or below soil wet")
        cfg = ConditionsConfig.from_dict(merged)
        r.store.set_setting(key, cfg.to_dict())
        r.evaluate_conditions()
        return cfg.to_dict()
    if key == "deck_m":
        if value is not None and not (isinstance(value, (int, float)) and 0.2 <= value <= 1.5):
            raise HTTPException(400, "deck width must be between 0.2 and 1.5 m, or null for the head default")
        r.store.set_setting(key, value)
        r.store.analysis_clear()
        return {"deck_m": value}
    if key == "map_write_enabled":
        if not isinstance(value, bool):
            raise HTTPException(400, "true or false")
        r.store.set_setting(key, value)
        r.store.event("map_write_switched", "map writing switched ON" if value else "map writing switched off")
        return {"map_write_enabled": value}
    if key == "weather_sources":
        from . import weather as weather_mod
        try:
            clean = weather_mod.validate(value)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        r.store.set_setting(key, clean)
        r.apply_weather_sources()
        return clean
    if key == mcp_server.SWITCH:
        if not isinstance(value, bool):
            raise HTTPException(400, "true or false")
        r.store.set_setting(key, value)
        r.store.event("mcp_control_switched", "AI assistant commands switched ON" if value else "AI assistant commands switched off")
        return {key: value}
    if key == "robot_video_enabled":
        _person(request)                  # a person's session, as for the password: not an API token
        if not isinstance(value, bool):
            raise HTTPException(400, "true or false")
        r.store.set_setting(key, value)
        r.store.event("robot_video_switched", "robot video switched ON" if value else "robot video switched off")
        return {key: value}
    if key == "robot_route":
        from .link import ROUTE_MODES
        if value not in ROUTE_MODES:
            raise HTTPException(400, f"one of {', '.join(ROUTE_MODES)}")
        before = r.store.setting("robot_route") or "auto"
        r.store.set_setting(key, value)
        if before != value:
            r.store.event("route_setting_changed", f"route to the robot: {before} → {value}")
        return {key: value}
    if key == "wifi_survey":
        from .recorder import WIFI_MODES
        if value not in WIFI_MODES:
            raise HTTPException(400, f"one of {', '.join(WIFI_MODES)}")
        r.store.set_setting(key, value)
        r.store.event("wifi_survey_switched", {"off": "Wi-Fi survey switched off", "next": "Wi-Fi survey on for the next run",
                                               "on": "Wi-Fi survey on for every run"}[value])
        return {key: value}
    if key == "control_enabled":
        if not isinstance(value, bool):
            raise HTTPException(400, "true or false")
        r.store.set_setting(key, value)
        r.store.event("control_switched", "control switched ON" if value else "control switched off")
        return {"control_enabled": value}
    if key == "gates":
        if not isinstance(value, dict):
            raise HTTPException(400, "expected an object")
        merged = control.gates(r.store)
        for k, v in value.items():
            if k not in control.GATE_DEFAULTS:
                raise HTTPException(400, f"unknown gate {k!r}")
            default = control.GATE_DEFAULTS[k]
            if isinstance(default, bool):
                bad = not isinstance(v, bool)
            elif isinstance(default, (int, float)):
                bad = not _is_number(v)
            elif isinstance(default, str):
                # After dark a pause would leave the robot on the lawn all night: dock, notify or ignore only.
                bad = v not in control.GUARD_ACTIONS or (k == "guard_after_dark" and v == "pause")
            else:
                bad = not (isinstance(v, list) and all(isinstance(x, str) for x in v))
            if bad:
                raise HTTPException(400, f"bad value for {k}")
            if k == "guard_resume_after_min" and not 1 <= v <= 45:
                raise HTTPException(400, "resume after: 1 to 45 minutes")
            merged[k] = v
        before = control.gates(r.store)
        r.store.set_setting(key, {k: merged[k] for k in control.GATE_DEFAULTS})
        after = control.gates(r.store)
        changed = [f"{k} {_gate_text(before.get(k))} → {_gate_text(after[k])}" for k in control.GATE_DEFAULTS
                   if before.get(k) != after[k]]
        if changed:     # run rules and guards decide what the robot is sent: every change is on the record
            who = "an assistant" if getattr(getattr(request, "url", None), "hostname", None) == "panel" else "the panel"
            r.store.event("gates_changed", f"{'; '.join(changed)} (by {who})")
        return after
    if key == "yard_cameras":
        if not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
            raise HTTPException(400, "a list of camera names")
        r.store.set_setting(key, value)
        return {"yard_cameras": value}
    if key == "camera_polygons":
        if not isinstance(value, dict):
            raise HTTPException(400, "{camera: [[lon, lat], ...]}")
        clean = {}
        for cam, ring in value.items():
            try:
                pts = geo.clean_ring(ring)
            except (TypeError, ValueError):
                raise HTTPException(400, f"coverage for {cam}: expected [[lon, lat], ...]")
            if len(pts) < 3 or geo.ring_area_m2(pts) < 1:
                raise HTTPException(400, f"coverage for {cam} needs at least 3 separate corners around a real area")
            clean[cam] = pts
        r.store.set_setting(key, clean)
        return {"camera_polygons": clean}
    if key in ("presence_entity", "ha_notify_service", "webhook_url"):
        if value is not None and not isinstance(value, str):
            raise HTTPException(400, "a string or null")
        value = (value or "").strip() or None
        if value and key == "webhook_url" and not value.startswith(("http://", "https://")):
            raise HTTPException(400, "webhook must be an http(s) URL")
        if value and key == "presence_entity" and not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", value):
            raise HTTPException(400, "expected an entity id like alarm_control_panel.home")
        if value and key == "ha_notify_service" and not re.fullmatch(r"notify\.[a-z0-9_]+", value):
            raise HTTPException(400, "expected a notify service like notify.mobile_app_phone")
        r.store.set_setting(key, value)
        return {key: value}
    raise HTTPException(404, f"unknown setting {key!r}")


# -- robot video (§13) -----------------------------------------------------------------------------------------
@app.post("/api/video/token")
async def video_token(request: Request, body: dict = Body(...)):
    """Join details for one robot camera: Agora App ID, channel, token, the viewer's uid, and the stream's key and salt.
    People only: an API token is refused here and in the sign-in middleware (not in TOKEN_WRITES), and the MCP server
    has no route to it. Off until robot_video_enabled is on. Nothing is logged or recorded as an event except that a
    token was fetched (never the token, key, salt, channel or address) (§13)."""
    _person(request)
    r = rec(request)
    if not r.store.setting("robot_video_enabled", SETTINGS_DEFAULTS["robot_video_enabled"]):
        raise HTTPException(403, "robot video is off (Settings → Robot)")
    camera = body.get("camera")
    if not isinstance(camera, str) or camera not in video_mod.CAMERAS:
        raise HTTPException(400, f"camera: one of {', '.join(video_mod.CAMERAS)}")
    base = request.app.state.cfg.robot_service_url
    if not base:
        raise HTTPException(503, "robot video is not configured: set YARBO_ROBOT_SERVICE_URL")
    uid = secrets.randbelow(2**31 - 2) + 2              # chosen here: never 0, never the robot's 1
    http = getattr(request.app.state, "video_http", None)
    own = http is None
    if own:
        http = httpx.AsyncClient(timeout=15)
    try:
        tok = await video_mod.fetch(ops(request).cloud, http, base, camera, uid)
    except video_mod.VideoTokenError as exc:
        raise HTTPException(409 if exc.auth else 502, str(exc))    # a fixed phrase: no key, salt, token or address
    finally:
        if own:
            await http.aclose()
    return JSONResponse({"app_id": tok.app_id, "channel": tok.channel, "token": tok.token, "uid": tok.uid,
                         "key": tok.key, "salt": tok.salt}, headers={"Cache-Control": "no-store"})


# -- phase 2: cloud session -----------------------------------------------------------------------------------------
@app.post("/api/cloud/login")
async def cloud_login(request: Request, body: dict = Body(...)):
    """The password is handed straight to Yarbo's SDK (which RSA-encrypts it) and never stored or logged."""
    if request.app.state.cfg.demo:
        raise HTTPException(403, "this is a demo instance (DEMO=1): it never signs in to the Yarbo cloud")
    email, password = (body.get("email") or "").strip(), body.get("password") or ""
    if not email or not password:
        raise HTTPException(400, "email and password are required")
    try:
        await asyncio.to_thread(ops(request).cloud.login, email, password)
    except Exception as exc:
        raise HTTPException(401, f"sign-in failed: {type(exc).__name__}: {exc}")
    finally:
        password = None  # noqa: F841
    return ops(request).cloud.status()


@app.post("/api/cloud/logout")
async def cloud_logout(request: Request):
    await asyncio.to_thread(ops(request).cloud.logout)
    return ops(request).cloud.status()


# -- phase 2: control -------------------------------------------------------------------------------------------------
@app.get("/api/control/catalogue")
async def control_catalogue(request: Request):
    return {"enabled": ops(request).controller.enabled(),
            "commands": {k: v["label"] for k, v in control.CATALOGUE.items()},
            "blocked": sorted(control.HARD_BLOCKED), "gates": control.gates(rec(request).store)}


def _actor(request: Request) -> str:
    """Who a command is from, for the interlocks and the audit log: a program with an API token, or a person."""
    return "token" if getattr(request.state, "who", None) == "token" else "user"


@app.post("/api/control/preview")
async def control_preview(request: Request, body: dict = Body(...)):
    cmd = body.get("cmd", "")
    if cmd not in control.CATALOGUE:
        raise HTTPException(400, f"{cmd!r} is not in the catalogue")
    return await ops(request).controller.preview(cmd, body.get("args") or {}, actor=_actor(request))


@app.post("/api/control/send")
async def control_send(request: Request, body: dict = Body(...)):
    if body.get("confirm") is not True:
        raise HTTPException(400, "confirm: true is required")
    return await ops(request).controller.send(body.get("cmd", ""), body.get("args") or {}, actor=_actor(request),
                                              override=bool(body.get("override")), wait="background")


@app.get("/api/control/log")
async def control_log(request: Request, limit: int = 100):
    return {"log": rec(request).store.control_log(limit)}


# -- phase 2: schedule ------------------------------------------------------------------------------------------------
@app.get("/api/schedule")
async def schedule(request: Request):
    o = ops(request)
    return {"windows": o.scheduler.windows(), "upcoming": o.scheduler.upcoming(datetime.now()),
            "pending": o.scheduler.pending(), "log": rec(request).store.schedule_log(50),
            "plans": rec(request).plans, "robot_schedules": rec(request).robot_schedules}


@app.put("/api/schedule/windows")
async def schedule_windows(request: Request, body: list = Body(...)):
    try:
        return {"windows": ops(request).scheduler.save_windows(body)}
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/schedule/pending/confirm")
async def pending_confirm(request: Request):
    return await ops(request).confirm_pending()


@app.post("/api/schedule/pending/dismiss")
async def pending_dismiss(request: Request):
    return ops(request).dismiss_pending()


# -- phase 2: maintenance ---------------------------------------------------------------------------------------------
@app.get("/api/maintenance")
async def maintenance_view(request: Request):
    return ops(request).maintenance()


@app.put("/api/maintenance/items")
async def maintenance_items(request: Request, body: list = Body(...)):
    from .maintenance import RECOMMENDED, UNITS
    store = rec(request).store
    clean = []
    for it in body:
        if it.get("unit") not in UNITS or not str(it.get("name", "")).strip():
            raise HTTPException(400, "each item needs a name and a known unit")
        interval = it.get("interval")
        if interval is not None and not (isinstance(interval, (int, float)) and interval > 0):
            raise HTTPException(400, "interval must be a positive number or null")
        clean.append({"id": it.get("id") or __import__("uuid").uuid4().hex[:8],
                      "key": it["key"] if isinstance(it.get("key"), str) and it["key"] in RECOMMENDED else None,
                      "name": str(it["name"]).strip(),
                      "unit": it["unit"], "interval": interval, "done_at": it.get("done_at") or time.time(),
                      "baseline": it.get("baseline", 0), "note": str(it.get("note", ""))[:200]})
    store.set_setting("maintenance_items", clean)
    return ops(request).maintenance()


@app.post("/api/maintenance/{item_id}/done")
async def maintenance_done(request: Request, item_id: str, body: dict = Body(None)):
    o = ops(request)
    view = o.maintenance()
    items = rec(request).store.setting("maintenance_items") or []
    for it in items:
        if it["id"] == item_id:
            it["baseline"] = view["counters"].get(it["unit"], 0)
            it["done_at"] = time.time()
            it["note"] = str((body or {}).get("note", ""))[:200]
            rec(request).store.event("maintenance_done", it["name"])
            break
    else:
        raise HTTPException(404, "no such item")
    rec(request).store.set_setting("maintenance_items", items)
    return o.maintenance()


# -- phase 2: Frigate and follow-cam ----------------------------------------------------------------------------------
@app.get("/api/frigate/cameras")
async def frigate_cameras(request: Request):
    o = ops(request)
    return {"cameras": await o.frigate.cameras(), "yard": o.yard_cameras(),
            "polygons": rec(request).store.setting("camera_polygons", {}) or {}, "status": o.frigate.status()}


@app.get("/api/frigate/snapshot/{camera}.jpg")
async def frigate_snapshot(request: Request, camera: str, height: int = 360):
    data = await ops(request).frigate.snapshot(camera, max(120, min(height, 1080)))
    if data is None:
        raise HTTPException(404, "no snapshot")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/frigate/stream/{camera}.mjpeg")
async def frigate_stream(request: Request, camera: str, height: int = 720, fps: int = 5):
    """The follow-cam as live MJPEG (an <img> plays it). Ends when the browser goes away."""
    s = await ops(request).frigate.stream(camera, max(120, min(height, 1080)), max(1, min(fps, 15)))
    if s is None:
        raise HTTPException(404, "no stream")
    ctype, chunks = s

    async def body():
        last = time.monotonic()
        try:
            async for b in chunks:
                yield b
                if time.monotonic() - last > 2:
                    last = time.monotonic()
                    if await request.is_disconnected():
                        break
        finally:
            await chunks.aclose()

    return StreamingResponse(body(), media_type=ctype, headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/followcam")
async def followcam(request: Request):
    o = ops(request)
    g = control.gates(rec(request).store)
    return {**o.follow_camera(), "yard": await o.yard_status(g["yard_clear_minutes"], g["yard_labels"])}


@app.post("/api/notify/test")
async def notify_test(request: Request):
    return await ops(request).notifier.send("test", "Groundskeeper test", "Notifications are working.",
                                            url=f"{request.app.state.cfg.panel_url}/#live" if request.app.state.cfg.panel_url else None)


# -- Map Studio (spec §7) ---------------------------------------------------------------------------------------------
WRITE_REASON = ("No-go zones and pathways can be written: adding, deleting and changing them (a change is sent as "
                "add-the-new, delete-the-old). Areas and no-vision zones cannot: the app reshapes areas only by driving "
                "the robot. After writing, the map is read back and compared; anything off is undone.")


def _gps_ref(request: Request):
    ref = rec(request).gps_ref
    if ref is None:
        raise HTTPException(503, "the GPS reference has not been read yet")
    return ref


def _current_sha(request: Request) -> str | None:
    v = rec(request).store.versions("map")
    return v[0]["sha"] if v else None


def _map_for(request: Request, ref: str) -> dict:
    r = rec(request)
    if ref == "current":
        if r.map_raw is None:
            raise HTTPException(503, "map not fetched yet")
        return r.map_raw
    if ref == "draft":
        d = r.store.setting("map_draft")
        if not d:
            raise HTTPException(404, "no draft")
        return d["map"]
    kind, _, ident = ref.partition(":")
    if kind == "version" and ident.isdigit():
        v = r.store.version(int(ident))
        if v and v["kind"] == "map":
            return v["json"]
    if kind == "backup" and ident.lstrip("-").isdigit():
        m = r.store.latest_version(f"backup:{int(ident)}")
        if m is not None:
            return m
        raise HTTPException(404, f"backup {ident} has not been fetched yet")
    raise HTTPException(404, f"unknown map {ref!r}")


def _ref_label(request: Request, ref: str) -> str:
    if ref == "current":
        return "the robot's current map"
    if ref == "draft":
        return "the draft"
    kind, _, ident = ref.partition(":")
    if kind == "version":
        v = rec(request).store.version(int(ident)) if ident.isdigit() else None
        return f"recorded {datetime.fromtimestamp(v['fetched']).strftime('%b %d %H:%M')}" if v else ref
    if kind == "backup":
        b = next((b for b in rec(request).backups() if str(b["id"]) == ident), None)
        if b:
            when = datetime.fromtimestamp(b["timestamp"] / (1000 if b["timestamp"] > 1e11 else 1)).strftime("%b %d %Y") \
                if b.get("timestamp") else ""
            return f"robot backup “{b['name'] or b['id']}” {when}".strip()
    return ref


def _draft_meta(request: Request, d: dict | None) -> dict | None:
    if not d:
        return None
    undo = rec(request).store.setting("map_draft_undo") or []
    return {"from": d["from"], "from_label": d["from_label"], "created": d["created"], "updated": d["updated"],
            "log": d.get("log", [])[-30:], "undo": len(undo),
            "stale": bool(d.get("base_sha")) and d.get("base_sha") != _current_sha(request)}


def _write_checks(request: Request, d: dict, issues: list, plan: dict) -> list[dict]:
    r = rec(request)
    t = r.t
    snap_age = round(time.time() - r.snapshot_at, 1) if r.snapshot_at else None
    errors = [i for i in issues if i["level"] == "error"]
    on = bool(r.store.setting("map_write_enabled", False))
    cloud = ops(request).cloud.state
    starting = ops(request).controller.start_settling()
    checks = [
        ("Map writing switched on", on, "on" if on else "off (switch it on below)"),
        ("Yarbo cloud connected", cloud == "connected", cloud),
        ("Robot online", snap_age is not None and snap_age < 120, f"last snapshot {snap_age} s ago" if snap_age else "no data"),
        ("Docked", (t.get("charging") or 0) > 0, "on the charger" if (t.get("charging") or 0) > 0 else "not on the charger"),
        ("Idle", not (r.run_id or t.get("planning")), "a plan is running" if (r.run_id or t.get("planning")) else "idle"),
        ("No start just sent", not starting, starting or "none"),
        ("Draft is valid", not errors, f"{len(errors)} error(s)" if errors else "no errors"),
        ("Draft starts from the robot's current map", not _draft_meta(request, d)["stale"],
         "the robot's map changed after this draft was started" if _draft_meta(request, d)["stale"] else "yes"),
        ("Something to write", bool(plan["steps"]),
         f"{len(plan['steps'])} command(s)" + (f"; {len(plan['blocked'])} change(s) stay in the draft" if plan["blocked"] else "")
         if plan["steps"] else ("only changes the robot cannot take yet; they stay in the draft" if plan["blocked"] else "nothing")),
    ]
    return [{"name": a, "ok": bool(b), "hard": True, "detail": c} for a, b, c in checks]


DRAFT_CONFLICT = "The draft was changed in another window or tab. Reloaded it; please redo your last step."


def _check_base(d: dict | None, base, given: bool) -> None:
    """Optimistic locking for the one shared draft: a client sends the `updated` stamp of the draft it was looking
    at (null for none), and an edit based on an older draft is refused instead of silently overwriting it."""
    if given and base != (d["updated"] if d else None):
        raise HTTPException(409, DRAFT_CONFLICT)


def _draft_view(request: Request) -> dict:
    r = rec(request)
    d = r.store.setting("map_draft")
    if not d:
        raise HTTPException(404, "no draft")
    ref = _gps_ref(request)
    cur = r.map_raw or {}
    issues = mapstudio.validate_draft(cur, d["map"], ref)
    plan = mapwrite.plan(cur, d["map"], ref)
    changes = plan["diff"]
    return {"meta": _draft_meta(request, d), "geojson": mapstudio.geojson(d["map"], ref), "issues": issues,
            "diff": changes, "overlay": mapstudio.diff_geojson(cur, d["map"], ref, changes),
            "checks": _write_checks(request, d, issues, plan),
            "send": {"steps": [{"label": s["label"], "cmd": s["cmd"],
                                "big": bool(s.get("big")) or (s["op"] == "update" and d.get("from") != "current")}
                               for s in plan["steps"]],
                     "blocked": plan["blocked"], "enabled": bool(r.store.setting("map_write_enabled", False))},
            "last_write": r.store.setting("map_last_write"),
            "features": mapstudio.summary(d["map"], ref, r.cfd)}


async def _map_issues(raw, ref) -> list:
    if not raw or not ref:
        return []
    try:
        return await asyncio.to_thread(mapstudio.validate, raw, ref)
    except Exception:
        log.exception("validating the robot's map failed")
        return []


@app.get("/api/studio")
async def studio(request: Request):
    r = rec(request)
    ref = r.gps_ref
    versions = [{"ref": f"version:{v['id']}", "fetched": v["fetched"], "sha": v["sha"], "bytes": v["bytes"]}
                for v in r.store.versions("map")]
    return {"features": mapstudio.summary(r.map_raw, ref, r.cfd) if r.map_raw and ref else [],
            "map_fetched": r.map_fetched, "versions": versions, "backups": r.backups(),
            "draft": _draft_meta(request, r.store.setting("map_draft")), "captures": r.store.captures(25),
            "max_points": mapstudio.MAX_POINTS, "max_points_areas": mapstudio.AREA_MAX_POINTS, "addable": list(mapstudio.ADDABLE),
            "backup_slots": mapwrite.MANUAL_BACKUPS, "last_restore": r.store.setting("map_last_restore"),
            "issues": await _map_issues(r.map_raw, ref),   # what the robot's map already has
            "write": {"available": True, "enabled": bool(r.store.setting("map_write_enabled", False)),
                      "reason": WRITE_REASON, "last": r.store.setting("map_last_write")}}


@app.get("/api/studio/geojson")
async def studio_geojson(request: Request, ref: str = "current"):
    return mapstudio.geojson(_map_for(request, ref), _gps_ref(request))


@app.get("/api/studio/diff")
async def studio_diff(request: Request, a: str, b: str = "current"):
    ref = _gps_ref(request)
    ma, mb = _map_for(request, a), _map_for(request, b)
    d = mapstudio.diff(ma, mb, ref)
    return {"a": a, "b": b, "a_label": _ref_label(request, a), "b_label": _ref_label(request, b), "diff": d,
            "overlay": mapstudio.diff_geojson(ma, mb, ref, d)}


@app.get("/api/studio/export")
async def studio_export(request: Request, ref: str = "current", fmt: str = "geojson"):
    raw = _map_for(request, ref)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    base = f"yarbo-map-{ref.replace(':', '-')}-{stamp}"
    if fmt == "json":
        body, mime, ext = json.dumps(raw, indent=1), "application/json", "json"
    elif fmt == "kml":
        body = mapstudio.to_kml(mapstudio.geojson(raw, _gps_ref(request)), f"Yarbo map ({_ref_label(request, ref)})")
        mime, ext = "application/vnd.google-earth.kml+xml", "kml"
    elif fmt == "geojson":
        body, mime, ext = json.dumps(mapstudio.geojson(raw, _gps_ref(request))), "application/geo+json", "geojson"
    else:
        raise HTTPException(400, "fmt is geojson, kml or json")
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{base}.{ext}"'})


@app.post("/api/studio/reload")
async def studio_reload(request: Request):
    """Read the robot's map now (get_map); a new version is recorded if it changed."""
    try:
        await rec(request).reload_map()
    except asyncio.TimeoutError:
        raise HTTPException(504, "the robot did not answer get_map")
    return {"ok": True, "fetched": rec(request).map_fetched}


@app.post("/api/studio/backups/refresh")
async def studio_backups_refresh(request: Request):
    try:
        return {"backups": await rec(request).refresh_backups()}
    except asyncio.TimeoutError:
        raise HTTPException(504, "the robot did not answer get_all_map_backup")


def _backup_block(request: Request, restore: bool) -> str | None:
    r, t = rec(request), rec(request).t
    if not r.store.setting("map_write_enabled", False):
        return "map writing is switched off (Map Studio → Yard, or Settings → Safety)"
    if ops(request).cloud.state != "connected":
        return f"the Yarbo cloud is {ops(request).cloud.state}"
    if restore:
        age = time.time() - r.snapshot_at if r.snapshot_at else None
        if age is None or age > 120:
            return "no recent data from the robot"
        if not (t.get("charging") or 0) > 0:
            return "the robot must be on its charger"
        if r.run_id or r.plan_active() is not False:
            return "a plan may be running (the robot's plan state is not idle)"
        return ops(request).controller.start_settling()
    return None


async def _backup_action(request: Request, coro_fn, restore: bool = False):
    why = _backup_block(request, restore)
    if why:
        raise HTTPException(409, why)
    try:
        result = await coro_fn()
    except mapwrite.WriteRefused as exc:
        raise HTTPException(409, str(exc))
    try:
        result["backups"] = await rec(request).refresh_backups()
    except Exception:
        pass
    return result


@app.post("/api/studio/backups/save")
async def studio_backup_save(request: Request):
    """Make a robot backup of the map as it is now (the app's "Save Current Map")."""
    return await _backup_action(request, ops(request).backups.save)


@app.post("/api/studio/backups/{bid}/delete")
async def studio_backup_delete(request: Request, bid: int, body: dict = Body(...)):
    if body.get("confirm") is not True:
        raise HTTPException(400, "confirm: true is required")
    return await _backup_action(request, lambda: ops(request).backups.delete(bid))


@app.post("/api/studio/backups/{bid}/rename")
async def studio_backup_rename(request: Request, bid: int, body: dict = Body(...)):
    """Rename a robot backup (rename_map_backup, captured 2026-10-10); read back from the list."""
    try:
        return await _backup_action(request, lambda: ops(request).backups.rename(bid, body.get("name")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/studio/backups/{bid}/restore")
async def studio_backup_restore(request: Request, bid: int, body: dict = Body(...)):
    """Replace the robot's map with one of its backups. The map, plans and settings as they are now are kept locally
    first ("prerestore"; no robot slot used); plans are deleted by the robot and listed for recreating; area mow
    settings are put back where the backup keeps the area's id, once the restored map is confirmed."""
    if body.get("confirm") is not True:
        raise HTTPException(400, "confirm: true is required")
    why = _backup_block(request, True)                  # before the (slow) backup read
    if why:
        raise HTTPException(409, why)
    r = rec(request)
    try:
        backup_map = await r.fetch_backup(bid)
    except Exception as exc:
        raise HTTPException(504, f"could not read backup {bid} from the robot first ({exc})")
    result = await _backup_action(request, lambda: ops(request).backups.restore(
        bid, backup_map, _gps_ref(request), keep_settings=body.get("keep_settings", True) is not False,
        keep_plans=body.get("keep_plans", True) is not False), restore=True)
    with contextlib.suppress(Exception):
        await r.refresh_map()                           # plans and map as the robot has them now
    return result


@app.get("/api/studio/backups/{bid}/preview")
async def studio_backup_preview(request: Request, bid: int):
    """What restoring this backup would change (areas kept / removed / added, plans deleted). Reads only; the map
    and plans are read fresh, since the cached ones can be hours old."""
    r = rec(request)
    try:
        backup_map = await r.fetch_backup(bid)
        await r.refresh_map()
    except Exception as exc:
        raise HTTPException(504, f"could not read backup {bid} and the current map from the robot ({exc})")
    return {"preview": backups_mod.preview(r.map_raw or {}, backup_map, r.plans or []),
            "blocked": _backup_block(request, True)}


@app.post("/api/studio/backups/{bid}/fetch")
async def studio_backup_fetch(request: Request, bid: int):
    try:
        raw = await rec(request).fetch_backup(bid)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"the robot did not answer for backup {bid}")
    except ValueError as exc:
        raise HTTPException(502, str(exc))
    counts = {k: len(raw.get(k) or []) for k in mapstudio.POLYGONS + mapstudio.LINES if raw.get(k)}
    return {"ok": True, "ref": f"backup:{bid}", "counts": counts}


# -- network: link strengths and the robot's Wi-Fi (app/network.py) ------------------------------------------------
def _network_write_block(request: Request, held: bool = False) -> str | None:
    """Joining or forgetting a Wi-Fi network can cut a link, so only with the robot on its charger and no plan.
    held: asked again by the write itself, which holds the writer's lock by then."""
    r, t = rec(request), rec(request).t
    if not r.store.setting("map_write_enabled", False):
        return "robot writes are switched off (Settings → Safety → Map writing)"
    if ops(request).cloud.state != "connected":
        return f"the Yarbo cloud is {ops(request).cloud.state}"
    age = time.time() - r.snapshot_at if r.snapshot_at else None
    if age is None or age > 120:
        return "no recent data from the robot"
    if not (t.get("charging") or 0) > 0:
        return "the robot must be on its charger"
    if r.run_id or r.plan_active() is not False:
        return "a plan may be running (the robot's plan state is not idle)"
    if not held and ops(request).map_writer.lock.locked():
        return "another write to the robot is in progress"
    return ops(request).controller.start_settling()


@app.get("/api/network")
async def network_status(request: Request):
    """Link strengths and the latest Wi-Fi scan, as already known (asks the robot nothing)."""
    return {**ops(request).network.status(), "write_block": _network_write_block(request)}


@app.post("/api/network/scan")
async def network_scan(request: Request):
    """A fresh Wi-Fi scan on the robot (get_wifi_list, a read) and the connected network's name; not during a plan."""
    if not rec(request).can_read():
        raise HTTPException(409, "no route to the robot")
    try:
        result = await ops(request).network.scan()
    except mapwrite.WriteRefused as exc:
        raise HTTPException(409, str(exc))
    return {**result, "write_block": _network_write_block(request)}


async def _network_write(request: Request, coro_fn):
    why = _network_write_block(request)
    if why:
        raise HTTPException(409, why)
    try:
        result = await coro_fn()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except mapwrite.WriteRefused as exc:
        raise HTTPException(409, str(exc))
    return {**result, **ops(request).network.status(), "write_block": _network_write_block(request)}


@app.post("/api/network/wifi/join")
async def network_wifi_join(request: Request, body: dict = Body(...)):
    """Join a network from the latest scan (change_to_wifi, the phone app's command). Confirmed by the robot naming it
    as its connected network. The password goes to the robot only; it is never stored or logged."""
    gate = lambda: _network_write_block(request, held=True)        # noqa: E731
    return await _network_write(request, lambda: ops(request).network.join(body.get("name"), body.get("password"), gate))


@app.post("/api/network/wifi/forget")
async def network_wifi_forget(request: Request, body: dict = Body(...)):
    """Forget a saved network (forget_wifi). Confirmed by a fresh scan no longer marking it saved."""
    gate = lambda: _network_write_block(request, held=True)        # noqa: E731
    return await _network_write(request, lambda: ops(request).network.forget(body.get("name"), gate))


@app.get("/api/studio/draft")
async def studio_draft(request: Request):
    return _draft_view(request)


async def _create_draft(request: Request, src: str) -> None:
    if src == "current":   # start from what is on the robot now, not the copy from the last 6-hourly read
        with contextlib.suppress(Exception):
            await rec(request).reload_map()
    raw = _map_for(request, src)
    store = rec(request).store
    now = time.time()
    known = [rec(request).map_raw] + [store.version(v["id"])["json"] for v in store.versions("map")] + \
            [store.version(v["id"])["json"] for v in store.versions_like("backup:%")]
    store.set_setting("map_draft", {"from": src, "from_label": _ref_label(request, src), "base_sha": _current_sha(request),
                                    "created": now, "updated": now, "map": raw, "id_floor": mapstudio.max_feature_id(*known),
                                    "log": [f"started from {_ref_label(request, src)}"]})
    store.set_setting("map_draft_undo", [])


@app.post("/api/studio/draft")
async def studio_draft_create(request: Request, body: dict = Body(...)):
    await _create_draft(request, body.get("from") or "current")
    return _draft_view(request)


@app.delete("/api/studio/draft")
async def studio_draft_discard(request: Request, base: float | None = None):
    store = rec(request).store
    _check_base(store.setting("map_draft"), base, base is not None)
    store.set_setting("map_draft", None)
    store.set_setting("map_draft_undo", [])
    return {"ok": True}


_draft_lock = asyncio.Lock()    # edits run off the loop now: one read-modify-write of the draft at a time


@app.post("/api/studio/draft/edit")
async def studio_draft_edit(request: Request, body: dict = Body(...)):
    async with _draft_lock:
        return await _draft_edit(request, body)


async def _draft_edit(request: Request, body: dict):
    store = rec(request).store
    given = "base" in body
    _check_base(store.setting("map_draft"), body.pop("base", None), given)
    started = not store.setting("map_draft")
    if started:
        await _create_draft(request, "current")   # the first edit starts a draft from the robot's map
    d = store.setting("map_draft")
    try:
        if body.get("op") == "revert":
            new, desc = mapstudio.revert(d["map"], rec(request).map_raw or {}, body.get("kind"), body.get("id"))
        else:   # off the event loop: a tidy on a long boundary takes a moment
            new, desc = await asyncio.to_thread(mapstudio.apply_edit, d["map"], body, _gps_ref(request), d.get("id_floor", 0))
    except mapstudio.EditError as exc:
        if started:
            store.set_setting("map_draft", None)
        raise HTTPException(400, str(exc))
    undo = (store.setting("map_draft_undo") or [])[-24:]
    undo.append({"map": d["map"], "log": d.get("log", [])})
    store.set_setting("map_draft_undo", undo)
    d.update(map=new, updated=time.time(), log=d.get("log", []) + [desc])
    store.set_setting("map_draft", d)
    return _draft_view(request)


# -- Map Studio: tidy (read-only previews; applying one is a draft edit, op "tidy" / "tidy_many") -----------------
def _tidy_map(request: Request) -> dict:
    d = rec(request).store.setting("map_draft")
    return d["map"] if d else _map_for(request, "current")


def _tidy_row(kind: str, f: dict, r: dict | None, ref) -> dict:
    row = {"kind": kind, "id": f.get("id"), "name": f.get("name") or "", "label": mapstudio.KIND_LABELS.get(kind, kind),
           "bias": tidy.BIAS.get(kind), "sendable": kind in mapwrite.SENDABLE or kind in mapwrite.UPDATABLE}
    if r:
        row.update({k: r[k] for k in ("ok", "tool", "why", "needs_tol", "max_move", "before", "after") if k in r})
        if r.get("ok"):
            row["geometry"] = mapstudio.tidy_geometry(kind, f, r["pts"], ref)
    return row


@app.post("/api/studio/tidy/preview")
async def studio_tidy_preview(request: Request, body: dict = Body(...)):
    """What each tool would do to one shape (in the draft when there is one), without changing anything."""
    ref, m = _gps_ref(request), _tidy_map(request)
    kind, fid = body.get("kind"), body.get("id")
    f = mapstudio.find(m, kind, fid) if isinstance(kind, str) else None
    if f is None:
        raise HTTPException(404, f"no {kind} with id {fid}")
    if kind not in mapstudio.TIDY_KINDS:
        raise HTTPException(400, f"{mapstudio.KIND_LABELS.get(kind, kind)}s cannot be tidied")
    if not isinstance(kind, str):
        raise HTTPException(400, "kind must be a string")
    try:
        tol = mapstudio._tol(body.get("tol"))
    except mapstudio.EditError as exc:
        raise HTTPException(400, str(exc))
    tools = [t for t in (body.get("tools") or tidy.TOOLS) if t in tidy.TOOLS]
    if kind not in mapstudio.POLYGONS:
        tools = [t for t in tools if t not in ("round", "square")]

    def work():
        return {t: _tidy_row(kind, f, mapstudio.tidy_run(kind, f, t, tol), ref) for t in tools}
    return {"kind": kind, "id": fid, "tol": tol, "bias": tidy.BIAS.get(kind), "tools": await asyncio.to_thread(work)}


@app.post("/api/studio/tidy/review")
async def studio_tidy_review(request: Request, body: dict = Body(None)):
    """One suggested cleanup per shape on the whole map (draft or current), for the Tidy map card."""
    ref, m = _gps_ref(request), _tidy_map(request)
    try:
        tol = mapstudio._tol((body or {}).get("tol"))
    except mapstudio.EditError as exc:
        raise HTTPException(400, str(exc))

    def work():
        rows = []
        for kind, f in mapstudio.iter_features(m):
            if kind not in mapstudio.TIDY_KINDS:
                continue
            r = tidy.recommend(mapstudio.own_points(f), kind in mapstudio.POLYGONS, tol, tidy.BIAS.get(kind))
            if r:
                rows.append(_tidy_row(kind, f, r, ref))
        return rows
    return {"tol": tol, "rows": await asyncio.to_thread(work)}


@app.post("/api/studio/draft/undo")
async def studio_draft_undo(request: Request, body: dict = Body(None)):
    async with _draft_lock:
        return await _studio_draft_undo(request, body)


async def _studio_draft_undo(request: Request, body: dict = Body(None)):
    store = rec(request).store
    d, undo = store.setting("map_draft"), store.setting("map_draft_undo") or []
    _check_base(d, (body or {}).get("base"), "base" in (body or {}))
    if not d or not undo:
        raise HTTPException(400, "nothing to undo")
    prev = undo.pop()
    d.update(map=prev["map"], log=prev["log"], updated=time.time())
    store.set_setting("map_draft", d)
    store.set_setting("map_draft_undo", undo)
    return _draft_view(request)


@app.post("/api/studio/draft/apply")
async def studio_draft_apply(request: Request, body: dict = Body(...)):
    """Write the draft to the robot (app.mapwrite). Needs confirm: true, the draft stamp the user was looking at,
    and every check passing; then re-reads the robot's map to be sure the draft still starts from it. One apply at a
    time: the re-read, the checks and the write happen together."""
    if body.get("confirm") is not True:
        raise HTTPException(400, "confirm: true is required")
    async with _APPLY_LOCK:
        return await _apply_draft(request, body)


_APPLY_LOCK = asyncio.Lock()


async def _apply_draft(request: Request, body: dict):
    r, store = rec(request), rec(request).store
    d = store.setting("map_draft")
    _check_base(d, body.get("base"), True)
    view = _draft_view(request)
    failed = [c for c in view["checks"] if not c["ok"]]
    if failed:
        return JSONResponse({"sent": False, "refused": "; ".join(f"{c['name']}: {c['detail']}" for c in failed),
                             "checks": view["checks"]}, status_code=409)
    fresh = await r.reload_map()
    if _current_sha(request) != d.get("base_sha"):
        return JSONResponse({"sent": False, "refused": "the robot's map changed since this draft was started; "
                             "nothing was sent", "checks": view["checks"]}, status_code=409)
    plan = mapwrite.plan(fresh, d["map"], _gps_ref(request))
    from_old = d.get("from") != "current" and any(s["op"] == "update" for s in plan["steps"])
    if from_old and body.get("allow_big") is not True:
        # Putting areas or no-vision zones back from an older version or a backup (the local undo) always needs
        # the explicit OK.
        return JSONResponse({"sent": False, "refused": "this draft puts areas or no-vision zones back from an older "
                             "version or a backup; confirm those changes to send it"}, status_code=409)
    try:
        result = await ops(request).map_writer.write(fresh, d["map"], _gps_ref(request),
                                                     allow_big=body.get("allow_big") is True)
    except mapwrite.WriteRefused as exc:
        return JSONResponse({"sent": False, "refused": str(exc)}, status_code=409)
    result["at"] = time.time()
    store.set_setting("map_last_write", result)
    if result.get("ok"):
        blocked = mapwrite.plan(fresh, d["map"], _gps_ref(request))["blocked"]
        if blocked:
            # What could not be sent stays in the draft, now on top of the robot's updated map.
            after = await r.reload_map()
            now = time.time()
            store.set_setting("map_draft", {**d, "map": mapwrite.carry_unsent(after, d["map"], blocked),
                                            "base_sha": _current_sha(request), "updated": now,
                                            "log": [*d.get("log", []), f"sent {len(result['steps'])} change(s); "
                                                    f"{len(blocked)} kept in the draft"]})
            result["kept_in_draft"] = [b.get("name") or b["id"] for b in blocked]
        else:
            store.set_setting("map_draft", None)
        store.set_setting("map_draft_undo", [])
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.get("/api/studio/app-edit")
async def app_edit_status(request: Request):
    return {"session": rec(request).session_view()}


@app.post("/api/studio/app-edit")
async def app_edit(request: Request, body: dict = Body(...)):
    """Record an app edit: start (reads the map, then keeps every app command and robot message) or stop (reads
    the map again, shows what changed, saves everything to DATA_DIR/captures). Nothing is sent to the robot but reads."""
    r = rec(request)
    if body.get("action") == "start":
        minutes = body.get("minutes", 30)
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 1 <= minutes <= 120:
            raise HTTPException(400, "minutes: 1 to 120")
        try:
            return {"session": await r.start_session(float(minutes))}
        except Exception as exc:
            raise HTTPException(503, f"could not read the map first ({exc})")
    if body.get("action") == "stop":
        return {"session": await r.stop_session()}
    raise HTTPException(400, "action: start or stop")


@app.get("/api/studio/captures")
async def studio_captures(request: Request, limit: int = 200):
    return {"captures": rec(request).store.captures(limit)}


@app.get("/api/studio/captures/{cid}")
async def studio_capture(request: Request, cid: int):
    c = rec(request).store.capture(cid)
    if not c:
        raise HTTPException(404, "no such capture")
    return c


@app.get("/api/studio/overlay")
async def studio_overlay(request: Request):
    return rec(request).store.setting("studio_overlay") or {"type": "FeatureCollection", "features": [], "name": None}


@app.put("/api/studio/overlay")
async def studio_overlay_put(request: Request, body: dict = Body(...)):
    """A reference layer the user imports (a survey, a GeoJSON or KML export). Display only; never sent anywhere."""
    if body.get("type") != "FeatureCollection" or not isinstance(body.get("features"), list):
        raise HTTPException(400, "expected a GeoJSON FeatureCollection")
    if len(json.dumps(body)) > 2_000_000:
        raise HTTPException(413, "overlay too large (2 MB max)")
    rec(request).store.set_setting("studio_overlay", {"type": "FeatureCollection", "name": str(body.get("name") or "")[:80],
                                                      "features": body["features"]})
    return {"ok": True, "features": len(body["features"])}


@app.delete("/api/studio/overlay")
async def studio_overlay_delete(request: Request):
    rec(request).store.set_setting("studio_overlay", None)
    return {"ok": True}


@app.get("/api/stats")
async def stats(request: Request):
    return rec(request).store.counts()


ALIGN_DEFAULT = {"east_m": 0.0, "north_m": 0.0, "rotate_deg": 0.0, "scale": 1.0, "opacity": 1.0}


def _alignments(store) -> dict:
    """One alignment per imagery source (sources sit differently). The pre-catalog single alignment was NAIP's."""
    al = store.setting("imagery_alignments")
    if al is None:
        old = store.setting("imagery_alignment")
        al = {"usgs_naip": {**ALIGN_DEFAULT, **old}} if old else {}
        store.set_setting("imagery_alignments", al)
    return al


def _alignment(request: Request) -> dict:
    src = request.app.state.imagery.source_id
    return {**ALIGN_DEFAULT, **_alignments(rec(request).store).get(src, {})}


def _imagery_view(request: Request) -> dict:
    r = rec(request)
    return {"yard": r.yard, "context": request.app.state.imagery.context_info(),
            "alignment": _alignment(request), "ref": list(r.gps_ref) if r.gps_ref else None}


@app.get("/api/imagery")
async def imagery(request: Request):
    return _imagery_view(request)


def _yard_centre(request: Request, required: bool = True) -> tuple[float, float] | tuple[None, None]:
    im = request.app.state.imagery
    bbox = im.bbox or (geo.bounds(rec(request).map_geojson) if rec(request).map_geojson else None)
    if not bbox:
        if required:
            raise HTTPException(503, "the map has not been read yet")
        return None, None
    return (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2


def _preview_url(im, src: dict, result: dict | None) -> str | None:
    if result and result.get("ok") and (not src.get("auth") or src.get("has_key")) and im.preview_file(src["id"]):
        return f"/api/imagery/sources/{src['id']}/preview.jpg?v={int(result.get('at') or result.get('error_at') or 0)}"
    return None


@app.get("/api/imagery/sources")
async def imagery_sources(request: Request):
    im, store = request.app.state.imagery, rec(request).store
    lon, lat = _yard_centre(request, required=False)
    probe = store.setting("imagery_probe") or {}
    sources = [{**c, "probe": probe.get(c["id"]), "preview": _preview_url(im, c, probe.get(c["id"]))}
               for c in im.catalog(lon, lat)]
    world = im.world
    auto = store.setting("imagery_auto") or {}
    return {"current": im.source_id, "mode": im.mode, "custom": im.local_entry("custom"), "sources": sources,
            "auto": {"choice": im.auto_choice(probe, lon, lat) if lon is not None else None,
                     "chosen": auto.get("chosen"), "at": auto.get("at"), "failed": auto.get("failed"),
                     "error": auto.get("error")},
            "counts": {"contacted": len(im.candidates(lon, lat)) if lon is not None else 0,
                       "elsewhere": sum(1 for c in sources if c["covers"] is False)},
            "kinds": im.kinds(), "problems": im.problems, "yard": rec(request).yard,
            "local_catalog": str(im.local_dir),
            "context": {"world_on": im.world_on,
                        "world_layer": {"name": world["name"], "attribution": world["attribution"],
                                        "terms": world["license"].get("name", ""), "max_zoom": world["max_zoom"],
                                        "homepage": world.get("homepage"), "native_m": world["native_m"],
                                        "license": world["license"]} if world else None}}


@app.get("/api/imagery/kinds")
async def imagery_kinds(request: Request):
    """The protocols a source can use, with the fields each one reads (what a source form needs)."""
    return {"kinds": request.app.state.imagery.kinds()}


@app.get("/api/imagery/sources/{sid}/preview.jpg")
async def imagery_preview(request: Request, sid: str):
    """The 40 m sample of the yard the last check fetched from this source."""
    path = request.app.state.imagery.preview_file(sid)
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


# -- choosing the source -------------------------------------------------------------------------------------------------
# The source and the mode change in requests and in the upkeep task; Imagery.lock serialises every such change (the
# functions marked "lock held" expect their caller to hold it). Checks (probes) run outside it: they change neither.

def _map_centre(app) -> tuple[float, float] | None:
    im, r = app.state.imagery, app.state.rec
    if im.yard_centre():
        return im.yard_centre()
    bbox = geo.bounds(r.map_geojson) if r.map_geojson else None
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2) if bbox else None


async def _check(app, ids: list[str] | None = None) -> dict:
    """Sample these sources (default: every one that may be contacted for this yard) and keep the results. Only
    sources whose outline contains the yard are ever asked (decided locally, in Imagery.check)."""
    im, store = app.state.imagery, app.state.store
    centre = _map_centre(app)
    if not centre:
        raise HTTPException(503, "the map has not been read yet")
    results = await im.check(ids or im.candidates(*centre), *centre, store.setting("imagery_probe") or {})
    probe = {**(store.setting("imagery_probe") or {}), **results}      # no await between read and write
    store.set_setting("imagery_probe", probe)
    return probe


def _forget_sample(app, sid: str) -> None:
    store = app.state.store
    probe = store.setting("imagery_probe") or {}
    if probe.pop(sid, None) is not None:
        store.set_setting("imagery_probe", probe)
    app.state.imagery.drop_sample(sid)


async def _switch(app, provider: str, mode: str) -> None:
    """Show this source (lock held). Fetches the yard only when there is no fresh image of it; the previous choice
    stays, and HTTPException(502) is raised, if that fails."""
    im, store, r = app.state.imagery, app.state.store, app.state.rec
    before = {"provider": im.source_id, "mode": im.mode}
    im.configure({"provider": provider, "mode": mode}, store.setting("imagery_keys"))
    try:
        r.yard = await im.ensure_yard(strict=True)
    except ImageryError as exc:
        im.configure(before, store.setting("imagery_keys"))
        raise HTTPException(502, f"could not use that source: {exc}")
    store.set_setting("imagery_source", {"provider": provider, "mode": mode})


async def _apply_auto(app, probe: dict) -> dict:
    """Automatic mode (lock held): switch to the sharpest free source with imagery at the yard, noting a switch in
    Events. Never raises: a failed switch is recorded, and the upkeep tries it again a day later at the earliest."""
    im, store = app.state.imagery, app.state.store
    centre = _map_centre(app)
    if im.mode != "auto" or not centre:
        return {}
    choice = im.auto_choice(probe, *centre)
    record = {"chosen": choice, "at": time.time(), "fingerprint": im.fingerprint(*centre)}
    if choice and choice != im.source_id:
        try:
            await _switch(app, choice, "auto")
            src = im.source(choice)
            store.event("imagery_source", f"Automatic: map imagery is now {src['name']} ({src['native_m'] * 100:g} cm), "
                                          f"the sharpest free source with imagery at the yard")
        except HTTPException as exc:
            record.update(chosen=im.source_id, failed=choice, error=str(exc.detail)[:200])
            log.warning("imagery: automatic could not switch to %s: %s", choice, exc.detail)
    store.set_setting("imagery_auto", record)
    return record


async def _fall_back(app) -> str | None:
    """The source in use has gone (removed, or its key was). Lock held. Switches to automatic and its pick (else the
    first free source here), without failing: returns a warning when the new source could not be fetched yet."""
    im, store = app.state.imagery, app.state.store
    centre = _map_centre(app)
    probe = store.setting("imagery_probe") or {}
    free_here = [i for i in (im.candidates(*centre) if centre else []) if not im.source(i).get("auth")]
    target = (im.auto_choice(probe, *centre) if centre else None) or (free_here[0] if free_here else DEFAULT_SOURCE)
    im.configure({"provider": target, "mode": "auto"}, store.setting("imagery_keys"))
    store.set_setting("imagery_source", {"provider": target, "mode": "auto"})
    try:
        app.state.rec.yard = await im.ensure_yard(strict=True)
        return None
    except ImageryError as exc:
        app.state.rec.yard = im.yard_meta
        return f"now on automatic ({im.source(target)['name'] if any(c['id'] == target for c in im.sources) else target}), " \
               f"but it could not be fetched yet: {exc}"


async def _upkeep_once(app) -> None:
    """One pass of the upkeep: re-sample stale sources (free ones and the one in use), and in automatic mode re-pick
    when that happened, when the free sources here changed, or a day after a failed switch."""
    im, store = app.state.imagery, app.state.store
    centre = im.yard_centre()
    if not centre:
        return
    probe = store.setting("imagery_probe") or {}
    stale = im.stale(probe, *centre, background=True)
    if stale:
        probe = await _check(app, stale)
    async with im.lock:
        auto = store.setting("imagery_auto") or {}
        retry = auto.get("failed") and time.time() - auto.get("at", 0) > 86400
        if im.mode == "auto" and (stale or retry or auto.get("fingerprint") != im.fingerprint(*centre)):
            await _apply_auto(app, probe)


async def imagery_upkeep(app) -> None:
    """Keeps the picker's samples current and, in automatic mode, the sharpest free source in use. Runs 30 s after
    start, whenever the yard's bounds first appear or move, and hourly. Contacts only sources whose outline contains
    the yard."""
    im = app.state.imagery
    await asyncio.sleep(30)
    while True:
        im.wake.clear()
        try:
            await _upkeep_once(app)
        except Exception:
            log.exception("imagery upkeep")
        try:
            await asyncio.wait_for(im.wake.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


def _sources_view(request: Request, **extra) -> dict:
    return {**_imagery_view(request), "mode": request.app.state.imagery.mode, **extra}


@app.post("/api/imagery/probe")
async def imagery_probe(request: Request, body: dict | None = Body(None)):
    """Check again: sample every source that covers the yard (or just `ids`). In automatic mode, re-pick."""
    ids = (body or {}).get("ids")
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)):
        raise HTTPException(400, "ids must be a list of source ids")
    probe = await _check(request.app, ids)
    async with request.app.state.imagery.lock:
        auto = await _apply_auto(request.app, probe)
    return _sources_view(request, probe=probe,
                         warning=f"Automatic could not switch: {auto['error']}" if auto.get("failed") else None)


async def _go_auto(request: Request) -> dict:
    im, store = request.app.state.imagery, rec(request).store
    lon, lat = _yard_centre(request)
    probe = store.setting("imagery_probe") or {}
    if im.stale(probe, lon, lat):
        probe = await _check(request.app, im.stale(probe, lon, lat))
    if not im.auto_choice(probe, lon, lat):
        raise HTTPException(409, "no free source has imagery at the yard; add a key or a source of your own")
    async with im.lock:
        im.mode = "auto"
        store.set_setting("imagery_source", {"provider": im.source_id, "mode": "auto"})
        auto = await _apply_auto(request.app, probe)
    return _sources_view(request, chosen=auto.get("chosen"), probe=probe,
                         warning=f"Automatic could not switch: {auto['error']}" if auto.get("failed") else None)


@app.put("/api/imagery/source")
async def imagery_source(request: Request, body: dict = Body(...)):
    """{"mode": "auto"}: let the panel pick. {"provider": id}: the user's choice, kept until they change it.
    {"provider": "custom", "custom": {...}}: save the custom source and use it."""
    im, store = request.app.state.imagery, rec(request).store
    if body.get("mode") == "auto":
        return await _go_auto(request)
    provider = body.get("provider")
    async with im.lock:
        if provider == "custom" and body.get("custom") is not None:
            # A new custom source: a local catalog entry (DATA_DIR/imagery/sources/custom.json), put back exactly as
            # it was if the new one cannot be used. Without a "custom" body, "custom" is a switch to the saved one.
            custom = body["custom"]
            if not isinstance(custom, dict):
                raise HTTPException(400, "custom needs kind and url")
            previous = im.snapshot_local("custom")
            try:
                im.save_local(custom_entry(custom))
            except ImageryError as exc:
                raise HTTPException(400, f"custom source: {exc}")
            try:
                await _switch(request.app, "custom", "manual")
            except HTTPException:
                im.restore_local("custom", previous)
                raise
        elif provider == im.source_id and im.yard_meta:
            im.mode = "manual"                          # "My choice" on what is already showing: nothing to fetch
            store.set_setting("imagery_source", {"provider": provider, "mode": "manual"})
            return _sources_view(request)
        elif provider in [c["id"] for c in im.sources]:
            await _switch(request.app, provider, "manual")
        else:
            raise HTTPException(400, f"unknown source {provider!r}")
        store.event("imagery_source", f"map imagery switched to {im.source()['name']}")
    lon, lat = _yard_centre(request, required=False)
    if lon is not None and provider in im.stale(store.setting("imagery_probe") or {}, lon, lat):
        await _check(request.app, [provider])          # its card shows a sample of what is now in use
    return _sources_view(request)


@app.delete("/api/imagery/sources/{sid}")
async def imagery_delete_source(request: Request, sid: str):
    """Remove one of the user's own sources (a bundled one it replaced comes back)."""
    im, store = request.app.state.imagery, rec(request).store
    src = next((c for c in im.sources if c["id"] == sid), None)
    if not src or src["origin"] == "bundled":
        raise HTTPException(400, "only sources added on this server can be removed")
    warning = None
    async with im.lock:
        im.delete_local(sid)
        _forget_sample(request.app, sid)
        store.event("imagery_source", f"removed the imagery source {src['name']}")
        if im.source_id == sid and not any(c["id"] == sid for c in im.sources):
            im.forget_yard_from(sid)
            warning = await _fall_back(request.app)
    return _sources_view(request, warning=warning)


@app.put("/api/imagery/key")
async def imagery_key(request: Request, body: dict = Body(...)):
    """Store (or clear, with key: null) a source's API key on this server, then sample the source with it. Keys are
    never sent back."""
    im, store = request.app.state.imagery, rec(request).store
    provider = body.get("provider")
    src = next((c for c in im.sources if c["id"] == provider), None)
    if not src or not src.get("auth"):
        raise HTTPException(400, "that source does not take a key")
    key = (body.get("key") or "").strip()
    out = {"provider": provider, "has_key": bool(key)}
    async with im.lock:
        keys = store.setting("imagery_keys") or {}
        if key:
            keys[provider] = key
        else:
            keys.pop(provider, None)
        store.set_setting("imagery_keys", keys)
        im.set_keys(keys)
        if not key:
            _forget_sample(request.app, provider)          # its sample, and its yard image, were made with the key
            if im.source_id == provider:
                im.forget_yard_from(provider)
                out["warning"] = await _fall_back(request.app)
    lon, lat = _yard_centre(request, required=False)
    if key and lon is not None and provider in im.candidates(lon, lat):
        out["probe"] = (await _check(request.app, [provider])).get(provider)
    return out


# -- adding a source -----------------------------------------------------------------------------------------------------
@app.post("/api/imagery/discover")
async def imagery_discover(request: Request, body: dict = Body(...)):
    """What does this address serve? Asks only that address, with no location; returns a draft entry to review."""
    lat = _yard_centre(request, required=False)[1]          # converts Web Mercator sizes here; never sent
    try:
        return await discover(request.app.state.imagery.http_strict, body.get("url"), lat)
    except ImageryError as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/imagery/try")
async def imagery_try(request: Request, body: dict = Body(...)):
    """A 40 m sample of the yard from an unsaved entry: the first request that carries the yard to that service."""
    lon, lat = _yard_centre(request)
    try:
        return await request.app.state.imagery.try_entry(body.get("entry") or {}, lon, lat)
    except ImageryError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/imagery/sources")
async def imagery_add_source(request: Request, body: dict = Body(...)):
    """Save a source of the user's own (and use it, with "use": true). It is sampled at once for its card."""
    im, store = request.app.state.imagery, rec(request).store
    try:
        src = im.add_local(body.get("entry"), replace=bool(body.get("replace")))
    except ImageryError as exc:
        raise HTTPException(409 if "already" in str(exc) else 400, str(exc))
    store.event("imagery_source", f"added the imagery source {src['name']}")
    if _yard_centre(request, required=False)[0] is not None:
        await _check(request.app, [src["id"]])
    warning = None
    if body.get("use"):
        async with im.lock:
            try:
                await _switch(request.app, src["id"], "manual")
                store.event("imagery_source", f"map imagery switched to {src['name']}")
            except HTTPException as exc:
                warning = f"Saved, but it could not be shown yet: {exc.detail}"
    return _sources_view(request, id=src["id"], warning=warning)


@app.get("/api/imagery/outlines")
async def imagery_outlines(request: Request):
    """The shared coverage outlines an exported source can refer to."""
    return {"outlines": request.app.state.imagery.outlines()}


@app.post("/api/imagery/sources/{sid}/extent")
async def imagery_source_extent(request: Request, sid: str):
    """Where one of the user's own sources says it has imagery: its discovery run again on the saved address (keys
    included, which stay on the server), for the export's "the service's own extent". No location is sent."""
    im = request.app.state.imagery
    src = next((c for c in im.sources if c["id"] == sid), None)
    if not src or src["origin"] == "bundled":
        raise HTTPException(400, "only sources added on this server have an extent to look up")
    key = im.keys.get(sid, "")
    try:
        found = await discover(im.http_strict, src["url"].replace("{key}", key), None)
    except ImageryError as exc:
        raise HTTPException(422, str(exc))
    if src["kind"] == "wms":                     # the saved layer's box, never another layer's
        layer = next((l for l in found["choices"].get("layers", []) if l["name"] == src.get("layers")), {})
        return {"extent": layer.get("bbox")}
    return {"extent": found["extent"]}


@app.post("/api/imagery/sources/{sid}/export")
async def imagery_export(request: Request, sid: str, body: dict = Body(...)):
    """A source of the user's own as catalog files for a pull request, with the yard kept out of them."""
    im = request.app.state.imagery
    src = next((c for c in im.sources if c["id"] == sid), None)
    if not src or src["origin"] == "bundled":
        raise HTTPException(400, "only sources added on this server can be exported")
    lon, lat = _yard_centre(request, required=False)
    try:
        return share.export(src, country=body.get("country"), region=body.get("region"), place=body.get("place"),
                            landmark=body.get("landmark"), coverage=body.get("coverage"),
                            worldwide=bool(body.get("worldwide")), yard=(lon, lat) if lon is not None else None)
    except ImageryError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/imagery/auto")
async def imagery_auto(request: Request):
    """The first panel's "find the sharpest free imagery": now the same as switching to automatic."""
    return await _go_auto(request)


@app.put("/api/imagery/context")
async def imagery_context(request: Request, body: dict = Body(...)):
    """Switch the zoomed-out worldwide layer on or off."""
    im, store = request.app.state.imagery, rec(request).store
    ctx = {"world": bool(body.get("world", True))}
    store.set_setting("imagery_context", ctx)
    im.world_on = ctx["world"]
    return _imagery_view(request)


@app.post("/api/imagery/refresh")
async def imagery_refresh(request: Request):
    im = request.app.state.imagery
    async with im.lock:
        try:
            rec(request).yard = await im.ensure_yard(force=True)
        except ImageryError as exc:
            raise HTTPException(502, str(exc))
    return _imagery_view(request)


@app.get("/imagery/{name}")
async def imagery_file(request: Request, name: str):
    path = request.app.state.imagery.yard_file(name)
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/tiles/world/{z}/{x}/{y}.jpg")
async def world_tile(request: Request, z: int, x: int, y: int):
    data = await request.app.state.imagery.world_tile(z, x, y)
    if data is None:
        return Response(status_code=204)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=604800"})


@app.get("/tiles/src/{z}/{x}/{y}.jpg")
async def source_tile(request: Request, z: int, x: int, y: int):
    """?v= (the source's version) only busts the browser cache when the source changes."""
    data = await request.app.state.imagery.source_tile(z, x, y)
    if data is None:
        return Response(status_code=204)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/", include_in_schema=False)
async def index():
    """index.html with every script and stylesheet stamped with the UI's version, so a redeploy beats any cache."""
    return HTMLResponse(stamp((WEB / "index.html").read_text(encoding="utf-8")), headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
