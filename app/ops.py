"""Phase 2 orchestration: cloud session, control, gates, during-run guards, the scheduler, alerts, maintenance and
the follow-cam. Kept out of the recorder so recording stays simple and always on."""
import asyncio
import logging
import time
from datetime import datetime

from . import conditions, control, maintenance, telemetry
from . import why as reasons
from .cloud import CloudTransport
from .link import RobotLink
from .control import Controller
from .backups import Backups
from .mapwrite import BOOKKEEPING, MapWriter
from .network import Network
from .frigate import Frigate
from .ha import presence_from
from .notify import Notifier
from .runs import PROGRESSING
from .runs import parse_plan_feedback
from .scheduler import Scheduler

log = logging.getLogger(__name__)
PHONE_APP_COMMANDS = {"set_working_state", "cmd_vel", "smart_vision_control"}
GUARD_GAP_S = 60          # at least this long between two guard RESUME attempts (pause and dock are never held back)
RESUME_BACKOFF_S = 300    # after the resume checks fail, wait this long before looking again
GUARD_RETRY_S = 20        # a protective command that was refused or not confirmed is sent again after this (doubling)
GUARD_RETRY_MAX_S = 160
GUARD_HYST_S = 30         # a condition must stay gone this long before the guard counts it as cleared (Frigate flaps)
GUARD_CARRY_S = 300       # after a restart, a detection seen this recently in the same run is the same one (no new notice)
GUARD_SAVE_S = 30         # an active detection is written to the database at most this often
RESUME_MAX_S = 3600       # a guard pause older than this is left for a person to resume
STUCK_CLEAR_M = 3.0       # a stuck alert clears once the robot is this far from where it got stuck
# Activities where there is nothing for a guard to pause (or send home): the robot is already stopped or heading in.
NOT_PAUSABLE = {"paused", "charging_break", "returning", "docking", "docked", "idle"}
NOT_DOCKABLE = {"charging_break", "returning", "docking", "docked"}
BUSY = {"mowing", "transit", "planning", "moving"}
# How each guard is named in Settings → Run rules → During a run, for its notices (#54).
GUARD_RULES = {"person": "person guard", "rain": "rain guard", "away": "everyone-left guard", "dark": "after-dark guard"}
AFTER_CHARGE_S = 120      # the robot counts as leaving its charger if it was charging mid-plan this recently (it can
                          # pass through idle for a moment on its way out)


def point_in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


