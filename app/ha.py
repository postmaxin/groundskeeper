"""Optional: publish the panel's view of the robot to Home Assistant as entities (spec §17).

Enabled only when an address and a long-lived access token are set (Settings → Home Assistant, or HA_URL and HA_TOKEN). Uses HA's REST state API, so the entities
are re-created on every push; they need no HA-side configuration.
"""
import asyncio
import hashlib
import logging
import re

from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)
PREFIX = "yarbo_panel"


class HAPublisher:
    def __init__(self, url: str | None, token: str | None, interval_s: float = 60):
        self.interval_s = interval_s
        self._client = None
        self._set(url, token)

    def _set(self, url: str | None, token: str | None) -> None:
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.enabled = bool(self.url and self.token)
        self.last_ok: float | None = None
        self.error = "" if self.enabled else "not configured (Settings → Home Assistant)"
        self._client = httpx.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {self.token}"}) \
            if self.enabled else None

    async def configure(self, url: str | None, token: str | None) -> None:
        await self.close()
        self._set(url, token)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def push(self, entities: dict[str, dict]) -> None:
        if not self.enabled:
            return
        refused = []
        try:
            for entity_id, body in entities.items():
                if body.get("state") is None:       # no value yet (e.g. no battery reading right after a restart):
                    body = {**body, "state": "unknown"}      # HA refuses a null state with 400
                r = await self._client.post(f"{self.url}/api/states/{entity_id}", json=body)
                if r.status_code >= 400:            # one entity refused: say so, and still send the others
                    refused.append(f"{entity_id} ({r.status_code})")
            msg = f"Home Assistant refused {', '.join(refused)}" if refused else ""
            if not refused:
                self.last_ok = asyncio.get_running_loop().time()
        except Exception as exc:                    # the connection itself: every entity would fail the same way
            msg = net_error(exc, self.url)
        if msg and msg != self.error:
            log.warning("HA push failed: %s", msg)
        self.error = msg

    def status(self) -> dict:
        return {"enabled": self.enabled, "url": self.url or None, "error": self.error}


def net_error(exc: Exception, url: str | None = None) -> str:
    """A failed connection in words a user can act on (httpx timeouts carry no message of their own)."""
    parts = urlsplit(url or "")
    host = parts.hostname or "the address"
    example = f"{parts.scheme or 'http'}://192.0.2.20{f':{parts.port}' if parts.port else ''}"
    text = str(exc)
    unresolved = ("Name or service not known", "nodename nor servname", "getaddrinfo failed", "Temporary failure in name resolution")
    if any(m in text for m in unresolved):
        local = host.endswith(".local")
        return (f"this server cannot find {host}" + (": .local names (mDNS) usually do not resolve inside Docker, "
                f"so use the IP address instead, like {example}" if local else ": check the spelling, or use the IP address"))
    if isinstance(exc, httpx.TimeoutException):
        return f"no answer from {host} in time (wrong address or port, or a firewall)"
    if isinstance(exc, httpx.ConnectError):
        return f"{host} refused the connection{': ' + text if text else ''} (is the port right?)"
    return text or type(exc).__name__


def entities_from(status: dict, conditions: dict | None, last_run: dict | None) -> dict[str, dict]:
    r = status["robot"]
    out = {
        f"sensor.{PREFIX}_state": {"state": status["state"],
                                   "attributes": {"friendly_name": "Yarbo state", "icon": "mdi:robot-mower",
                                                  "head": r["head"], "error_code": r["error_code"],
                                                  "pause_reason": status.get("pause_reason"),
                                                  "plan_error": status.get("plan_error")}},
        f"sensor.{PREFIX}_battery": {"state": r["battery"],
                                     "attributes": {"friendly_name": "Yarbo battery", "unit_of_measurement": "%",
                                                    "device_class": "battery", "state_class": "measurement"}},
        f"sensor.{PREFIX}_rtk": {"state": r["rtk"]["label"],
                                 "attributes": {"friendly_name": "Yarbo positioning", "icon": "mdi:crosshairs-gps",
                                                "satellites": r["rtk"]["sats"]}},
        f"binary_sensor.{PREFIX}_running": {"state": "on" if status.get("run") else "off",
                                            "attributes": {"friendly_name": "Yarbo run in progress",
                                                           "icon": "mdi:play-circle"}},
        # The entity keeps its old id (local_link) so existing dashboards and automations still find it; it says
        # whether the robot itself answered lately (through the Yarbo cloud), as /health's robot_connected does.
        f"sensor.{PREFIX}_local_link": {"state": "connected" if status["transport"]["robot"].get("heard") else "down",
                                        "attributes": {"friendly_name": "Groundskeeper link", "icon": "mdi:lan"}},
    }
    if conditions:
        out[f"sensor.{PREFIX}_lawn"] = {
            "state": conditions["state"],
            "attributes": {"friendly_name": "Lawn conditions", "icon": "mdi:grass", "dry_by": conditions.get("dry_by"),
                           "rules": [f"{x['rule']}: {x['state']} ({x['detail']})" for x in conditions["rules"]]}}
    if last_run:
        out[f"sensor.{PREFIX}_last_run"] = {
            "state": last_run.get("end_reason") or "running",
            "attributes": {"friendly_name": "Yarbo last run", "icon": "mdi:history", "started": last_run.get("started"),
                           "ended": last_run.get("ended"), "distance_m": last_run.get("distance_m"),
                           "rtk_fixed_pct": last_run.get("rtk_fixed_pct")}}
    return out


# -- summary for a Home Assistant integration (GET /api/ha/summary, #12) -----------------------------------------
# One small read that an integration polls with an API token. It carries no position, map or camera data. Bump
# SUMMARY_API when a field changes meaning or goes away; adding a field does not need a bump.
SUMMARY_API = 1


def next_window(upcoming: list[dict]) -> dict | None:
    for u in upcoming or []:
        if u.get("active") or u.get("next"):
            w = u.get("window") or {}
            return {"id": w.get("id"), "name": w.get("name"), "plan_id": w.get("plan_id"), "next": u.get("next"),
                    "active": bool(u.get("active")), "auto_start": bool(w.get("auto_start"))}
    return None


def robot_id(serial: str | None) -> str | None:
    """A stable name for the robot that is not its serial number, so the serial never reaches Home Assistant."""
    return hashlib.sha256(serial.encode()).hexdigest()[:16] if serial else None


def summary(status: dict, ops_status: dict, last_run: dict | None, upcoming: list[dict], plans: list[dict],
            start: dict | None, version: dict) -> dict:
    """What an integration shows: state, battery, positioning, lawn, last run, the next schedule window, and whether
    a start would go out now and if not why (start = the controller's preview of start_plan, or None)."""
    r, run = status["robot"], status.get("run")
    nxt = next_window(upcoming)
    plan_names = {p.get("id"): p.get("name") for p in plans or []}
    if nxt:
        nxt["plan_name"] = plan_names.get(nxt["plan_id"])
    can_start = None
    if start is not None:
        can_start = {"plan_id": start.get("plan_id"), "plan_name": plan_names.get(start.get("plan_id")),
                     "would_send": bool(start.get("would_send")), "reason": start.get("reason") or None,
                     "why": [{"code": w["code"], "title": w["title"], "fix": w["fix"]} for w in start.get("why") or []]}
    held = (ops_status.get("why") or {}).get("held")
    lr = None
    if last_run:
        lr = {k: last_run.get(k) for k in ("id", "started", "ended", "end_reason", "distance_m", "rtk_fixed_pct")}
    return {
        "api": SUMMARY_API, **version, "robot_id": robot_id(status.get("sn")),
        "state": status["state"], "activity": status.get("activity"), "error_code": r["error_code"],
        "pause_reason": status.get("pause_reason"), "plan_error": status.get("plan_error"),
        "held": {"code": held["code"], "title": held["title"]} if held else None,
        "battery": r["battery"], "charging": r.get("charging_status"), "head": r["head"],
        "rtk": {"label": r["rtk"]["label"], "sats": r["rtk"]["sats"]},
        "running": bool(run),
        "run": {"id": run["id"], "started": run["started"], "distance_m": run["distance_m"]} if run else None,
        "local_link": bool(status["transport"]["robot"].get("heard")),   # old name kept for the HA integration
        "cloud": (ops_status.get("cloud") or {}).get("state"),
        "control_enabled": bool(ops_status.get("control_enabled")),
        "lawn": status.get("conditions"),
        "last_run": lr,
        "next_window": nxt,
        "pending_confirm": bool(ops_status.get("pending")),
        "can_start": can_start,
        "snapshot_age_s": status.get("snapshot_age_s"),
        # The panel's own stuck check (never the robot's StateMSG.stuck), as on Live and in the alerts.
        "stalled": bool(status.get("stalled")), "stalled_s": status.get("stalled_s") or 0,
        "rtk_wait": bool(status.get("rtk_wait")), "lost_contact": bool(status.get("lost_contact")),
    }