class Ops:
    def __init__(self, cfg, rec):
        self.cfg, self.rec, self.store = cfg, rec, rec.store
        self.cloud = CloudTransport(cfg.data_dir, rec.sn, self._from_cloud)
        # Local first, the cloud as a fallback, for reads and commands alike (app/link.py).
        self.link = RobotLink(rec, self.cloud, self.store, mark_sent=rec.mark_sent)
        self.link.on_change = self._route_changed
        rec.link = self.link
        self.frigate = Frigate(cfg.frigate_url)
        self.notifier = Notifier(self.store, cfg.ha_url, cfg.ha_token)
        rec.on_sn = self.robot_changed
        self.controller = Controller(self.store, self.link, self.context, self.expect_reply, lambda: self.rec.t)
        # Another client's command, other than the one being watched (whose late echo may look like someone else's).
        self.controller.others_at = lambda cmd: max(
            (t for name, t in (getattr(self.rec, "foreign_cmds", None) or {}).items() if name != cmd), default=0.0)
        self.scheduler = Scheduler(self.store)
        self.map_writer = MapWriter(self.store, self.link.write_command,
                                    self.expect_reply, rec.reload_map,
                                    rec.read_pathway_params, lambda: self.cloud.username, rec.read_area_records,
                                    rec.read_deadend_params)
        self.backups = Backups(self.store, self.map_writer,
                               lambda cmd, payload: (rec.request_area_params(payload["id"]) if cmd == "read_mower_area_params"
                                                     else rec.request(cmd, payload=payload)),
                               rec.reload_map, rec.read_area_records)
        self.network = Network(self.store, rec, self.map_writer)
        # A start and a write never overlap (#57, #69): each side asks the other right before it sends.
        self.controller.writing = self.map_writer.lock.locked
        self.map_writer.start_block = self.write_hold
        self.phone_app_at = 0.0
        self._follow, self._follow_next = None, (None, 0.0)  # follow-cam: shown, and a new pick and since when
        self.presence_cache = (0.0, None)
        # Guard conditions during a plan: key -> {active, since, clear_since, why, acted, acted_at, tries}
        self.guards: dict[str, dict] = {}
        # The after-dark guard (_left_charger_after_dark): last seen charging mid-plan, when it left, which run is out
        self._charged_at, self._charged_saved, self._left_at, self._night_out, self._dark_kept = 0.0, 0.0, None, None, None
        self._guard_saved: dict[str, float] = {}     # when each active detection was last written (guard_detections)
        self._guard_boot: dict | None = None         # what the previous process kept (read once, on first use)
        self._guard_cmd_at = 0.0          # last guard resume attempt
        self._resume: dict | None = None  # a guard resume waiting for the robot to confirm it
        self._last_error = 0
        self._cloud_alerted = False
        self._head = None
        self._stuck_at: tuple | None = None   # (x, y) where the stuck alert fired
        self._lost_since: float | None = None
        self._rtk_wait = False                # told that the robot is standing still waiting for an RTK fix
        self._codes: dict = {}                # last plan / recharge error code reported, so each alerts once
        self._pause_code = None               # the robot's own pause reason last seen, so each pause alerts once

    # -- plumbing ----------------------------------------------------------------------------------------------
    def _from_cloud(self, topic: str, data, ts: float) -> None:
        """Cloud messages join the recorder's queue (so DeviceMSG and replies are handled once, in one place)
        and also feed phone-app detection."""
        parts = topic.split("/", 3)
        if len(parts) == 4 and parts[2] == "app" and parts[3] in PHONE_APP_COMMANDS:
            if not self.rec.own_echo(parts[3], ts, data):
                self.phone_app_at = ts
        self.rec._from_thread(topic, data, ts)

    def _route_changed(self, what: str, before: str, after: str) -> None:
        """A route to the robot changed (local <-> cloud): say so once, so a firmware lockout or a 4G spell shows."""
        self.store.event("route_changed", f"{what} now go through the {after} (were {before})")

    def phone_app_active(self, quiet: float) -> bool | None:
        """Is the Yarbo app open on someone's phone? The robot streams DeviceMSG about once a second while the app is
        connected (seen on the local broker, 2026-10-21; the panel's own reads come back on data_feedback), and the app's
        own keep-awake shows on the cloud. A stream is proof the app is open; its absence is not yet proof it is closed
        (not shown for every kind of app session), so without the cloud "no stream" is unknown, which still blocks the
        starts nobody watches (scheduler, assistant, guard resume). Right after the panel starts or a route comes up,
        quiet is unknown until the route has been up for the quiet window."""
        now = time.time()
        streaming = now - self.rec.app_stream_at < quiet
        if self.cloud.state == "connected" and now - getattr(self.cloud, "connected_since", 0.0) >= quiet:
            return streaming or now - self.phone_app_at < quiet
        return True if streaming else None

    def phone_app_seen_at(self) -> float:
        """The last sign of the phone app on either route (its keep-awake on the cloud, its stream locally)."""
        return max(self.phone_app_at, self.rec.app_stream_at)

    def expect_reply(self, cmd: str) -> asyncio.Future:
        fut = self.rec.loop.create_future()
        waiters = self.rec._pending.setdefault(cmd, [])
        waiters[:] = [f for f in waiters if not f.done()]   # run commands rarely get a reply: prune old waiters
        waiters.append(fut)
        self.rec._sent[cmd] = time.time()
        return fut

    async def start(self) -> None:
        if getattr(self.cfg, "demo", False):
            pass                                     # DEMO=1 never uses a cloud session, even one left in DATA_DIR
        elif await asyncio.to_thread(self.cloud.restore):
            self.cloud.start()
        if self.frigate.enabled:
            await self.frigate.cameras()  # prime the list so status() reports a real count
        await asyncio.gather(self._scheduler_loop(), self._guard_loop(), self._alert_loop())

    async def close(self) -> None:
        tasks = list(self.controller._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.cloud.stop()
        await self.frigate.close()
        await self.notifier.close()

    # -- settings changes (Settings → integrations) -----------------------------------------------------------
    def robot_changed(self, sn: str) -> None:
        """The recorder found or was given another serial: the cloud session must subscribe to that robot."""
        if self.cloud.sn == sn:
            return
        self.cloud.sn = sn
        if self.cloud._thread and self.cloud._thread.is_alive():
            asyncio.get_running_loop().run_in_executor(None, self.cloud.restart)

    async def apply_frigate(self) -> None:
        await self.frigate.configure(self.cfg.frigate_url)

    async def apply_home_assistant(self) -> None:
        await self.rec.ha.configure(self.cfg.ha_url, self.cfg.ha_token)
        self.notifier.configure(self.cfg.ha_url, self.cfg.ha_token)

    # -- context for interlocks --------------------------------------------------------------------------------
    async def presence(self) -> bool | None:
        """True if someone is home per the configured HA entity (Alarmo), False if armed away, None if unknown."""
        entity = self.store.setting("presence_entity")
        ha = self.rec.ha
        if not (entity and ha.enabled):
            return None
        at, value = self.presence_cache
        if time.time() - at < 30:
            return value
        try:
            r = await ha._client.get(f"{ha.url}/api/states/{entity}")
            r.raise_for_status()
            value = presence_from({"entity_id": entity, **r.json()})
        except Exception as exc:
            log.warning("presence lookup failed: %s", exc)
            value = None
        self.presence_cache = (time.time(), value)
        return value

    def yard_cameras(self) -> list[str]:
        return self.store.setting("yard_cameras", []) or []

    async def yard_status(self, minutes: float, labels) -> dict:
        if not self.frigate.enabled or not self.yard_cameras():
            return {"ok": None, "detail": "Frigate not set up" if not self.frigate.enabled else "no yard cameras chosen",
                    "events": []}
        d = await self.frigate.detections(self.yard_cameras(), labels, minutes * 60)
        if not d["ok"]:
            return {"ok": None, "detail": d.get("reason", "Frigate unreachable"), "events": []}
        ev = d["events"]
        if not ev:
            return {"ok": True, "detail": f"nothing in the last {minutes:g} min", "events": []}
        names = ", ".join(sorted({f"{e['label']} on {e['camera']}" for e in ev}))
        return {"ok": False, "detail": names, "events": ev}

    def write_hold(self, cmd: str) -> str | None:
        """The writer asks this at a write's first send (#69): a start still settling holds every write back, and
        anything but backup bookkeeping needs the plan state known to be idle. Fails closed."""
        why = self.controller.start_settling()
        if why or cmd in BOOKKEEPING:
            return why
        active = self.rec.plan_active()
        # The robot's planning flag too: at a plan's start a "no plan working" reply can still be newer than it.
        # (Not planning_paused: it can outlive a plan; Map Studio's Idle row reads the same flag.)
        if self.rec.run_id or active or (self.rec.t.get("planning") or 0) > 0:
            return "a plan is running; nothing was sent"
        if active is None:
            return "the robot's plan state is unknown; nothing was sent"
        return None

    async def context(self, args: dict) -> dict:
        r = self.rec
        g = control.gates(self.store)
        t = r.t
        snap_age = round(time.time() - r.snapshot_at, 1) if r.snapshot_at else None
        quiet = g["phone_app_quiet_s"]
        return {
            "snapshot_age_s": snap_age,
            "route_state": self.link.state,
            "plans": r.plans,
            "error_code": int(t.get("error_code") or 0),
            "battery": t.get("battery"),
            "running": bool(r.run_id) or bool(r.plan_active()),
            "yard": await self.yard_status(args.get("yard_minutes") or g["yard_clear_minutes"], g["yard_labels"]),
            "phone_app_active": self.phone_app_active(quiet),
            "conditions": (r.conditions or {}).get("state"),
            "presence": await self.presence(),
            "writing": self.map_writer.lock.locked() if getattr(self, "map_writer", None) else None,
        }

    # -- scheduler ---------------------------------------------------------------------------------------------
    async def _evaluate_window(self, w: dict):
        if w.get("plan_id") is None:
            return False, [], "no plan chosen for this window"
        ctx = await self.context({"plan_id": w["plan_id"]})
        checks = control.check("start_plan", {"plan_id": w["plan_id"]}, ctx, control.gates(self.store), "scheduler")
        # Raising a confirmation does not need a route to the robot or to know about the phone app (confirming does);
        # keep those checks visible but soft.
        # Only for a window that asks first: an auto-start window sends at once, and the send keeps them hard.
        if not w.get("auto_start"):
            for c in checks:
                if c["name"] == "Route to the robot" or (c["name"] == "Phone app not driving it" and c["ok"] is None):
                    c["hard"] = False
                    c["ok"] = c["ok"] or None
        ok, reason = control.verdict(checks, override=False)
        return ok, checks, reason

    def plan_name(self, plan_id) -> str:
        return next((p.get("name") for p in self.rec.plans if p.get("id") == plan_id), f"plan {plan_id}")

    def camera_for_plan(self, plan_id) -> str | None:
        polys = self.store.setting("camera_polygons", {}) or {}
        areas = next((p.get("areaIds") for p in self.rec.plans if p.get("id") == plan_id), []) or []
        feats = (self.rec.map_geojson or {}).get("features", [])
        for f in feats:
            if f["properties"].get("kind") == "areas" and f["properties"].get("id") in areas:
                ring = f["geometry"]["coordinates"][0]
                lon = sum(c[0] for c in ring) / len(ring)
                lat = sum(c[1] for c in ring) / len(ring)
                for cam, poly in polys.items():
                    if point_in_ring(lon, lat, poly):
                        return cam
        cams = self.yard_cameras()
        return cams[0] if cams else None

    async def _raise_pending(self, w: dict, checks: list) -> None:
        date = datetime.now().strftime("%Y-%m-%d")
        cam = self.camera_for_plan(w["plan_id"])
        pending = {"window_id": w["id"], "date": date, "plan_id": w["plan_id"], "plan": self.plan_name(w["plan_id"]),
                   "window": w.get("name") or f"{w['start']}-{w['end']}", "created": time.time(), "camera": cam,
                   "checks": checks}
        self.store.set_setting("schedule_pending", pending)
        self.store.log_schedule(w["id"], date, "pending", {"plan": pending["plan"]})
        base = self.cfg.panel_url
        await self.notifier.send(
            "start_pending", f"Ready to start {pending['plan']}",
            "All gates pass. Open the panel to confirm (expires in 30 min)."
            + ("" if self.controller.enabled() else " Control is switched off, so confirming will not start it yet."),
            url=f"{base}/#live" if base else None,
            image=self._signed_snapshot(base, cam))

    def _signed_snapshot(self, base: str | None, cam: str | None) -> str | None:
        """The notification's camera image: the phone fetches it with no session, so the link is signed and expires
        within the hour (app/auth.py sign_path)."""
        if not base or not cam:
            return None
        from .auth import Auth
        return base + Auth(self.store).sign_path(f"/api/frigate/snapshot/{cam}.jpg")

    async def _auto_start(self, w: dict) -> None:
        date = datetime.now().strftime("%Y-%m-%d")
        result = await self.controller.send("start_plan", {"plan_id": w["plan_id"]}, actor="scheduler")
        self.store.log_schedule(w["id"], date, "started" if result.get("sent") else "failed",
                                {"result": result.get("refused") or result.get("error") or result.get("msg")})
        await self.notifier.send("auto_start", f"{'Started' if result.get('sent') else 'Could not start'} "
                                 f"{self.plan_name(w['plan_id'])}",
                                 reasons.lines(result.get("why") or []) or str(result.get("refused") or result.get("msg") or ""))

    async def _skipped(self, w: dict, why: list[dict], reason: str = "") -> None:
        if any(r["code"] == "wrong_head" for r in why):
            return                                   # a mower window through the snow season: not news every day
        base = self.cfg.panel_url
        await self.notifier.send("skipped", f"Skipped {w.get('name') or self.plan_name(w.get('plan_id'))} today",
                                 reasons.lines(why) or (reason if reason and reason != "ok" else
                                                        "another start was waiting for confirmation"),
                                 url=f"{base}/#schedule" if base else None)

    async def confirm_pending(self) -> dict:
        p = self.scheduler.pending()
        if not p:
            return {"ok": False, "error": "nothing is waiting for confirmation"}
        result = await self.controller.send("start_plan", {"plan_id": p["plan_id"]}, actor="user", wait="background")
        self.store.log_schedule(p["window_id"], p["date"], "confirmed" if result.get("sent") else "failed",
                                {"result": result.get("refused") or result.get("error") or result.get("msg")})
        self.scheduler.clear_pending()
        return result

    def dismiss_pending(self) -> dict:
        p = self.scheduler.pending()
        if p:
            self.store.log_schedule(p["window_id"], p["date"], "dismissed", {})
            self.scheduler.clear_pending()
        return {"ok": True}

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self.scheduler.tick(datetime.now(), self.rec.t.get("head_type"), self._evaluate_window,
                                          self._raise_pending, self._auto_start, self._skipped)
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(60)

    # -- guards during a run -----------------------------------------------------------------------------------
    # Each guard watches one condition (someone in the yard, rain, everyone left) and is LEVEL-triggered: while the
    # condition holds and the robot is still working, it keeps sending its pause/dock (every GUARD_RETRY_S) until the
    # robot's state confirms it. A condition counts as cleared only after GUARD_HYST_S without it.
    # Auto-resume (opt-in, gate guard_resume) only undoes a pause the guard itself sent AND saw confirmed, only while
    # the robot has sat paused ever since (no state change, no command from anyone else, same plan session per a fresh
    # plan reply), only within RESUME_MAX_S, and only through the controller's guard-resume interlocks (yard clear on
    # the cameras, someone home, lawn dry, phone app quiet - unknown fails). An unconfirmed resume is not retried.
    # Docking is never undone automatically.
    async def _guard_conditions(self, g: dict) -> dict[str, tuple[bool | None, str]]:
        """key -> (active: True / False / None = unknown, why)."""
        out = {}
        if g["guard_person"] != "ignore":
            yard = await self.yard_status(1, g["yard_labels"])
            live = [e for e in yard["events"] if e.get("in_progress")]
            out["person"] = (bool(live) if yard["ok"] is not None else None,
                             f"{live[0]['label']} on {live[0]['camera']}" if live else "someone in the yard")
        if g["guard_rain"] != "ignore":
            raining = (self.rec.conditions or {}).get("raining")
            out["rain"] = (None if self.rec.conditions is None else bool(raining), "rain")
        if g["guard_everyone_left"] != "ignore":
            present = await self.presence()
            out["away"] = (None if present is None else present is False, "everyone left (Alarmo armed away)")
        if g.get("guard_after_dark", "ignore") != "ignore":
            out["dark"] = (self._left_charger_after_dark(), "it left the charger after dark")
        return out

    def _left_charger_after_dark(self) -> bool:
        """A plan that stopped to charge heads back out by itself when the battery is full, at any hour (seen on a
        real mow at 20:36 in October). True from the moment it leaves the charger mid-plan while it is dark at the
        yard, until sunrise, so a second try the same night is sent home too. Not when a person sent it out (their
        command came around the departure), not when it left before sunset, not without the yard's location. The
        armed state and the last charging time are kept in the database, so a panel restart mid-departure keeps it."""
        r, now = self.rec, time.time()
        ref = getattr(r, "gps_ref", None)
        if self._night_out is None and self._dark_kept is None:
            self._dark_kept = self.store.setting("guard_after_dark_state") or {}
            if self._dark_kept.get("run") == r.run_id and now < (self._dark_kept.get("until") or 0):
                self._night_out = r.run_id
            self._charged_at = max(self._charged_at, float(self._dark_kept.get("charged_at") or 0))
        if r.activity == "charging_break":
            self._charged_at, self._left_at = now, None
            if now - self._charged_saved >= 60:
                self._charged_saved = now
                self._keep_dark(charged_at=now)
        elif (r.activity in BUSY and self._left_at is None and 0 < now - self._charged_at < AFTER_CHARGE_S):
            self._left_at = now
            person = self._people_cmd_at() >= self._charged_at - AFTER_CHARGE_S
            if ref and conditions.is_dark(now, *ref) and not person:
                self._night_out = r.run_id
                self._keep_dark(run=r.run_id, until=conditions.next_sunrise_ts(now, *ref) or now + 12 * 3600)
        dark = conditions.is_dark(now, *ref) if ref else None
        if not dark and self._night_out is not None:
            self._night_out = None
            self._keep_dark(run=None, until=None)
        return bool(dark) and self._night_out is not None and self._night_out == r.run_id

    def _keep_dark(self, **change) -> None:
        kept = {**(self.store.setting("guard_after_dark_state") or {}), **change}
        self.store.set_setting("guard_after_dark_state", kept)
        self._dark_kept = kept

    @staticmethod
    def _new_guard() -> dict:
        return {"active": False, "seen_at": 0.0, "since": None, "clear_since": None, "why": "", "notified": False,
                "warned": False, "pending": None, "tried_at": 0.0, "tries": 0, "acted": None, "acted_at": 0.0,
                "owner": None, "snoozed": False, "held": False, "held_at": None}

    def _people_cmd_at(self) -> float:
        """Last time a person told the robot something: this panel (not the guard), the phone app, another client."""
        return max(self.controller.last_human_cmd_at, getattr(self.rec, "foreign_cmd_at", 0) or 0)

    def _drop_ownership(self, why: str) -> None:
        held = [k for k, st in self.guards.items() if st["acted"] == "pause"]
        for k in held:
            self.guards[k].update(acted=None, owner=None)
        if held:
            self.store.event("guard_released", f"guard pause handed back to people: {why}")

    async def _guard_tick(self) -> None:
        r = self.rec
        if not (r.run_id or r.plan_active()):
            self.guards.clear()
            self._resume = None
            return
        if not self.controller.enabled():
            self._drop_ownership("control switched off")
        g = control.gates(self.store)
        # After dark, a pause would leave the robot on the lawn all night: only dock, notify or ignore apply.
        dark = g.get("guard_after_dark", "ignore")
        actions = {"person": g["guard_person"], "rain": g["guard_rain"], "away": g["guard_everyone_left"],
                   "dark": "dock" if dark == "pause" else dark}
        conds = await self._guard_conditions(g)
        now = time.time()
        for key in [k for k in self.guards if k not in conds]:      # switched to "ignore" since
            self.guards.pop(key)
        for key, (active, why) in conds.items():
            st = self.guards.setdefault(key, self._new_guard())
            if active:
                st.update(seen_at=now, why=why)
            on = bool(active) or (st["active"] and (active is None or now - st["seen_at"] < GUARD_HYST_S))
            if on and not st["active"]:
                st.update(active=True, since=now, clear_since=None, notified=self._guard_carried(key, now),
                          warned=False, snoozed=False, tries=0, held=False, held_at=None)
            elif not on and st["active"]:
                st.update(active=False, clear_since=now)
            if st["active"]:
                self._guard_remember(key, now)
            if st["active"]:
                await self._guard_hold(key, st, actions[key])
        await self._guard_confirmations()
        await self._guard_docked(g)
        await self._guard_resume(g)

    def _guard_carried(self, key: str, now: float) -> bool:
        """Is this the same detection as one the previous process already notified (same run, seen within
        GUARD_CARRY_S)? Then it carries on quietly; the guard state itself was in memory and starts again. Only what
        was kept when this process started counts, and each key once: a person who leaves and comes back later in
        the same run is a new detection and is notified."""
        if self._guard_boot is None:
            self._guard_boot = self.store.setting("guard_detections") or {}
        seen = (self._guard_boot.get("keys") or {}).pop(key, None)
        run = self.rec.run_id
        return run is not None and self._guard_boot.get("run") == run and seen is not None and now - seen < GUARD_CARRY_S

    def _guard_remember(self, key: str, now: float) -> None:
        """Keep an active detection in the database (every GUARD_SAVE_S), for _guard_carried after a restart."""
        last = self._guard_saved.get(key, 0.0)
        if now - last < GUARD_SAVE_S:
            return
        self._guard_saved[key] = now
        kept = self.store.setting("guard_detections") or {}
        if kept.get("run") != self.rec.run_id:
            kept = {"run": self.rec.run_id, "keys": {}}
        kept.setdefault("keys", {})[key] = now
        self.store.set_setting("guard_detections", kept)

    async def _guard_hold(self, key: str, st: dict, action: str) -> None:
        """While the condition holds: make sure the robot is paused (or heading home), not just asked once."""
        act, now = self.rec.activity, time.time()
        if action not in ("pause", "dock") or not self.controller.enabled():
            if not st["notified"]:
                st["notified"] = True
                await self.notifier.send("guard", f"Guard: {st['why']}", "notify only" + (
                    "" if self.controller.enabled() else " (control is switched off)"))
            return
        if st["pending"] or st["snoozed"]:
            return
        # held, not acted: a confirmed pause that was handed back to people (not "ours") clears acted, yet the guard
        # did hold the robot during this detection, so a person's resume after it is still their override (#53).
        # Once it has held the robot, counted from that: a command from before it overrides nothing. Not
        # carried over a restart on purpose (the safe side: one more pause, then the person's next resume wins).
        since = st["held_at"] or st["since"] or 0
        if (st["acted"] or st["tries"] or st["held"]) and self._people_cmd_at() > since and act in BUSY:
            # A person resumed or started it while the guard was holding: their call, for this detection only.
            st["snoozed"] = True
            await self.notifier.send("guard", f"Guard: {st['why']}", "overridden by a person for this detection; "
                                     "it applies again the next time the condition appears")
            return
        if act == "moving" and not self.rec.plan_active():
            return                       # driven by hand, no plan: nothing a pause would change
        if (action == "pause" and act in NOT_PAUSABLE) or (action == "dock" and act in NOT_DOCKABLE):
            if not st["notified"]:
                st["notified"] = True
                await self.notifier.send("guard", f"Guard: {st['why']}",
                                         f"nothing sent: the robot is already {self.rec.status()['state']}")
            return
        if now - st["tried_at"] < min(GUARD_RETRY_MAX_S, GUARD_RETRY_S * 2 ** max(0, st["tries"] - 1)):
            return
        st["tried_at"] = now
        st["tries"] += 1
        cmd = "pause" if action == "pause" else "cmd_recharge"
        result = await self.controller.send(cmd, actor="guard", wait=False)
        if result.get("sent"):
            st["pending"] = {"cmd": cmd, "action": action, "at": now}
            if not st["notified"]:
                st["notified"] = True
                await self.notifier.send("guard", f"Guard: {st['why']}", f"{action}: sent (the {GUARD_RULES[key]}, "
                                         "Settings → Run rules → During a run)")
        elif not st["warned"]:
            st["warned"] = True
            await self.notifier.send("guard", f"Guard: {st['why']}",
                                     f"{action} NOT sent ({result.get('refused') or result.get('error')}); retrying")

    async def _guard_confirmations(self) -> None:
        """Watch the robot's state for the guard commands sent with wait=False."""
        r, now = self.rec, time.time()
        for key, st in self.guards.items():
            p = st["pending"]
            if not p:
                continue
            timeout, test, meaning = control.CONFIRM[p["cmd"]]
            if test(r.t):
                # Own the pause only if it is clearly ours: it landed after we sent it and nobody else spoke since.
                ours = (r.activity_at >= p["at"] - 1 and self._people_cmd_at() < p["at"]
                        and self.phone_app_seen_at() < p["at"] and r.plan is not None and r.plan.active
                        and r.plan.session is not None)
                st.update(pending=None, acted=p["action"], acted_at=now, warned=False, tries=0, held=True,
                          held_at=st["held_at"] or now,
                          owner={"activity_at": r.activity_at, "session": r.plan.session} if ours else None)
                await self.notifier.send("guard", f"Guard: {st['why']}", f"{p['action']}: done, {meaning}" + (
                    ". On a real mow, Return to dock ended the plan: the next schedule window starts it again"
                    if key == "dark" else ""))
            elif now - p["at"] > timeout:
                st["pending"] = None
                if st["tries"] in (1, 3):
                    await self.notifier.send("guard", f"Guard: {st['why']}", f"{p['action']}: no change seen in "
                                             f"{timeout} s" + ("; sending again" if st["tries"] == 1 else
                                                               " after 3 tries; still trying, check the robot"))
        rs = self._resume
        if rs:
            timeout, test, meaning = control.CONFIRM["resume"]
            if test(r.t) or now - rs["at"] > timeout:
                self._resume = None
                for k in rs["keys"]:
                    if k in self.guards:
                        self.guards[k].update(acted=None, owner=None)
                ok = test(r.t)
                await self.notifier.send("guard", "Guard: resume", f"done, {meaning}" if ok else
                                         f"no change seen in {timeout} s; left paused for a person")

    async def _guard_resume(self, g: dict) -> None:
        r, now = self.rec, time.time()
        held = [k for k, st in self.guards.items() if st["acted"] == "pause"]
        if not held or self._resume:
            return
        sts = [self.guards[k] for k in held]
        first = min(st["acted_at"] for st in sts)
        # Ownership: the robot must have sat paused, untouched, ever since the guard's pause was confirmed.
        # ...and still paused for the guard's reason (1): an e-stop, bumper, stuck, fault or rain pause on top of
        # it is the robot's own call and must never be resumed by the panel.
        if (r.activity != "paused" or not control.CONFIRM["pause"][1](r.t)
                or any(st["owner"] is None or st["owner"]["activity_at"] != r.activity_at for st in sts)):
            self._drop_ownership(f"the robot is now {r.status()['state']}")
            return
        if self._people_cmd_at() > first or self.phone_app_seen_at() > first:
            self._drop_ownership("someone else sent the robot a command")
            return
        if any(not st["owner"]["session"] for st in sts):
            self._drop_ownership("the paused plan could not be identified")
            return
        if not g["guard_resume"]:
            return
        if now - first > RESUME_MAX_S:
            self._drop_ownership(f"paused over {RESUME_MAX_S // 60} min")
            await self.notifier.send("guard", "Guard: not resuming", "paused too long; resume it yourself")
            return
        after = float(g["guard_resume_after_min"]) * 60
        if any(st["active"] or st["pending"] for st in self.guards.values()):
            return
        if any(st["clear_since"] is None or now - st["clear_since"] < after for st in sts):
            return
        if now - self._guard_cmd_at < GUARD_GAP_S:
            return
        self._guard_cmd_at = now
        yard_minutes = max(float(g["guard_resume_after_min"]), float(g["yard_clear_minutes"]))
        preview = await self.controller.preview("resume", {"yard_minutes": yard_minutes}, actor="guard")
        if not preview.get("would_send"):
            self._guard_cmd_at = now + RESUME_BACKOFF_S - GUARD_GAP_S     # look again in 5 min, not every minute
            if not any(st["warned"] for st in sts):
                for st in sts:
                    st["warned"] = True
                await self.notifier.send("guard", "Guard: not resuming yet", str(preview.get("reason")))
            return
        try:
            plan = parse_plan_feedback(await r.request("get_plan_feedback", timeout=10))
        except Exception as exc:
            log.info("guard resume: no plan status (%s)", exc)
            return
        sessions = {st["owner"]["session"] for st in sts}
        if plan is None or not plan.active or plan.session not in sessions:
            self._drop_ownership("the paused plan is no longer the one in progress")
            return
        result = await self.controller.send("resume", {"yard_minutes": yard_minutes}, actor="guard", wait=False)
        whys = ", ".join(st["why"] for st in sts)
        if result.get("sent"):
            self._resume = {"at": now, "keys": held}
            await self.notifier.send("guard", "Guard: resuming", f"clear for {after / 60:g} min after {whys}")
        elif not any(st["warned"] for st in sts):
            for st in sts:
                st["warned"] = True
            await self.notifier.send("guard", "Guard: not resuming yet", str(result.get("refused") or result.get("error")))

    async def _guard_docked(self, g: dict) -> None:
        """A guard sent the robot home, the cause has gone, and the robot heads back out by itself (a mid-plan
        charging break resumes on its own). That is a resume nobody checked: run the guard-resume interlocks and,
        if they fail, send it home again."""
        r = self.rec
        for st in self.guards.values():
            if st["acted"] != "dock" or st["active"] or st["pending"] or r.activity not in BUSY:
                continue
            yard_minutes = max(float(g["guard_resume_after_min"]), float(g["yard_clear_minutes"]))
            preview = await self.controller.preview("resume", {"yard_minutes": yard_minutes}, actor="guard")
            if preview.get("would_send"):
                st.update(acted=None, owner=None)
                await self.notifier.send("guard", f"Guard: {st['why']}",
                                         "the robot went back out by itself; the resume checks pass")
            elif self.controller.enabled() and time.time() - st["tried_at"] >= GUARD_RETRY_S:
                st["tried_at"] = time.time()
                result = await self.controller.send("cmd_recharge", actor="guard", wait=False)
                if result.get("sent"):
                    st["pending"] = {"cmd": "cmd_recharge", "action": "dock", "at": time.time()}
                await self.notifier.send("guard", f"Guard: {st['why']}", "the robot went back out by itself but "
                                         f"{preview.get('reason')}; sending it home again")

    async def _guard_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                self.controller.keep_awake_tick()           # §13: say when the Cameras tile's keep-awake has stopped
            except Exception:
                log.exception("keep-awake check failed")
            try:
                await self._guard_tick()
            except Exception:
                log.exception("guard check failed")

    def guard_status(self) -> list[dict]:
        return [{"key": k, "active": st["active"], "why": st["why"], "acted": st["acted"],
                 "pending": (st["pending"] or {}).get("action"), "since": st["since"],
                 "clear_since": st["clear_since"]} for k, st in self.guards.items()]

    # -- alerts and maintenance --------------------------------------------------------------------------------
    async def _alert_loop(self) -> None:
        last_maint_day = None
        while True:
            await asyncio.sleep(30)
            try:
                t = self.rec.t
                err = int(t.get("error_code") or 0)
                if err and err != self._last_error:
                    await self.notifier.send("robot_error", "Yarbo error", f"error code {err}")
                self._last_error = err
                await self._check_robot_alerts()
                head = t.get("head_type")
                if head is not None and self._head is not None and head != self._head:
                    n = (self.store.setting("head_changes", 0) or 0) + 1
                    self.store.set_setting("head_changes", n)
                    await self.notifier.send("head_changed", "Head changed", f"now {self.rec.status()['robot']['head']}")
                if head is not None:
                    self._head = head
                if self.cloud.state == "needs login" and not self._cloud_alerted:
                    await self.notifier.send("cloud_login", "Yarbo cloud needs a new sign-in", "Settings → Robot")
                    self._cloud_alerted = True
                elif self.cloud.state == "connected":
                    self._cloud_alerted = False
                today = datetime.now().strftime("%Y-%m-%d")
                if today != last_maint_day and datetime.now().hour >= 8:
                    last_maint_day = today
                    due = [i for i in self.maintenance()["items"] if i["due"] in ("overdue", "due", "due soon")]
                    if due:
                        await self.notifier.send("maintenance", "Maintenance due",
                                                 ", ".join(f"{i['name']} ({i['due']})" for i in due))
            except Exception:
                log.exception("alert check failed")

    async def _check_robot_alerts(self) -> None:
        """Stuck (the robot's flag, or progressing without getting anywhere) and lost contact during a run. Each
        notifies once and once more when it clears; stuck clears only when the robot really got somewhere (or
        stopped working), so a robot rocking on the spot does not flap."""
        r = self.rec
        t, tr = r.t, r.trouble()
        url = f"{self.cfg.panel_url}/#live" if self.cfg.panel_url else None
        # The robot's own stuck signals (flag, pause reason 6, recharge -9) or the panel's stall detection.
        robot_stuck = bool(t.get("stuck")) or t.get("planning_paused") == 6 or t.get("recharging") == -9
        stuck_now = robot_stuck or tr["stalled"]
        x, y = t.get("x"), t.get("y")
        # Standing still without a fix, blades stopped: the robot waits for its fix (not stuck). Said once, and
        # once more when it drives on; if it still stands once the fix is back, the stuck alert below takes over.
        rtk_wait = tr.get("rtk_wait", False) and not robot_stuck and self._stuck_at is None
        if rtk_wait and not self._rtk_wait:
            self._rtk_wait = True
            await self.notifier.send("stuck", "Yarbo is waiting for an RTK fix",
                                     f"{r.status()['state']}, blades stopped, no RTK fix for {tr['stalled_s'] / 60:.0f} "
                                     "min; it carries on by itself once the fix is back", url=url)
        elif self._rtk_wait and not tr.get("rtk_wait", False) and not tr["lost_contact"] \
                and (tr.get("data_age_s") or 0) <= 60:          # stale data says nothing about the fix
            self._rtk_wait = False
            await self.notifier.send("stuck", "Yarbo is no longer waiting for an RTK fix",
                                     f"RTK {telemetry.rtk_label(t.get('rtk_status'))}; the robot is {r.status()['state']}")
        if self._stuck_at is None:
            if stuck_now:
                self._stuck_at = (x, y)
                why = ("the robot reports it is stuck" if robot_stuck else
                       f"{r.status()['state']} but it has not moved in {tr['stalled_s'] / 60:.0f} min")
                await self.notifier.send("stuck", "Yarbo may be stuck", f"{why}; check the map and cameras", url=url)
        else:
            sx, sy = self._stuck_at
            moved = None not in (x, y, sx, sy) and ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5 > STUCK_CLEAR_M
            if r.activity not in PROGRESSING and not robot_stuck:
                self._stuck_at = None
                await self.notifier.send("stuck", "Stuck alert cleared", f"the robot is now {r.status()['state']}")
            elif moved and not stuck_now:
                self._stuck_at = None
                await self.notifier.send("stuck", "Yarbo is moving again",
                                         f"more than {STUCK_CLEAR_M:g} m from where it got stuck")
        st = r.status()
        first = not self._codes                 # after a restart: learn the current codes, do not re-announce them
        for key, label in (("plan_error", "Plan error"), ("recharge_error", "Return-to-dock error")):
            now_err = st.get(key)
            if key == "recharge_error" and t.get("recharging") == -9:
                now_err = None                  # "stuck" is already the stuck alert
            if now_err and now_err != self._codes.get(key) and not first:
                await self.notifier.send("robot_error", f"Yarbo: {label}", now_err, url=url)
            self._codes[key] = now_err
        # The robot paused itself (bumper, e-stop, rain...): say why and what to do, once per pause. Stuck has its
        # own alert above; a charging break is routine.
        paused = getattr(r, "run_id", None) is not None and getattr(r, "activity", None) == "paused"
        held = reasons.from_pause(t.get("planning_paused")) if paused else None
        code = held["code"] if held else None
        if code != self._pause_code and held and code not in ("stuck", "charging_break") and not first:
            await self.notifier.send("robot_paused", f"Yarbo paused: {held['title']}", held["fix"], url=url)
        self._pause_code = code
        if tr["lost_contact"] and self._lost_since is None:
            self._lost_since = r.update_at
            await self.notifier.send("lost_contact", "No data from Yarbo",
                                     f"the panel has had nothing from the robot for {tr['data_age_s'] / 60:.0f} min "
                                     "during a run (weak network, out of range, or the local link is down)", url=url)
        elif not tr["lost_contact"] and self._lost_since is not None:
            gap = max(0.0, r.update_at - self._lost_since)
            self._lost_since = None
            await self.notifier.send("lost_contact", "Yarbo is reporting again",
                                     f"after about {gap / 60:.0f} min without data")

    def maintenance(self) -> dict:
        counters = maintenance.counters_from(self.store, self.store.setting("head_changes", 0) or 0)
        items = self.store.setting("maintenance_items")
        if items is None:
            items = maintenance.default_items(counters)
            self.store.set_setting("maintenance_items", items)
            self.store.set_setting("maintenance_defaults", maintenance.DEFAULTS_VERSION)
        elif (self.store.setting("maintenance_defaults") or 0) < maintenance.DEFAULTS_VERSION:
            items = maintenance.upgrade(items, counters)
            self.store.set_setting("maintenance_items", items)
            self.store.set_setting("maintenance_defaults", maintenance.DEFAULTS_VERSION)
            self.store.event("maintenance_defaults", "maintenance intervals set to Yarbo's recommendations where unset")
        return {"counters": counters, "items": maintenance.status(items, counters), "units": maintenance.UNITS}

    # -- follow-cam --------------------------------------------------------------------------------------------
    FOLLOW_HOLD_S = 10

    def follow_camera(self) -> dict:
        """The camera whose coverage holds the robot, and `covered`: whether any coverage holds it (Live's camera layout
        shows the map instead when none does). A new answer must hold for FOLLOW_HOLD_S before the follow-cam switches:
        a docked robot's position jitters across the edge of two coverages, and every switch restarts the live stream
        or swaps the big picture."""
        pick = self._follow_pick()
        now, cur = time.time(), self._follow
        key = lambda d: (d["camera"], d["covered"])
        if cur and key(pick) != key(cur) and cur["camera"] in (self.store.setting("camera_polygons", {}) or {}):
            if self._follow_next[0] != key(pick):
                self._follow_next = (key(pick), now)
            if now - self._follow_next[1] < self.FOLLOW_HOLD_S:
                return cur
        self._follow, self._follow_next = pick, (None, 0.0)
        return pick

    def _follow_pick(self) -> dict:
        pos = self.rec.status()["robot"]["position"]
        polys = self.store.setting("camera_polygons", {}) or {}
        if pos.get("lon") is not None:
            for cam, ring in polys.items():
                if point_in_ring(pos["lon"], pos["lat"], ring):
                    return {"camera": cam, "covered": True, "reason": "covers the robot's position"}
        cams = self.yard_cameras()
        return {"camera": cams[0] if cams else None, "covered": False,
                "reason": "no camera polygon covers the robot; showing the first yard camera" if cams else
                          "choose yard cameras in Settings"}

    def why_now(self) -> dict:
        """What Live shows under "why": the robot's own reason for a pause, and how the last run ended when it did
        not finish its plan (within the last 12 h)."""
        t = self.rec.t
        paused = self.rec.run_id is not None and self.rec.activity == "paused"
        held = reasons.from_pause(t.get("planning_paused")) if paused else None
        last = None
        if self.rec.run_id is None:
            runs = self.store.runs(1)
            if runs and runs[0].get("ended") and time.time() - runs[0]["ended"] < 12 * 3600:
                last = reasons.from_end(runs[0].get("end_reason"))
                if last:
                    last = {**last, "run_id": runs[0]["id"], "ended": runs[0]["ended"]}
        return {"held": held, "last_run": last}

    def status(self) -> dict:
        return {"cloud": self.cloud.status(), "control_enabled": self.controller.enabled(), "why": self.why_now(),
                "map_write_enabled": bool(self.store.setting("map_write_enabled", False)),
                "frigate": self.frigate.status(), "notify": self.notifier.channels(),
                "pending": self.scheduler.pending(), "guards": self.guard_status(),
                "last_command": self.controller.last, "link": self.link.status(),
                "phone_app_seen_s": (round(time.time() - max(self.phone_app_at, self.rec.app_stream_at))
                                     if max(self.phone_app_at, self.rec.app_stream_at) else None)}