# -- presence -----------------------------------------------------------------------------------------------------
# "Is anyone home?" from one HA entity. An alarm panel (Alarmo) armed away or on vacation means nobody; any other
# armed state or disarmed means someone. A person, device tracker or group of them: home means someone, anything
# else (not_home, a zone name) means nobody. zone.home counts the people in it.
PRESENCE_DOMAINS = {"alarm_control_panel": (0, "alarm panel: armed away = nobody home"),
                    "zone": (1, "people in the zone"), "group": (2, "group: home / not home"),
                    "person": (3, "one person: home / not home"), "device_tracker": (4, "one device: home / not home")}


def presence_from(state: dict | None) -> bool | None:
    """True if someone is home, False if nobody, None when the entity cannot say."""
    if not state:
        return None
    ent, st = str(state.get("entity_id") or ""), state.get("state")
    if st in (None, "unavailable", "unknown", ""):
        return None
    domain = ent.split(".", 1)[0]
    if domain == "zone":
        try:
            return float(st) > 0
        except (TypeError, ValueError):
            return None
    if domain in ("person", "device_tracker", "group"):
        if st in ("home", "on"):
            return True
        return False if domain != "group" or st in ("not_home", "off") else None
    return st not in ("armed_away", "armed_vacation")         # an alarm panel, as before (and anything unknown)


def presence_candidates(states: list[dict]) -> list[dict]:
    """The entities that can feed presence, alarm panels first. A group only when it holds people or trackers."""
    out = []
    for st in states or []:
        ent = str(st.get("entity_id") or "")
        domain = ent.split(".", 1)[0]
        if domain not in PRESENCE_DOMAINS or st.get("state") in ("unavailable", "unknown"):
            continue
        attrs = st.get("attributes") or {}
        if domain == "zone" and ent != "zone.home":
            continue
        if domain == "group" and not any(str(m).split(".", 1)[0] in ("person", "device_tracker")
                                         for m in attrs.get("entity_id") or []):
            continue
        rank, why = PRESENCE_DOMAINS[domain]
        name = str(attrs.get("friendly_name") or ent)
        out.append({"entity": ent, "name": name, "value": str(st.get("state")), "why": why,
                    "likely": domain in ("alarm_control_panel", "zone"), "_rank": (rank, name.lower())})
    out.sort(key=lambda x: x.pop("_rank"))
    return out


def notify_candidates(services: list[dict]) -> list[dict]:
    """HA's notify services (GET /api/services) as choices for the panel's alerts: the companion apps' phone services
    (notify.mobile_app_*) first and marked likely, then the rest. `notify.notify` and `notify.persistent_notification`
    are kept, but after the phones: they reach every device, or only HA's own sidebar."""
    out = []
    for dom in services or []:
        if not isinstance(dom, dict) or dom.get("domain") != "notify":
            continue
        for name, info in (dom.get("services") or {}).items():
            if not re.fullmatch(r"[a-z0-9_]{1,64}", str(name)) or name == "send_message":
                continue  # send_message is the entity-based action; it needs an entity_id the panel does not send
            info = info if isinstance(info, dict) else {}
            phone = name.startswith("mobile_app_")
            label = str(info.get("name") or "").strip()
            if name == "notify":
                label = "Every notifier"
            elif name == "persistent_notification":
                label = "Home Assistant's notifications panel"
            elif not label or label.lower().startswith("send a notification"):
                label = name.removeprefix("mobile_app_").replace("_", " ").strip().title() if phone else name.replace("_", " ")
            why = "phone (companion app)" if phone else "every notifier" if name == "notify" else                   "HA's notifications panel" if name == "persistent_notification" else "notifier"
            out.append({"service": f"notify.{name}", "name": label, "why": why, "likely": phone,
                        "_rank": (0 if phone else 2 if name in ("notify", "persistent_notification") else 1, name)})
    out.sort(key=lambda x: x.pop("_rank"))
    return out

