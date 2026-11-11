"""The recorder: listens to the robot, keeps the latest state, writes telemetry, runs, map versions and events.

Polling policy (read requests only):
* community_yarbo and the phone app already ask for get_device_msg often; their replies arrive on
  data_feedback and we consume them passively.
* We only ask ourselves when the latest full snapshot is older than POLL_IDLE_S (or POLL_ACTIVE_S during a run).
* Map, plans, GPS reference and low-confidence config are read on start and every MAP_REFRESH_S.
* Wi-Fi survey (Settings → Robot, off by default): get_wifi_list every WIFI_POLL_S while a run is moving. It is a
  ~4 s scan on the robot, so "next" (one run, then off by itself) comes first, to check the link stays healthy while
  it scans.
"""
import asyncio
from collections import deque
from collections.abc import Callable
import copy
import json
import logging
import math
import statistics
import threading
import time
from pathlib import Path

from . import corrections, coverage, demo_robot, geo, planned as planned_mod, problems, scheduler, telemetry
from .edges import EdgeIndex
from .codec import decode_map, decode_any, locate_map, redact
from .conditions import ConditionsConfig, evaluate
from .config import Config
from .ha import HAPublisher, entities_from
from .imagery import Imagery
from .runs import (ACTIVITY_LABELS, HELD, PAUSE_CAP_S, PROGRESSING, PlanStatus, RunDetector, RunStats, activity,
                   end_reason, is_moving, parse_plan_feedback, plan_flags_active, progressing)
from .store import Store
from .tempest import TempestListener
from . import weather as weather_mod
from .transport_local import LocalTransport, discover_sn

log = logging.getLogger(__name__)

# Replies we already keep elsewhere (the snapshot and the decoded map), so they are not stored as raw replies.
SKIP_REPLIES = {"get_device_msg", "get_map"}
# A backup's map can come back under any of these reply topics (seen in the wild); it is stored as backup:{id}.
BACKUP_REPLIES = ("get_map_buckup_from_id", "get_map_backup_from_id", "get_map_backup", "map_backup")
SKIP_REPLIES |= set(BACKUP_REPLIES)
# Replies that change on every poll (signal strengths) are shown live but never versioned in the database.
VOLATILE_REPLIES = {"get_wifi_list", "get_connect_wifi_name", "get_plan_feedback"}
# How often to ask for the running plan's progress while a plan is in progress (and soon after any state change).
PLAN_POLL_S = 60
PLAN_REPLIES_KEPT = 3     # full plan replies kept per run (see _keep_plan_reply)
# Joystick and keep-alive traffic would flood the capture table; their names are enough.
NOT_CAPTURED = {"cmd_vel", "heart_beat", "set_working_state"}
# A recording session ("Record an app edit", Map Studio → More) keeps everything except these high-rate streams,
# which are only counted. Replies to the panel's own periodic reads are skipped too (they are huge and not news).
SESSION_COUNT_ONLY = {("app", "cmd_vel"), ("app", "heart_beat"), ("app", "set_working_state"), ("device", "DeviceMSG"),
                      ("device", "heart_beat")}
SESSION_SKIP_REPLIES = {"get_device_msg", "get_map", "get_plan_feedback", "read_gps_ref", "read_all_plan",
                        "read_low_cfd_config", "get_wifi_list", "get_connect_wifi_name"}
SESSION_MAX_ENTRIES = 3000
SESSION_MAX_BYTES = 200_000     # per message
# The panel's own sends of these two are matched to their echoes one for one (Recorder.own_echo), not by a time window:
# the Cameras tile sends a keep-awake every 4 s, so any window longer than that would hide the phone app's own beats.
COUNTED_OWN = frozenset({"set_working_state", "smart_vision_control"})
OWN_ECHO_S = 5            # an echo of our own send is seen within this long of it
# Commands that mean a person (phone app, another client) is steering the robot: a guard gives up its pause.
PEOPLE_COMMANDS = {"start_plan", "pause", "resume", "stop", "cmd_recharge", "cmd_vel"}
# While doing one of these the robot should be getting somewhere; standing still for minutes means it is stuck
# (2026-10-05 10:07-10:19: "mowing", wheels at 0, position frozen, and the robot's own stuck flag stayed 0).
# (PROGRESSING / progressing() live in runs.py; "planning route" is left out: it can stand still while it computes.)
STALL_RADIUS_M = 1.0
STALL_ALERT_S = 180       # progressing but within STALL_RADIUS_M this long = probably stuck
FIX_HOLD_S = 10           # after waiting for an RTK fix, the fix must hold this long before the wait is over
STALL_GAP_S = 60          # a longer gap in the data starts a fresh comparison (no stall measured across an outage)
LOST_CONTACT_S = 120      # no data at all this long during a run = lost contact
POSES_KEPT = 64           # position samples kept for /api/stream's pose events (~1 a second while it moves)
POSE_RESET_S = 30         # nothing newer by the robot's clock this long: take the next sample anyway (its clock was set back)
WIFI_POLL_S = 30          # Wi-Fi survey: one scan this often while a run is moving
WIFI_MODES = ("off", "next", "on")    # off · next run only (then off by itself) · every run
WIFI_SAVE_S = 60          # the open run's survey counters are kept in the database this often (a restart loses no more)
WIFI_TEST_MIN_S = 600     # a "next run" survey with less moving time than this (a short drive) does not use up the test
WIFI_TIMEOUT_S = 120      # an ask unanswered this long counts as unanswered; only then is the next one sent
# Run 4 (2026-10-10 13:15): while mowing the robot often took 30-60 s to answer, and an ask sent after a 60 s timeout
# was answered together with the old one (two answers ~0.1 s apart). So: a longer timeout, a repeat inside WIFI_DUP_S
# is dropped, and only an answer within WIFI_FRESH_S of our ask is placed on the map (later, the robot has moved on).
WIFI_FRESH_S = 12
WIFI_DUP_S = 3
WIFI_NEAR_S = 10          # a pause in the robot's messages starting this soon after an ask is put down to the scan
GAP_S = 3                 # no message from the robot this long (it sends ~2 a second) counts as a pause
WIFI_KEEP_DAYS = 90       # survey rows (they hold neighbours' network names) are deleted after this


BLADE_OFF_RPM = 100       # blades slower than this (both) are stopped; mowing they turn at a few thousand


def waiting_for_fix(t: dict) -> bool:
    """No RTK fix and the blades stopped: the robot holds still until its fix comes back. Blades the robot does not
    report (a snow head) do not count against it."""
    rtk = t.get("rtk_status")
    if rtk is None or int(rtk) == 4:
        return False
    rpms = [abs(v) for v in (t.get("blade_rpm_l"), t.get("blade_rpm_r")) if isinstance(v, (int, float))]
    return not rpms or max(rpms) < BLADE_OFF_RPM


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.warning("robot connection attempt failed: %s", task.exception())


DEADEND_STALE_S = 60      # a timed-out dead-end settings read may still be answered this long after
PLAN_JOIN_MAX_S = 36 * 3600   # a resume of the same plan session this long after the previous run still joins it
FIRMWARE_SETTLE_S = 60    # a new firmware version counts once it has been reported this long without a break


class Recorder:
    def __init__(self, cfg: Config, store: Store, imagery: Imagery, sn: str):
        self.cfg, self.store, self.imagery, self.sn = cfg, store, imagery, sn
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.transport: LocalTransport | None = None     # made by apply_robot() once there is an address
        self.link = None             # app.link.RobotLink, set by Ops: reads fall back to the cloud through it
        self.foreign_cmds: dict[str, float] = {}   # PEOPLE_COMMANDS seen from other clients, by name
        self.app_stream_at = 0.0     # last unrequested DeviceMSG: the robot streams them only while the phone app is open
        self.robot_error = ""                            # why there is no local connection
        self.on_sn: Callable[[str], None] | None = None  # told when the serial is found or changed (Ops follows)
        self._robot_lock = asyncio.Lock()
        self._discover_at = 0.0
        self._applied: tuple | None = None               # (host, port) the current serial was found on
        self._robot_task: asyncio.Task | None = None
        self.started_at = time.time()

        self.snapshot: dict = {}
        self.snapshot_at = 0.0          # last full get_device_msg reply
        self.update_at = 0.0            # last update of any kind
        self.heartbeat: dict = {}
        self.heartbeat_at = 0.0
        self.t: dict = {}               # flat values from the latest snapshot
        self.poses: deque = deque(maxlen=POSES_KEPT)   # position samples in the robot's pose order (see _note_pose)
        self.pose_seq = 0
        self._pose_rt: float | None = None      # the robot's time (odom_ts) of the newest sample kept
        self._pose_at = 0.0                     # when that sample arrived

        self.gps_ref: tuple[float, float] | None = None
        self.map_raw: dict | None = None
        self.map_geojson: dict | None = None
        self.map_fetched = 0.0
        self.plans: list = []
        self.cfd: dict | None = None
        self.robot_schedules: list | None = None   # the robot's own (app-made) schedules; None = not read yet
        self.yard: dict | None = imagery.yard_meta

        self.detector = RunDetector(cfg.run_idle_end_s)
        self.run_id: int | None = None
        self.run_stats: RunStats | None = None
        self.run_started = 0.0
        self.activity = "idle"            # runs.activity() of the latest snapshot
        self.activity_at = 0.0            # when it last changed
        self.plan: PlanStatus | None = None   # latest get_plan_feedback, parsed
        self.plan_at = 0.0
        self.run_plan: PlanStatus | None = None  # the last in-progress feedback seen during the open run
        self._plan_asked = 0.0
        self._plan_due = False            # ask for plan status soon (state changed)
        self._plan_silent = False         # warned that the flags say "plan" but no plan reply comes
        self.foreign_cmd_at = 0.0         # last run command from anyone but this panel
        self.session: dict | None = None  # an "app edit" recording in progress
        self._stall_anchor: tuple[float, float, float] | None = None   # (x, y, since) while progressing
        self._stall_seen = 0.0
        self._stall_waiting = False       # waiting for an RTK fix (waiting_for_fix), until the fix holds FIX_HOLD_S
        self._fix_back_at: float | None = None   # when the fix came back during such a wait
        self.stalled_s = 0.0              # how long it has been "working" without getting anywhere

        self.last_row_at = 0.0
        self.last_raw_at = 0.0
        self.last_poll_at = 0.0
        self.firmware: dict = store.firmware_last()
        self._fw_pending: dict[str, tuple[str, float]] = {}   # component -> (new version, first seen) not yet settled
        self._corrections = corrections.GapTracker()          # RTK correction gaps, an event when each one ends
        self.replies: dict[str, dict] = {}     # latest data_feedback per topic, from anyone's request
        self._pending: dict[str, list[asyncio.Future]] = {}
        self._backup_lock = asyncio.Lock()
        self._pathway_lock = asyncio.Lock()
        self._deadend_timeouts: list[float] = []      # reads of dead-end settings that timed out (a late answer may come)
        self._area_lock = asyncio.Lock()
        self._preview_lock = asyncio.Lock()
        self._sent: dict[str, float] = {}
        self._own_lock = threading.Lock()         # own_echo is called from the cloud thread and the event loop
        self._own_beats: dict[str, deque] = {}    # COUNTED_OWN command -> send times not yet matched to an echo
        self._own_seen: dict[tuple, bool] = {}    # (name, arrival ts) -> was it ours: one message, one answer

        self.tempest: TempestListener | None = None      # made by apply_weather()
        self.conditions: dict | None = None
        self.ha_weather = weather_mod.HAWeather(self._ha_state)
        self.ha = HAPublisher(cfg.ha_url, cfg.ha_token)
        self._analysing: set[int] = set()
        self._edge_src: tuple | None = None       # (map_raw object, gps_ref) the index was built from
        self._edge_index: EdgeIndex | None = None
        self._signal_cache: dict = {}
        self.wifi_survey: dict | None = None      # the open run's survey counters (see _poll_wifi)
        self._wifi_asked = 0.0
        self._wifi_waiting: tuple | None = None   # (ts, x, y) of our unanswered get_wifi_list
        self._wifi_foreign_at = 0.0               # another client's get_wifi_list (its answer is not ours to time)
        self._wifi_answer_at = 0.0                # the last get_wifi_list answer (a repeat right after is dropped)
        self._robot_at = 0.0                      # the robot's last message of any kind (not our own echoes)
        self._run_summaries: dict = {}            # run_id -> ((rows, last ts, write_gen), problems summary)
        self._problem_lock = asyncio.Lock()

        self._restore()

    def _restore(self) -> None:
        ref = self.store.latest_version("gps_ref")
        if ref:
            self._set_ref(ref)
        raw = self.store.latest_version("map")
        if raw:
            self.map_raw = raw
            self.map_geojson = geo.map_to_geojson(raw, self.gps_ref)
        self.plans = (self.store.latest_version("plans") or {}).get("data", [])
        self.cfd = self.store.latest_version("cfd")
        for topic, item in self.store.latest_replies().items():
            self.replies[topic] = {"ts": item["changed"], "changed": item["changed"], "data": item["data"]}
        closed = self.store.close_orphan_runs()
        if closed:
            self.store.event("run_closed", f"{closed} run(s) left open by a previous process")
        redone = self.store.recompute_all_runs_once()
        if redone:
            self.store.event("runs_recomputed", f"totals of {redone} run(s) worked out again from their stored rows")
        self._settle_old_surveys(time.time())

    def _settle_old_surveys(self, now: float) -> None:
        """A surveyed run cut off by a restart and never joined again (it ended too long ago to be) still gets its
        report, from the counters kept with it, and they are then dropped."""
        for key in self.store.settings_like("wifi_survey_run:"):
            try:
                rid = int(key.split(":", 1)[1])
            except ValueError:
                self.store.delete_setting(key)
                continue
            run = self.store.run(rid)
            if run is None or (run.get("ended") and now - run["ended"] > PAUSE_CAP_S):
                self._finish_wifi_survey(rid)

    # -- plumbing ----------------------------------------------------------------------------------------------
    def mark_sent(self, cmd: str):
        """The panel is sending `cmd` now (RobotLink calls this just BEFORE every send: the echo can reach the broker's
        thread before publish() returns): its echo is ours. Returns an undo for a send that then fails."""
        now = time.time()
        before = self._sent.get(cmd)
        self._sent[cmd] = now
        counted = cmd in COUNTED_OWN
        if counted:
            with self.__dict__.setdefault("_own_lock", threading.Lock()):
                self.__dict__.setdefault("_own_beats", {}).setdefault(cmd, deque(maxlen=16)).append(now)

        def undo() -> None:
            if before is None:
                self._sent.pop(cmd, None)
            else:
                self._sent[cmd] = before
            if counted:
                with self.__dict__.setdefault("_own_lock", threading.Lock()):
                    try:
                        self.__dict__.setdefault("_own_beats", {}).get(cmd, deque()).remove(now)
                    except ValueError:
                        pass                      # already used by an echo, or aged out
        return undo

    def own_echo(self, name: str, ts: float, data=None) -> bool:
        """Is the message `name` that arrived at `ts` the echo of something this panel sent? One answer per message, so
        the phone-app detection (ops) and the recorder, which both look at it, agree. Most commands: sent within
        the last OWN_ECHO_S. The keep-awake and camera-on (COUNTED_OWN): each of our sends accounts for ONE echo
        and goes unused after OWN_ECHO_S, so the Yarbo app's own beats, arriving seconds after ours, still count as
        the app's. What we send is always {"state": 1}: a message that says anything else (the app switching the cameras
        off) is never ours and leaves our pending send for its real echo."""
        if name not in COUNTED_OWN:
            return ts - self._sent.get(name, 0) < OWN_ECHO_S
        if isinstance(data, dict) and not (type(data.get("state")) is int and data["state"] == 1):
            return False
        lock = self.__dict__.setdefault("_own_lock", threading.Lock())
        with lock:
            seen = self.__dict__.setdefault("_own_seen", {})
            if (name, ts) in seen:
                return seen[(name, ts)]
            q = self.__dict__.setdefault("_own_beats", {}).setdefault(name, deque(maxlen=16))
            while q and q[0] < ts - OWN_ECHO_S:
                q.popleft()                       # never echoed back (or the echo was missed): it matches nothing now
            ours = bool(q) and q[0] <= ts         # a send recorded after the message arrived cannot be what it echoes
            if ours:
                q.popleft()
            seen[(name, ts)] = ours
            while len(seen) > 64:
                seen.pop(next(iter(seen)))
            return ours

    def _from_thread(self, topic: str, data, ts: float) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (topic, data, ts))

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.apply_robot()
        await self.apply_weather()
        loops = [self._consume(), self._poll(), self._refresh_loop(), self._housekeeping(), self._conditions_loop(),
                 self._ha_loop(), self._ha_weather_loop()]
        if getattr(self.cfg, "demo", False):
            loops.append(self._demo_weather_loop())
        await asyncio.gather(*loops)

    async def _demo_weather_loop(self) -> None:
        """DEMO=1: made-up weather every 5 minutes, so the lawn conditions stay current (no Tempest, no HA)."""
        from .demo import weather_row
        while True:
            self.store.add_weather(weather_row(time.time()))
            self.evaluate_conditions()
            await asyncio.sleep(300)

    def can_read(self) -> bool:
        """Some route can carry a read: the local broker, or (through the link) the cloud."""
        if self.link is not None:
            return self.link.read_route() is not None
        return bool(self.transport and self.transport.connected)

    def _send_read(self, cmd: str, payload: dict | None = None) -> str | None:
        """Publish a read request the way the link says (local first, the cloud as a fallback)."""
        if self.link is not None:
            return self.link.read(cmd, payload)
        self.transport.request(cmd, payload)
        return "local"

    async def request(self, cmd: str, timeout: float = 15, payload: dict | None = None, replies: tuple = ()):
        """Send a read request and wait for its data_feedback reply (on `cmd` or any of `replies`)."""
        if not self.transport and not self.can_read():
            raise ConnectionError(self.robot_error or "the robot is not set up")
        fut = self.loop.create_future()
        names = (cmd, *replies)
        for n in names:
            self._pending.setdefault(n, []).append(fut)
        self._sent[cmd] = time.time()
        route = None
        try:
            route = self._send_read(cmd, payload)
            reply = await asyncio.wait_for(fut, timeout)
            if self.link is not None:
                self.link.note_read(cmd, route, True)
            return reply
        except asyncio.TimeoutError:
            if self.link is not None:
                self.link.note_read(cmd, route, False)
            raise
        finally:
            for n in names:
                waiters = self._pending.get(n, [])
                if fut in waiters:
                    waiters.remove(fut)

    # -- map backups (the robot keeps its own; reading them is a plain read request) ---------------------------
    def backups(self) -> list[dict]:
        rep = (self.replies.get("get_all_map_backup") or {}).get("data") or \
            self.store.latest_version("reply:get_all_map_backup") or {}
        items = decode_any(rep.get("data")) if isinstance(rep, dict) else None
        fetched = {v["kind"].split(":", 1)[1]: v["fetched"] for v in self.store.versions_like("backup:%")}
        out = []
        for b in items if isinstance(items, list) else []:
            if isinstance(b, dict) and "id" in b:
                out.append({"id": b["id"], "name": b.get("name") or "", "timestamp": b.get("timestamp"),
                            "auto": bool(b.get("is_auto_backup")), "fetched": fetched.get(str(b["id"]))})
        return sorted(out, key=lambda b: b.get("timestamp") or 0, reverse=True)

    async def reload_map(self) -> dict:
        """A fresh get_map (used around map writes): stores a version if it changed and updates the live map."""
        reply = await self.request("get_map", timeout=30)
        raw = decode_map(reply.get("data"))
        self.store.save_version("map", raw)
        self.map_raw = raw
        self.map_geojson = geo.map_to_geojson(raw, self.gps_ref)
        self.map_fetched = time.time()
        return raw

    async def read_area_records(self) -> dict:
        """{id: full area record} from read_all_clean_area (the robot's own copy, incl. keys get_map leaves out)."""
        reply = await self.request("read_all_clean_area", timeout=15)
        data = reply.get("data")
        items = data.get("data") if isinstance(data, dict) else data
        if reply.get("state") != 0 or not isinstance(items, list):
            raise ValueError(f"no area records (state {reply.get('state')}: {reply.get('msg')})")
        return {a["id"]: a for a in items if isinstance(a, dict) and isinstance(a.get("id"), int)}

    async def request_area_params(self, aid: int) -> dict:
        """The raw reply to read_mower_area_params. Replies are matched by topic only, so these reads are serialised
        (the settings editor and a backup restore both make them)."""
        async with self._area_lock:
            return await self.request("read_mower_area_params", timeout=10, payload={"id": int(aid)})

    async def preview_area_path(self, payload: dict) -> dict | None:
        """The robot's planned route for an area outline with that area's saved mow settings: {path: [{x, y, phi}],
        total_time (s)}. The app asks for it while editing; it takes ~10 s for the big area. Serialised (replies match
        by topic only)."""
        async with self._preview_lock:
            reply = await self.request("preview_snowbot_area_path", timeout=60, payload=payload)
        data = decode_any(reply.get("data")) if isinstance(reply.get("data"), str) else reply.get("data")
        return data if reply.get("state") == 0 and isinstance(data, dict) else None

    async def read_area_params(self, aid: int) -> dict | None:
        """One area's mowing settings; id 0 in them means never saved (the robot's defaults)."""
        reply = await self.request_area_params(aid)
        data = reply.get("data")
        return data if reply.get("state") == 0 and isinstance(data, dict) else None

    async def reload_plans(self) -> list:
        """A fresh read_all_plan, kept as the current plans (and a version when they changed)."""
        reply = await self.request("read_all_plan")
        if not isinstance(reply.get("data"), dict):
            raise ValueError("the robot returned no plan list")
        self.plans = reply["data"].get("data") or []
        self.store.save_version("plans", reply["data"])
        return self.plans

    async def read_deadend_params(self, did: int) -> dict | None:
        """One dead end's settings. Serialised with the pathway reads (replies are matched by topic). The robot answers
        id 0 for a dead end whose settings were never saved (its defaults), so an id-0 answer cannot say which dead
        end it is for: while an earlier read that timed out may still be answering (DEADEND_STALE_S), an id-0 or
        other-id answer is dropped and the read asked again. Only an answer carrying this id is unambiguous."""
        async with self._pathway_lock:
            for _ in range(3):
                try:
                    reply = await self.request("read_deadend_params", timeout=10, payload={"id": int(did)})
                except asyncio.TimeoutError:
                    self._deadend_timeouts.append(time.time())
                    raise
                data = reply.get("data")
                if reply.get("state") != 0 or not isinstance(data, dict):
                    return None
                if data.get("id") == int(did):
                    return data
                recent = [t for t in self._deadend_timeouts if time.time() - t < DEADEND_STALE_S]
                self._deadend_timeouts = recent
                if data.get("id") == 0 and not recent:
                    return data
                if recent:
                    self._deadend_timeouts.pop(0)          # that was the late answer: one fewer outstanding
                    continue
                raise ValueError(f"the robot answered for dead end {data.get('id')} instead of {did}; try again")
            raise ValueError(f"no unambiguous answer for dead end {did}; try again")

    async def read_pathway_params(self, pid: int) -> dict | None:
        """One pathway's settings. Replies are matched by topic only, so reads are serialised and a reply that
        carries another pathway's id (a late answer to an earlier read) is refused rather than trusted."""
        async with self._pathway_lock:
            reply = await self.request("read_mower_pathway_params", timeout=10, payload={"id": int(pid)})
        data = reply.get("data")
        if reply.get("state") != 0 or not isinstance(data, dict):
            return None
        if data.get("id") != int(pid):
            raise ValueError(f"the robot answered for pathway {data.get('id')} instead of {pid}; try again")
        return data

    async def refresh_backups(self) -> list[dict]:
        await self.request("get_all_map_backup", timeout=10)
        await asyncio.sleep(0)  # let _handle store the reply first
        return self.backups()

    async def fetch_backup(self, bid: int) -> dict:
        async with self._backup_lock:
            reply = await self.request("get_map_buckup_from_id", timeout=40, payload={"id": int(bid)},
                                       replies=BACKUP_REPLIES[1:])
        raw = locate_map(reply.get("data"))
        if raw is None:
            raise ValueError(f"backup {bid}: no map in the reply (state {reply.get('state')}: {reply.get('msg')})")
        self.store.save_version(f"backup:{int(bid)}", raw)
        return raw

    # -- message handling --------------------------------------------------------------------------------------
    async def _consume(self) -> None:
        while True:
            topic, data, ts = await self.queue.get()
            try:
                self._handle(topic, data, ts)
            except Exception:
                log.exception("failed to handle %s", topic)

    def _handle(self, topic: str, data, ts: float) -> None:
        parts = topic.split("/", 3)
        if len(parts) < 4:
            return
        channel, name = parts[2], parts[3]
        if self.session and not self.session.get("ended"):
            self._session_record(channel, name, data, ts)
        if channel == "device":
            self._robot_heard(ts)
        if channel == "device" and name == "data_feedback" and isinstance(data, dict):
            reply_to = data.get("topic")
            for fut in self._pending.pop(reply_to, []):
                if not fut.done():
                    fut.set_result(data)
            if reply_to == "get_device_msg" and isinstance(data.get("data"), dict):
                self._ingest(data["data"], ts, full=True)
            if reply_to == "get_wifi_list":
                self._on_wifi(data, ts)
            if reply_to == "get_plan_feedback":
                self._on_plan(parse_plan_feedback(data), ts)
                if self.run_id is not None and data.get("state") == 0 and isinstance(data.get("data"), dict):
                    self._keep_plan_reply(data["data"], ts)
                    self._keep_plan_paths(data["data"], ts)
            elif isinstance(reply_to, str) and reply_to not in SKIP_REPLIES:
                body = {"state": data.get("state"), "msg": data.get("msg"), "data": data.get("data")}
                changed = reply_to not in VOLATILE_REPLIES and self.store.save_version(f"reply:{reply_to}", redact(body))
                previous = self.replies.get(reply_to, {})
                self.replies[reply_to] = {"ts": ts, "changed": ts if changed else previous.get("changed", ts),
                                          "data": body}
        elif channel == "device" and name == "DeviceMSG" and isinstance(data, dict):
            # The robot streams these only while the phone app is connected; the panel's own reads come back on
            # data_feedback, never here. Every one counts (the demo robot's aside): if one were ever caused by the
            # panel's own read, the result is "app open", which blocks automatic starts - the safe direction.
            if not getattr(self.cfg, "demo", False):
                self.app_stream_at = ts
            self._ingest(data, ts, full=False)
        elif channel == "device" and name == "heart_beat" and isinstance(data, dict):
            self.heartbeat, self.heartbeat_at = data, ts
        elif channel == "app":
            ours = self.own_echo(name, ts, data)
            if not ours and name in PEOPLE_COMMANDS:
                self.foreign_cmd_at = ts
                self.foreign_cmds[name] = ts     # by command: a late echo of the panel's own is told apart
            if not ours and name == "get_wifi_list":
                self._wifi_foreign_at = ts
            safe = json.dumps(redact(data))                 # never keep another client's secrets
            if not ours and self.store.command_seen(name, ts, safe[:300]):
                self.store.event("new_command_seen", f"another client sent {name} {safe[:200]}")
            if not ours and not name.startswith(("get_", "read_")) and name not in NOT_CAPTURED:
                self.store.add_capture(name, ts, safe)

    def _ingest(self, snap: dict, ts: float, full: bool) -> None:
        telemetry.deep_merge(self.snapshot, snap)
        self.update_at = ts
        if full:
            self.snapshot_at = ts
        t = telemetry.extract(self.snapshot)
        if t.get("x") is not None and t.get("y") is not None and self.gps_ref:
            t["lon"], t["lat"] = geo.to_lonlat(self.gps_ref, t["x"], t["y"])
        elif t.get("rtk_lat") and t.get("rtk_lon"):
            t["lon"], t["lat"] = t["rtk_lon"], t["rtk_lat"]
        self.t = t
        if isinstance(snap.get("CombinedOdom"), dict):
            self._note_pose(t, ts, telemetry.extract({"CombinedOdom": snap["CombinedOdom"]}).get("odom_ts"))
        self._track_firmware(ts)
        self._track_run(ts, t)
        # Only an update that carries the age: a partial one keeps the last age while the clock moves on.
        gap = self._corrections.feed(ts, t) if "rtcm_age" in snap else None
        if gap:
            self.store.event("rtk_corrections_gap", corrections.describe(gap))

        # ~1 s rows while it moves; parked mid-plan (paused, charging break) one row every 10 s is plenty.
        every = (10 if self.activity in HELD and not is_moving(t) else 0.9) if self.run_id else self.cfg.poll_idle_s
        due = ts - self.last_row_at >= every
        if due:
            row = {k: v for k, v in t.items() if k != "cell_temps"}
            row["ts"], row["run_id"] = ts, self.run_id
            self.store.add_telemetry(row)
            self.last_row_at = ts
        if full and ts - self.last_raw_at >= self.cfg.raw_every_s:
            self.store.add_raw(ts, self.snapshot)
            self.last_raw_at = ts

    def _note_pose(self, t: dict, ts: float, rt: float | None) -> None:
        """Keep a position sample for the 3D robot and Follow (/api/stream's pose events). After a long dropout the
        robot sends a backlog of old poses mixed with new ones, so a sample not newer by the robot's own clock
        (rt: this message's own odom_ts, not the merged snapshot's) than the newest kept is dropped: drawn, it
        would send the robot back on a detour. The same time again is the same pose. An older one is taken after
        POSE_RESET_S without anything newer (the robot's clock was set back). Samples without a time are kept in
        arrival order."""
        if t.get("lon") is None or t.get("lat") is None:
            return
        last = self._pose_rt
        if rt is not None and last is not None and (rt == last or rt < last and ts - self._pose_at < POSE_RESET_S):
            return
        if rt is not None:
            self._pose_rt = rt
        self._pose_at = ts
        self.pose_seq += 1
        sp = [v for v in (t.get("speed_l"), t.get("speed_r")) if isinstance(v, (int, float))]
        self.poses.append({"seq": self.pose_seq, "t": rt, "lon": t["lon"], "lat": t["lat"],
                           "heading": telemetry.heading_deg(t.get("phi")), "pitch": t.get("pitch"),
                           "roll": t.get("roll"), "speed": round(sum(sp) / len(sp), 3) if sp else None})

    def poses_since(self, seq: int) -> list[dict]:
        """The kept position samples after `seq` (pose_seq when the caller last looked), oldest first."""
        return [p for p in self.poses if p["seq"] > seq]

    # -- "record an app edit": learn a command by doing it once in the Yarbo app ----------------------------------
    def _session_record(self, channel: str, name: str, data, ts: float) -> None:
        s = self.session
        if ts > s["until"]:
            return
        key = f"{channel}/{name}"
        s["counts"][key] = s["counts"].get(key, 0) + 1
        if (channel, name) in SESSION_COUNT_ONLY:
            return
        if channel == "device" and name == "data_feedback" and isinstance(data, dict):
            reply_to = data.get("topic")
            if reply_to in SESSION_SKIP_REPLIES and ts - self._sent.get(reply_to, 0) < 5:
                return                                  # our own read, answered
            key = f"reply/{reply_to}"
        if channel == "app" and self.own_echo(name, ts, data):
            return                                      # our own request
        if len(s["entries"]) >= SESSION_MAX_ENTRIES:
            s["dropped"] += 1
            return
        data = redact(data)
        text = json.dumps(data, default=str)
        s["entries"].append({"ts": ts, "key": key, "bytes": len(text),
                             "data": data if len(text) <= SESSION_MAX_BYTES else text[:SESSION_MAX_BYTES]})

    async def start_session(self, minutes: float = 30) -> dict:
        if self.session and not self.session.get("ended"):
            return self.session_view()
        now = time.time()
        before = await self.reload_map()
        params = {}
        for a in before.get("areas") or []:                 # the baseline the app's settings saves are compared with
            try:
                p = await self.read_area_params(a["id"])
            except Exception:
                p = None
            if p is not None:
                params[a["id"]] = p
        self.session = {"id": int(now), "started": now, "until": now + minutes * 60, "counts": {}, "entries": [],
                        "dropped": 0, "map_before": copy.deepcopy(before), "params_before": params}
        self.store.event("app_edit_recording", f"recording app commands and robot messages for {minutes:g} min")
        return self.session_view()

    async def stop_session(self) -> dict:
        s = self.session
        if not s or s.get("ended"):
            return self.session_view()
        s["ended"] = time.time()
        try:
            after = await self.reload_map()
        except Exception as exc:
            after, s["map_error"] = None, str(exc)
        s["map_after"] = copy.deepcopy(after)
        if after is not None and self.gps_ref:
            from . import mapstudio
            d = mapstudio.diff(s["map_before"], after, self.gps_ref)
            s["map_changes"] = [{"what": what, "kind": r["kind"], "id": r["id"], "name": r.get("name"),
                                 "changes": r.get("changes")} for what in ("added", "removed", "changed") for r in d[what]]
        path = Path(self.cfg.data_dir) / "captures" / f"app-edit-{s['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(s, default=str))
        s["file"] = str(path)
        self.store.event("app_edit_recorded", f"{len(s['entries'])} messages kept; "
                         f"{len(s.get('map_changes') or [])} map change(s); saved to {path.name}")
        return self.session_view()

    def session_view(self) -> dict | None:
        s = self.session
        if not s:
            return None
        if not s.get("ended") and time.time() > s["until"] and self.loop:
            self.loop.create_task(self.stop_session())       # ran out of time
        keys: dict = {}
        for e in s["entries"]:
            keys[e["key"]] = keys.get(e["key"], 0) + 1
        from . import areasettings
        names = {a.get("id"): a.get("name") for a in (s.get("map_before") or {}).get("areas") or []}
        return {"id": s["id"], "started": s["started"], "until": s["until"], "ended": s.get("ended"),
                "kept": keys, "counted": s["counts"], "dropped": s["dropped"],
                "map_changes": s.get("map_changes"), "map_error": s.get("map_error"), "file": s.get("file"),
                "settings_changes": areasettings.settings_changes(s["entries"], s.get("params_before") or {}, names)}

    def _track_firmware(self, ts: float) -> None:
        """A component's version counts once the robot has reported it for FIRMWARE_SETTLE_S without a break: the
        chassis read 74.10.180 for ~15 s between two 2.1.46 readings (2026-10-18 11:54) and that was two
        firmware_changed events. A component never seen before is taken at once (nothing to compare with)."""
        pending = self._fw_pending
        for component, version in telemetry.firmware(self.snapshot).items():
            if self.firmware.get(component) == version:
                pending.pop(component, None)
                continue
            if component in self.firmware:
                first = pending.get(component)
                if first is None or first[0] != version:
                    pending[component] = (version, ts)
                    continue
                if ts - first[1] < FIRMWARE_SETTLE_S:
                    continue
                pending.pop(component)
                self.store.event("firmware_changed", f"{component}: {self.firmware[component]} -> {version}")
            self.store.firmware_add(component, version)
            self.firmware[component] = version

    def plan_active(self, ts: float | None = None) -> bool | None:
        """Is a plan in progress? The robot's flags decide; a "no plan working" reply that is newer than the last
        state change overrides a flag left set (planning_paused can outlive a plan); with no flags at all a recent
        reply decides."""
        ts = ts or time.time()
        flags = plan_flags_active(self.t)
        if self.plan is not None and not self.plan.active and self.plan_at >= self.activity_at:
            return False
        if flags is None and self.plan is not None and ts - self.plan_at < 3 * PLAN_POLL_S:
            return self.plan.active
        return flags

    def _track_run(self, ts: float, t: dict) -> None:
        act = activity(t)
        if act != self.activity:
            if self.activity_at and {act, self.activity} & {"paused", "charging_break"}:
                self.store.event("activity", f"{ACTIVITY_LABELS.get(self.activity, self.activity)} -> "
                                             f"{ACTIVITY_LABELS.get(act, act)}")
            self.activity, self.activity_at, self._plan_due = act, ts, True
        self._track_stall(ts, t, act)
        change = self.detector.update(ts, t, self.plan_active(ts))
        if change == "start":
            plan = telemetry.get(self.snapshot, "StateMSG.plan_msg") or None
            self.run_id = self.store.start_run(ts, t.get("head_type"), plan if isinstance(plan, str) else None)
            self.run_stats, self.run_started, self.run_plan, self._plan_due = RunStats(), ts, None, True
            self.store.event("run_started", f"run {self.run_id} ({telemetry.head_label(t.get('head_type'))})")
        if self.run_id is not None and self.run_stats is not None:
            if act not in HELD or is_moving(t):
                self.run_stats.add(t)
            elif t.get("error_code"):                      # parked: only an error still counts
                self.run_stats.max_error = max(self.run_stats.max_error, int(t["error_code"]))
        if change == "end" and self.run_id is not None:
            reason = end_reason(t, self.run_stats, self.run_plan, self.detector.paused_out)
            self.store.end_run(self.run_id, max(self.detector.last_moving, self.run_started), reason, self.run_stats)
            self.store.event("run_ended", f"run {self.run_id}: {reason}, {self.run_stats.distance_m:.0f} m")
            finished = self.run_id
            self.run_id, self.run_stats, self.run_plan = None, None, None
            try:
                self._finish_wifi_survey(finished)
            except Exception:
                log.exception("Wi-Fi survey wrap-up failed")
            if self.loop:
                self.loop.create_task(self.analyze_run(finished))

    def _track_stall(self, ts: float, t: dict, act: str) -> None:
        x, y = t.get("x"), t.get("y")
        if not progressing(t, act) or x is None or y is None:      # not on the dock either (it waits there)
            self._stall_anchor, self.stalled_s, self._stall_waiting, self._fix_back_at = None, 0.0, False, None
            return
        a, gap = self._stall_anchor, ts - self._stall_seen
        self._stall_seen = ts
        # Waiting for an RTK fix (waiting_for_fix) lasts until the fix has been back FIX_HOLD_S, so a fix that
        # flickers does not end the wait each time; then the clock restarts: the robot gets STALL_ALERT_S from then
        # to move before it counts as stuck.
        restart = False
        if waiting_for_fix(t):
            self._stall_waiting, self._fix_back_at = True, None
        elif self._stall_waiting:
            self._fix_back_at = self._fix_back_at or ts
            if ts - self._fix_back_at >= FIX_HOLD_S:
                self._stall_waiting, self._fix_back_at, restart = False, None, True
        if (a is None or gap > STALL_GAP_S or ((x - a[0]) ** 2 + (y - a[1]) ** 2) ** 0.5 > STALL_RADIUS_M
                or restart):
            self._stall_anchor, self.stalled_s = (x, y, ts), 0.0
        else:
            self.stalled_s = ts - a[2]

    def _keep_plan_reply(self, data: dict, ts: float) -> None:
        """The run's first full plan reply, to learn the robot's own plan from; again when the kept one carried no
        paths or was of another plan session and this one has paths (run 6's first reply was the week before's
        finished plan), at most PLAN_REPLIES_KEPT in all."""
        key = f"plan_feedback:{self.run_id}"
        kept = self.store.latest_version(key)
        old = (kept or {}).get("data") or {}
        if kept is None or (data.get("cleanPathProgress") and len(self.store.versions(key)) < PLAN_REPLIES_KEPT and (
                not old.get("cleanPathProgress") or planned_mod.reply_session(old) != planned_mod.reply_session(data))):
            self.store.save_version(key, {"ts": ts, "data": data})

    def _keep_plan_paths(self, data: dict, ts: float) -> None:
        """The robot's path for each pass of each area of the plan (the edge, type 1, and the fill, type 0: see
        app/planned.py), the first time a reply carries it this run (run 4's first reply had none: the path appears
        once the robot starts the area), with where the robot stood on it (clean_index) and the plan session. Kept
        per run, area and pass for planned-vs-driven. Until 2026-10-19 only an area's first entry was kept
        (plan_path:<run>:<area>), so run 7 was compared with 1.35 km of its 6.1 km plan."""
        session = planned_mod.reply_session(data)
        for item in data.get("cleanPathProgress") or []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                continue
            ptype = item["type"] if isinstance(item.get("type"), int) else None
            path = [p for p in item.get("path") or [] if isinstance(p, dict) and "x" in p and "y" in p]
            key = f"plan_path:{self.run_id}:{item['id']}:{ptype}"
            kept = self.store.latest_version(key) if len(path) >= 2 else {}
            if len(path) >= 2 and (kept is None or (session and kept.get("session") != session)):   # or a stale one
                self.store.save_version(key, {"ts": ts, "area_id": item["id"], "type": ptype, "session": session,
                                              "clean_index": item.get("clean_index"), "path": path})

    def _on_plan(self, plan: PlanStatus | None, ts: float) -> None:
        """A get_plan_feedback reply (ours or the phone app's). Tags the open run with its plan session, and joins it
        onto the previous run when that one was the same plan session (a resume after a pause or a charging break
        that outlasted the run, or a panel restart mid-plan), up to PLAN_JOIN_MAX_S after it. That was PAUSE_CAP_S,
        which a run closed by that very cap can never meet (its end is its last movement, PAUSE_CAP_S before it
        closed): run 6 (paused 2026-10-17 16:40) and its resume the next morning, run 7, stayed apart. Never on a reply
        that says the plan is (nearly) done: a new run's first reply can be the last plan's, finished (run 6's was the
        week before's), and must not pull the new run into that one."""
        if plan is None:
            return
        self.plan, self.plan_at = plan, ts
        if not plan.active or self.run_id is None or self.run_stats is None:
            return
        self.run_plan = plan
        self.store.set_run_plan(self.run_id, plan.session, plan.progress_pct)
        if not plan.session:
            return
        prev = self.store.previous_run(self.run_id)
        if (prev and prev.get("plan_session") == plan.session and prev.get("ended")
                and self.run_started - prev["ended"] < PLAN_JOIN_MAX_S and not plan.complete
                and prev.get("end_reason") != "plan finished"
                and (None in (prev.get("head_type"), (self.store.run(self.run_id) or {}).get("head_type"))
                     or prev.get("head_type") == (self.store.run(self.run_id) or {}).get("head_type"))
                and (prev.get("plan_progress_pct") is None or plan.progress_pct is None
                     or plan.progress_pct + 1 >= prev["plan_progress_pct"])):   # progress never goes back in a session
            merged_from = self.run_id
            merged = self.store.merge_runs(prev["id"], self.run_id, reopen=True, from_stats=self.run_stats)
            if merged:
                self.store.event("run_joined", f"run {self.run_id} continues run {prev['id']} (same plan session)")
                last_xy = self.run_stats.last_xy
                self.run_id, self.run_started = prev["id"], prev["started"]
                self.run_stats = RunStats.from_run(merged)
                self.run_stats.last_xy = last_xy
                self._join_survey(prev["id"], merged_from)

    # -- the robot connection (Settings → Robot, else the environment) -----------------------------------------
    async def apply_robot(self) -> dict:
        """(Re)connect to the robot's local broker at the address in effect, finding the serial from heart_beat when
        none is configured. Safe to call again: the previous connection is dropped first."""
        async with self._robot_lock:
            host, port, sn = self.cfg.yarbo_host, self.cfg.yarbo_port, self.cfg.yarbo_sn
            old, self.transport = self.transport, None
            if old:
                await asyncio.to_thread(old.stop)        # joins paho's thread: not on the loop
            self._discover_at = time.time()
            if getattr(self.cfg, "demo", False):
                return self._apply_demo_robot()
            if not host:
                if sn and sn != self.sn:                 # cloud-only: the cloud client still needs the serial
                    self.sn = sn
                    if self.on_sn:
                        self.on_sn(sn)
                self.robot_error = "no robot address yet: set it in Settings → Robot"
                return self.local_status()
            if not sn and self.sn and self._applied == (host, port):
                sn = self.sn                             # found earlier on this very broker
            if not sn:
                try:
                    sn = await asyncio.to_thread(discover_sn, host, port, 20)
                except Exception as exc:
                    self.robot_error = f"no robot found at {host}:{port} ({exc})"
                    log.warning(self.robot_error)
                    return self.local_status()
            self._applied = (host, port)
            if sn != self.sn:
                self.sn = sn
                if self.on_sn:
                    self.on_sn(sn)
            self.transport = LocalTransport(host, port, sn, self._from_thread)
            self.transport.start()
            self.robot_error = ""
            self.store.event("recorder_started", f"robot {sn} at {host}:{port}")
            return self.local_status()

    def _apply_demo_robot(self) -> dict:
        """DEMO=1: the made-up robot (app/demo_robot.py) instead of any broker. It carries on the mow the demo history
        left in progress (app/demo.py)."""
        sim, now = self.store.setting("demo_sim") or {}, time.time()
        at = sim.get("at", now)
        robot = demo_robot.DemoRobot(at, plan_id=sim.get("plan_id", 1), progress=sim.get("progress", 0.4),
                                     session_start=sim.get("session_start"),
                                     speed=getattr(self.cfg, "demo_speed", 1.0) or 1.0)
        robot.advance(now)
        if self.sn != demo_robot.DEMO_SN:
            self.sn = demo_robot.DEMO_SN
            if self.on_sn:
                self.on_sn(self.sn)
        self.transport = demo_robot.DemoTransport(self.sn, self._from_thread, robot)
        self.transport.start()
        self.robot_error = ""
        self.store.event("recorder_started", f"demo robot {self.sn} (synthetic yard)")
        return self.local_status()

    def local_status(self) -> dict:
        t, now = self.transport, time.time()
        host = self.cfg.yarbo_host
        return {
            "connected": bool(t and t.connected),
            "host": "demo robot" if getattr(self.cfg, "demo", False) else f"{host}:{self.cfg.yarbo_port}" if host else None,
            "last_message_age_s": round(now - t.last_message_at, 1) if t and t.last_message_at else None,
            "received": t.received if t else 0,
            "read_requests_sent": t.published if t else 0,
            "commands_sent": getattr(t, "commands_sent", 0) if t else 0,
            "error": self.robot_error or (t.last_error if t else "") or ("connecting" if t and not t.connected else ""),
        }

    async def apply_weather(self) -> None:
        """Listen for the Tempest hub on the UDP port in effect (0 or unset = off); restarts the listener on a change."""
        if self.tempest:
            self.tempest.stop()
            self.tempest = None
        port = self.cfg.tempest_port
        if port:
            self.tempest = TempestListener(int(port), self._on_weather, self._on_weather_event,
                                           self.weather_config()["station"])
            await self.tempest.start()

    def weather_config(self) -> dict:
        return weather_mod.config(self.store.setting("weather_sources"))

    def tempest_age_s(self) -> float | None:
        t = self.tempest
        return time.time() - t.last_obs_at if t and t.last_obs_at else None

    def apply_weather_sources(self) -> None:
        """A changed station pick applies at once; source choices are read on every row."""
        if self.tempest:
            self.tempest.station = self.weather_config()["station"]
        self.evaluate_conditions()

    def apply_panel(self) -> None:
        self.detector.idle_end_s = self.cfg.run_idle_end_s

    # -- Wi-Fi survey: which networks the robot hears where, for the Signal map ----------------------------------
    def wifi_mode(self) -> str:
        mode = self.store.setting("wifi_survey", "off")
        return mode if mode in WIFI_MODES else "off"

    def _surveying(self) -> bool:
        """A run is open, the survey is on and the robot is not parked (a pause or charging break mid-plan)."""
        return (self.run_id is not None and self.wifi_mode() != "off"
                and not (self.activity in HELD and not is_moving(self.t)))

    def _robot_heard(self, ts: float) -> None:
        """Any message from the robot. During a survey, a pause longer than GAP_S is counted, split by whether it
        began within WIFI_NEAR_S of an ask (the scan's doing?) or not (the yard's normal), so the test run compares
        the two itself."""
        last, self._robot_at = self._robot_at, max(self._robot_at, ts)
        s = self.wifi_survey
        if s is None or not last or ts - last <= GAP_S or not self._surveying():
            return
        gap = ts - last
        near = 0 <= last - self._wifi_asked <= WIFI_NEAR_S
        k = "near" if near else "other"
        s[f"gaps_{k}"] += 1
        s[f"gap_s_{k}"] += gap
        s[f"gap_max_{k}"] = max(s[f"gap_max_{k}"], gap)

    def _poll_wifi(self, now: float) -> None:
        """While a surveyed run moves: ask for the Wi-Fi list every WIFI_POLL_S, never with an ask still out (a slow
        robot is not given more scans to do), and count the moving time the test is judged on."""
        if not self._surveying():
            return
        s = self.wifi_survey
        if s is None:
            s = self.wifi_survey = {"asked": 0, "answered": 0, "unanswered": 0, "late": 0, "repeats": 0,
                                    "reply_s": [], "scans": 0,
                                    "moving_s": 0, "_tick": now, "gaps_near": 0, "gap_s_near": 0.0,
                                    "gap_max_near": 0.0, "gaps_other": 0, "gap_s_other": 0.0, "gap_max_other": 0.0}
        s["moving_s"] += min(2.0, max(0.0, now - s["_tick"]))
        s["_tick"] = now
        if now - s.get("_saved", 0) >= WIFI_SAVE_S:
            s["_saved"] = now
            self._save_survey()
        w = self._wifi_waiting
        if w is not None and now - w[0] >= WIFI_TIMEOUT_S:
            s["unanswered"] += 1
            self._wifi_waiting = w = None
        # never within WIFI_DUP_S of the last answer: anything arriving then is that answer's repeat, not ours
        if (w is None and self.transport and self.transport.connected and now - self._wifi_asked >= WIFI_POLL_S
                and now - self._wifi_answer_at >= WIFI_DUP_S):
            try:
                self._sent["get_wifi_list"] = self._wifi_asked = now
                self.transport.request("get_wifi_list")
                self._wifi_waiting = (now, self.t.get("x"), self.t.get("y"))
                s["asked"] += 1
            except Exception as exc:
                log.warning("Wi-Fi list request failed: %s", exc)

    def _on_wifi(self, data: dict, ts: float) -> None:
        """A get_wifi_list answer during a survey. Only an answer to our own ask (no other client's ask in between),
        within WIFI_FRESH_S, is stored, at the middle of the scan; the rest is only counted. Off keeps nothing. The
        same name heard twice (several access points or bands) keeps its strongest signal."""
        s, w = self.wifi_survey, self._wifi_waiting
        if s is None:
            return
        if ts - self._wifi_answer_at < WIFI_DUP_S:
            s["repeats"] += 1
            return
        self._wifi_answer_at = ts
        ours = w is not None and self._wifi_foreign_at < w[0]
        if w is not None:
            self._wifi_waiting = None
            if ours:
                s["answered"] += 1
                s["reply_s"].append(ts - w[0])
        if not ours or not self._surveying():
            return
        if ts - w[0] > WIFI_FRESH_S:
            s["late"] += 1
            return
        x, y = self.t.get("x"), self.t.get("y")
        if data.get("state") != 0 or x is None or y is None or ts - self.update_at > 10:
            return
        if w[1] is not None and w[2] is not None:
            x, y = (x + w[1]) / 2, (y + w[2]) / 2
        body = decode_any(data.get("data"))
        items = body.get("wifi_list") if isinstance(body, dict) else body
        if not isinstance(items, list):
            return
        best: dict[str, tuple[float, bool]] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("wifi_name") or "").strip()
            try:
                signal = float(it.get("signal"))
            except (TypeError, ValueError):
                continue
            if not name or not math.isfinite(signal):
                continue
            signal = max(0.0, min(100.0, signal))
            saved = it.get("saved") in (True, 1, "1", "true", "True") or best.get(name, (0.0, False))[1]
            best[name] = (max(signal, best.get(name, (0.0, False))[0]), saved)
        self.store.add_wifi_scan(ts, self.run_id, x, y, [(n, v, sv) for n, (v, sv) in best.items()])
        s["scans"] += 1

    def _save_survey(self) -> None:
        """Keep the open run's survey counters in the database (they were in memory only: run 4's verdict covered
        the 17 min after its last restart)."""
        if self.wifi_survey is not None and self.run_id is not None:
            self.store.set_setting(f"wifi_survey_run:{self.run_id}",
                                   {k: v for k, v in self.wifi_survey.items() if not k.startswith("_")})

    @staticmethod
    def _add_survey(a: dict | None, b: dict | None) -> dict | None:
        """Two pieces of one run's survey counters as one: counts and times add up, the longest pause is the longer."""
        if not a or not b:
            return dict(b or a) if (a or b) else None
        out = dict(a)
        for k, v in b.items():
            if k.startswith("_"):
                out[k] = v
            elif k.startswith("gap_max"):
                out[k] = max(a.get(k, 0), v)
            elif isinstance(v, list):
                out[k] = list(a.get(k) or []) + v
            elif isinstance(v, (int, float)):
                out[k] = a.get(k, 0) + v
        return out

    def _join_survey(self, prev_id: int, cur_id: int) -> None:
        """A run joined onto the earlier piece of its plan session: its survey carries on from that piece's."""
        before = self.store.setting(f"wifi_survey_run:{prev_id}")
        if before:
            live = self.wifi_survey or self.store.setting(f"wifi_survey_run:{cur_id}")
            self.wifi_survey = self._add_survey(before, live)
            if self.wifi_survey is not None:
                self.wifi_survey.setdefault("_tick", time.time())
        self.store.delete_setting(f"wifi_survey_run:{cur_id}")
        self._save_survey()

    def _finish_wifi_survey(self, run_id: int) -> None:
        """At the end of a surveyed run: one event with how the scanning went (the whole run, across restarts). A
        "next run" survey then switches itself off, unless the run moved too briefly to tell anything."""
        s, self.wifi_survey, self._wifi_waiting = self.wifi_survey, None, None
        if s is None:
            s = self.store.setting(f"wifi_survey_run:{run_id}")
        self.store.delete_setting(f"wifi_survey_run:{run_id}")
        if s is None:
            return
        med = statistics.median(s["reply_s"]) if s["reply_s"] else None
        pauses = lambda k: (f"{s[f'gaps_{k}']} (longest {s[f'gap_max_{k}']:.1f} s)" if s[f"gaps_{k}"] else "none")
        detail = (f"run {run_id}, {round(s['moving_s'] / 60)} min moving: asked {s['asked']} times, "
                  f"{s['answered']} answers" + (f" (median {med:.1f} s, slowest {max(s['reply_s']):.0f} s)" if med is not None else "")
                  + (f", {s['unanswered']} unanswered" if s["unanswered"] else "")
                  + (f", {s['late']} too late to place" if s["late"] else "")
                  + (f", {s['repeats']} repeated" if s["repeats"] else "")
                  + f", {s['scans']} scans stored; pauses in the robot's messages over {GAP_S} s: "
                    f"within {WIFI_NEAR_S} s of a scan {pauses('near')}, otherwise {pauses('other')}")
        if self.wifi_mode() == "next":
            if s["moving_s"] >= WIFI_TEST_MIN_S:
                self.store.set_setting("wifi_survey", "off")
                detail += "; test run done, survey switched off"
            else:
                detail += f"; under {WIFI_TEST_MIN_S // 60} min of driving, so the test waits for the next run"
        self.store.event("wifi_survey", detail)

    # -- loops -------------------------------------------------------------------------------------------------
    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(1)
            now = time.time()
            if not self.transport:
                # The robot was off or unset at start: look for it again every minute until it answers.
                if self.cfg.yarbo_host and now - self._discover_at > 60 and not self._robot_lock.locked():
                    self._robot_task = asyncio.create_task(self.apply_robot())
                    self._robot_task.add_done_callback(_log_failure)
                if not self.can_read():
                    continue
            readable = self.can_read()
            if readable and (self.run_id or self._plan_due or plan_flags_active(self.t)):
                if now - self._plan_asked > (5 if self._plan_due else PLAN_POLL_S):
                    self._plan_asked, self._plan_due = now, False
                    try:
                        self._sent["get_plan_feedback"] = now
                        self._send_read("get_plan_feedback")
                    except Exception as exc:
                        log.warning("plan status request failed: %s", exc)
            if plan_flags_active(self.t) and self._plan_asked and now - max(self.plan_at, self.activity_at) > 300:
                if not self._plan_silent:
                    self._plan_silent = True
                    self.store.event("plan_status_silent", "the robot's flags say a plan is in progress but it has "
                                     "not answered a plan-status request for 5 min; the run is held open meanwhile")
            elif self.plan_at > self.activity_at:
                self._plan_silent = False
            try:
                self._poll_wifi(now)
            except Exception:
                log.exception("Wi-Fi survey poll failed")
            interval = self.cfg.poll_active_s if self.run_id else self.cfg.poll_idle_s
            if (readable and now - self.snapshot_at > interval
                    and now - self.last_poll_at > max(1.0, interval / 2)):
                self.last_poll_at = now
                try:
                    self._sent["get_device_msg"] = now
                    self._send_read("get_device_msg")
                except Exception as exc:
                    log.warning("poll failed: %s", exc)

    async def _refresh_loop(self) -> None:
        while True:
            if self.can_read():
                try:
                    await self.refresh_map()
                    await asyncio.sleep(self.cfg.map_refresh_s)
                    continue
                except Exception as exc:
                    log.warning("map refresh failed: %s", exc)
            await asyncio.sleep(15)

    async def refresh_map(self) -> None:
        ref = await self.request("read_gps_ref")
        if isinstance(ref.get("data"), dict):
            self._set_ref(ref["data"])
            self.store.save_version("gps_ref", ref["data"])
        reply = await self.request("get_map", timeout=30)
        raw = decode_map(reply.get("data"))
        changed = self.store.save_version("map", raw)
        if changed and self.map_raw is not None:
            self.store.event("map_changed", "the robot's map differs from the last stored version")
        self.map_raw = raw
        self.map_geojson = geo.map_to_geojson(raw, self.gps_ref)
        self.map_fetched = time.time()
        plans = await self.request("read_all_plan")
        if isinstance(plans.get("data"), dict):
            self.plans = plans["data"].get("data") or []
            self.store.save_version("plans", plans["data"])
        try:
            cfd = await self.request("read_low_cfd_config")
            if isinstance(cfd.get("data"), dict):
                self.cfd = cfd["data"]
                self.store.save_version("cfd", cfd["data"])
        except asyncio.TimeoutError:
            log.info("read_low_cfd_config: no reply")
        try:
            found = scheduler.robot_schedules(await self.request("read_schedules"))
            if found is not None:
                before = self.robot_schedules
                self.robot_schedules = found
                if found and not before:
                    self.store.event("robot_schedule_seen", f"the robot has {len(found)} schedule(s) of its own (made "
                                     "in the Yarbo app); they start mows without the panel's run rules")
        except asyncio.TimeoutError:
            log.info("read_schedules: no reply")
        bbox = geo.bounds(self.map_geojson)
        if bbox:
            self.yard = await self.imagery.ensure_yard(geo.pad(bbox, 60))
        log.info("map refreshed: %d features, %d plans", len(self.map_geojson["features"]), len(self.plans))

    def _set_ref(self, data: dict) -> None:
        ref = data.get("ref") or {}
        if isinstance(ref.get("latitude"), (int, float)) and isinstance(ref.get("longitude"), (int, float)):
            self.gps_ref = (float(ref["latitude"]), float(ref["longitude"]))

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(3600)
            pruned = self.store.prune_raw(self.cfg.raw_keep_days)
            if pruned:
                log.info("pruned %d raw snapshots", pruned)
            self.store.prune_wifi(WIFI_KEEP_DAYS)

    # -- weather and conditions ------------------------------------------------------------------------------
    def _on_weather(self, row: dict) -> None:
        row = weather_mod.filter_tempest(row, self.weather_config(), 0.0)
        self.store.add_weather({k: row[k] for k in ("ts", "temp_c", "rh", "dew_c", "rain_mm", "precip_type",
                                                     "solar", "uv", "wind_avg", "wind_gust", "pressure",
                                                     "lightning", "battery_v") if k in row})
        self.evaluate_conditions()

    async def _ha_state(self, entity: str) -> dict | None:
        if not self.ha.enabled:
            raise RuntimeError("Home Assistant is not connected")
        r = await self.ha._client.get(f"{self.ha.url}/api/states/{entity}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def _ha_weather_loop(self) -> None:
        """Every minute: the HA sensors chosen in Settings → Lawn conditions, for the inputs they feed now."""
        while True:
            try:
                cfg = self.weather_config()
                if self.ha.enabled and any(cfg["ha"].get(k) for k in ("rain", "temp", "humidity", "soil")):
                    row = await self.ha_weather.read(cfg, self.tempest_age_s())
                    if row:
                        self.store.add_weather(row)
                        self.evaluate_conditions()
            except Exception:
                log.exception("HA weather read failed")
            await asyncio.sleep(60)

    def _on_weather_event(self, kind: str, msg: dict) -> None:
        if kind == "rain_started":
            self.store.event("rain_started", "Tempest reported the start of rain")
        self.evaluate_conditions()

    def conditions_config(self) -> ConditionsConfig:
        return ConditionsConfig.from_dict(self.store.setting("conditions"))

    def evaluate_conditions(self) -> dict | None:
        now = time.time()
        cfg = self.conditions_config()
        weather = self.store.weather_since(now - max(cfg.lookback_h, 14) * 3600)
        if not weather:
            self.conditions = None
            return None
        lat, lon = self.gps_ref if self.gps_ref else (None, None)
        wcfg = self.weather_config()
        age = self.tempest_age_s()
        rain_from = weather_mod.source_for(wcfg, "rain", age)
        last_rain = self.tempest.last_rain_start if self.tempest and rain_from == "tempest" else None
        self.conditions = evaluate(now, weather, last_rain, self.t.get("rain"), lat, lon, cfg, wcfg["units"])
        self.conditions["sources"] = {k: weather_mod.source_for(wcfg, k, age) for k in weather_mod.INPUTS}
        self.conditions["ha_weather"] = self.ha_weather.status()
        return self.conditions

    async def _conditions_loop(self) -> None:
        while True:
            try:
                self.evaluate_conditions()
            except Exception:
                log.exception("conditions evaluation failed")
            await asyncio.sleep(60)

    async def _ha_loop(self) -> None:
        while True:                          # keeps going even when HA is off: it can be switched on in Settings
            if self.ha.enabled:
                try:
                    runs = self.store.runs(1)
                    await self.ha.push(entities_from(self.status(), self.conditions, runs[0] if runs else None))
                except Exception:
                    log.exception("HA publish failed")
            await asyncio.sleep(self.ha.interval_s)

    # -- run analysis --------------------------------------------------------------------------------------------
    def areas_local(self) -> list[dict]:
        if not self.map_raw or not self.gps_ref:
            return []
        return [{"id": a.get("id"), "name": a.get("name") or "", "poly": geo.feature_poly_local(a, self.gps_ref)}
                for a in self.map_raw.get("areas") or []]

    def nogos_local(self) -> list[list[tuple[float, float]]]:
        if not self.map_raw or not self.gps_ref:
            return []
        return [geo.feature_poly_local(z, self.gps_ref) for z in self.map_raw.get("nogozones") or []
                if z.get("enable", True)]

    def edge_index(self) -> EdgeIndex | None:
        """Area and no-go boundaries for telling edge passes from fill; rebuilt when the map changes."""
        src = self._edge_src
        if src is None or src[0] is not self.map_raw or src[1] != self.gps_ref:   # identity, not id(): ids get reused
            rings = [a["poly"] for a in self.areas_local()] + self.nogos_local()
            self._edge_index, self._edge_src = (EdgeIndex(rings) if rings else None), (self.map_raw, self.gps_ref)
        return self._edge_index

    def edge_labels(self, rows: list, xy, planning, open_end: bool) -> list:
        """Edge labels for `rows` (context first, then the new batch). Driving between areas or planning a route is
        never an edge pass, even alongside a boundary. Pure CPU: callers run it off the event loop."""
        idx = self.edge_index()
        if idx is None:
            return [None] * len(rows)
        labels = idx.classify([(xy(r) if None not in xy(r) else None) for r in rows], open_end=open_end)
        return [0 if (lab and planning(r) in (2, 3)) else lab for r, lab in zip(rows, labels)]

    async def signal_cells(self, metric: str, days: float) -> list[dict]:
        """Cached per metric until a run row is added; the grid itself is built off the event loop."""
        if not self.gps_ref:
            return []
        since = time.time() - days * 86400
        n, last = self.store.signal_stamp(since)
        key = (metric, round(days, 3), n, last, self.gps_ref)
        if self._signal_cache.get(metric, (None,))[0] == key:
            return self._signal_cache[metric][1]
        points = self.store.signal_points(since)
        cells = self._rings(await asyncio.to_thread(coverage.signal_grid, points, metric))
        self._signal_cache[metric] = (key, cells)
        return cells

    async def wifi_cells(self, days: float, network: str | None) -> dict:
        """The Wi-Fi survey for the Signal map: the networks heard, and one network's signal per cell (by default
        the first in wifi_networks' order: a saved one, else the one heard best across the yard)."""
        since = time.time() - days * 86400
        stamp = (round(days, 3), *self.store.wifi_stamp(since))
        nets = self._signal_cache.get("wifi_nets")
        if nets is None or nets[0] != stamp:
            nets = self._signal_cache["wifi_nets"] = (stamp, self.store.wifi_networks(since))
        networks = nets[1]
        names = [n["name"] for n in networks]
        network = network if network in names else (names[0] if names else None)
        if not self.gps_ref or network is None:
            return {"networks": networks, "network": network, "cells": []}
        key = (*stamp, network, self.gps_ref)
        hit = self._signal_cache.get("wifi")
        if hit is None or hit[0] != key:
            points = self.store.wifi_points(since, network)
            cells = self._rings(await asyncio.to_thread(coverage.signal_grid, points, "wifi"))
            hit = self._signal_cache["wifi"] = (key, cells)
        return {"networks": networks, "network": network, "cells": hit[1]}

    def _rings(self, cells: list[dict]) -> list[dict]:
        """Each grid cell's outline in lon/lat, for the map."""
        for c in cells:
            x0, y0, s = c["i"] * c["cell_m"], c["j"] * c["cell_m"], c["cell_m"]
            c["ring"] = [geo.to_lonlat(self.gps_ref, x, y) for x, y in
                         ((x0, y0), (x0 + s, y0), (x0 + s, y0 + s), (x0, y0 + s), (x0, y0))]
        return cells

    async def problem_map(self, days: float, run_id: int | None = None, run_ids: set | None = None) -> dict:
        """Problem areas (app/problems.py) for the runs of the last `days`, or one run. Each run is summarised once
        (in a worker thread, on its own read-only connection) and reused until it gets new rows or runs are
        merged; only a run that is still recording is redone. One computation at a time: concurrent requests wait
        for it and then hit the cache."""
        if not self.gps_ref:
            return {"cells": [], "spots": [], "events": []}
        async with self._problem_lock:
            since = math.floor((time.time() - days * 86400) / 3600) * 3600     # an hourly window, not a sliding one
            stamps = self.store.run_stamps(since, run_id)
            if run_ids is not None:                 # only these runs (e.g. Insights: mows, not test drives)
                stamps = {k: v for k, v in stamps.items() if k in run_ids}
            summaries = []
            for rid, stamp in stamps.items():
                key = (*stamp, self.store.write_gen)
                hit = self._run_summaries.get(rid)
                if hit is None or hit[0] != key:
                    rows = await asyncio.to_thread(Store.read_run_rows, self.store.path, rid, problems.COLUMNS)
                    hit = (key, await asyncio.to_thread(problems.summarise_run, rows))
                    self._run_summaries[rid] = hit
                summaries.append(hit[1])
            if run_id is None and run_ids is None:  # forget runs that left the window (and merged-away ones)
                for rid in [r for r in self._run_summaries if r not in stamps]:
                    del self._run_summaries[rid]
            res = await asyncio.to_thread(problems.combine, summaries)
        ll = lambda x, y: geo.to_lonlat(self.gps_ref, x, y)
        for c in res["cells"]:
            x0, y0, s = c["i"] * c["cell_m"], c["j"] * c["cell_m"], c["cell_m"]
            c["ring"] = [ll(x, y) for x, y in ((x0, y0), (x0 + s, y0), (x0 + s, y0 + s), (x0, y0 + s), (x0, y0))]
        for p in res["spots"]:
            p["lon"], p["lat"] = ll(p["x"], p["y"])
        res["events"] = [{**e, **dict(zip(("lon", "lat"), ll(e["x"], e["y"])))} if e["x"] is not None else dict(e)
                         for e in res["events"]]
        return res

    def deck_m(self, head_type) -> float:
        override = self.store.setting("deck_m")
        if isinstance(override, (int, float)) and override > 0:
            return float(override)
        return coverage.DEFAULT_DECK_M.get(int(head_type) if head_type is not None else -1, 0.53)

    async def analyze_run(self, run_id: int, force: bool = False) -> dict | None:
        """Coverage and gaps of a run (cached once it has ended). Earlier runs of the same plan session count as
        mowed already (coverage.analyze `before`)."""
        cached = None if force else self.store.analysis_get(run_id)
        if cached and cached.get("v") == coverage.VERSION:
            return cached
        run = self.store.run(run_id)
        if not run or run_id in self._analysing or not self.gps_ref:
            return cached
        self._analysing.add(run_id)
        try:
            points = self.store.run_points(run_id)
            earlier = self.store.earlier_session_runs(run_id)
            before = [self.store.run_points(e["id"]) for e in earlier]
            result = await asyncio.to_thread(coverage.analyze, points, self.areas_local(), self.nogos_local(),
                                             self.deck_m(run.get("head_type")), before=before)
            result["earlier_runs"] = [e["id"] for e in earlier]
            for area in result["areas"]:
                for gap in area["gaps"]:
                    gap["hull_lonlat"] = [geo.to_lonlat(self.gps_ref, x, y) for x, y in gap["hull"]]
                    gap["centroid_lonlat"] = geo.to_lonlat(self.gps_ref, *gap["centroid"])
            now_run = self.store.run(run_id)       # re-read: a resume may have reopened it meanwhile
            if now_run and now_run.get("ended"):
                self.store.analysis_set(run_id, result)
            return result
        finally:
            self._analysing.discard(run_id)

    # -- views -------------------------------------------------------------------------------------------------
    def status(self) -> dict:
        now = time.time()
        t = self.t
        error = int(t.get("error_code") or 0)
        act = self.activity if self.snapshot else None
        if not self.snapshot:
            state = "waiting for data"
        elif error:
            state = "error"
        elif act == "idle" and t.get("working_state") == 1:
            state = "awake"
        else:
            state = ACTIVITY_LABELS.get(act, act)
        pp = int(t.get("planning_paused") or 0)
        pause_reason = telemetry.PAUSE_REASONS.get(pp, f"reason {pp}") if act == "paused" and pp not in (0, 1) else None
        plan_error = telemetry.code_label(telemetry.PLAN_ERRORS, t.get("planning"))
        recharge_error = telemetry.code_label(telemetry.RECHARGE_ERRORS, t.get("recharging"))
        plan = self.plan if self.plan and now - self.plan_at < 10 * PLAN_POLL_S else None
        run = None
        if self.run_id and self.run_stats:
            rp = self.run_plan
            run = {"id": self.run_id, "started": self.run_started,
                   "distance_m": round(self.run_stats.distance_m, 1), "points": self.run_stats.points,
                   "held_since": self.detector.held_since, "plan": rp.view() if rp else None}
        return {
            "now": now,
            "sn": self.sn,
            "state": state,
            "activity": act,
            "pause_reason": pause_reason,     # the state stays "paused"; why, when the robot paused itself
            "plan_error": plan_error,
            "recharge_error": recharge_error,
            "activity_since": self.activity_at or None,
            **self.trouble(now),
            "plan": {**plan.view(), "age_s": round(now - self.plan_at)} if plan else None,
            "snapshot_age_s": round(now - self.snapshot_at, 1) if self.snapshot_at else None,
            "heartbeat_age_s": round(now - self.heartbeat_at, 1) if self.heartbeat_at else None,
            "transport": {
                "local": {
                    **self.local_status(),
                },
                "cloud": {"note": "see ops.cloud in /api/status"},
            },
            "robot": {
                "battery": t.get("battery"),
                "battery_health": t.get("battery_health"),
                "voltage_v": round(t["voltage_mv"] / 1000, 2) if t.get("voltage_mv") else None,
                "current_a": round(t["current_ma"] / 1000, 2) if t.get("current_ma") is not None else None,
                "charging_status": t.get("charging"),
                "working_state": t.get("working_state"),
                "error_code": error,
                "head": telemetry.head_label(t.get("head_type")),
                "head_type": t.get("head_type"),
                "position": {"lon": t.get("lon"), "lat": t.get("lat"), "x": t.get("x"), "y": t.get("y"),
                             "heading": telemetry.heading_deg(t.get("phi")), "t": t.get("odom_ts")},
                "rtk": {"status": t.get("rtk_status"), "label": telemetry.rtk_label(t.get("rtk_status")),
                        "sats": t.get("sats"), "heading_dop": t.get("heading_dop"), "rtcm_age": t.get("rtcm_age"),
                        "confidence": t.get("odom_conf"), "shared_sats": t.get("heading_multi")},
                "halow_dbm": t.get("halow"),
                "cell_temps": t.get("cell_temps") or [],
                "pitch": t.get("pitch"),
                "roll": t.get("roll"),
                "rain_sensor": t.get("rain"),
                "cpu_temp": t.get("cpu_temp"),
                "blade": {"rpm": [t.get("blade_rpm_l"), t.get("blade_rpm_r")],
                          "temp": [t.get("blade_temp_l"), t.get("blade_temp_r")]},
                "speed": [t.get("speed_l"), t.get("speed_r")],
            },
            "run": run,
            "firmware": self.firmware,
            "map": {"features": len(self.map_geojson["features"]) if self.map_geojson else 0,
                    "fetched": self.map_fetched or None, "plans": len(self.plans)},
            "uptime_s": round(now - self.started_at),
            "conditions": {"state": self.conditions["state"], "dry_by": self.conditions["dry_by"]}
            if self.conditions else None,
            "tempest": self.tempest.status() if self.tempest else None,
            "ha": self.ha.status(),
            "demo": bool(getattr(self.cfg, "demo", False)),
        }

    def trouble(self, now: float | None = None) -> dict:
        """Stuck / lost-contact signals, shared by the status payload and the alerts so they always agree. Standing
        still without an RTK fix and with the blades stopped is the robot waiting for its fix, not stuck
        (rtk_wait): it did that 2026-10-17 16:15-16:21 and 2026-10-18 08:54-09:05 on float, and drove on as soon
        as the fix came back. With the blades turning, or a fix, standing still is stuck."""
        now = now or time.time()
        fresh = self.update_at and now - self.update_at <= STALL_GAP_S
        stalled_s = round(self.stalled_s) if fresh else 0
        long_stop = stalled_s >= STALL_ALERT_S
        rtk_wait = long_stop and self._stall_waiting
        return {"stalled_s": stalled_s, "stalled": long_stop and not rtk_wait, "rtk_wait": rtk_wait,
                "lost_contact": bool(self.run_id) and bool(self.update_at) and now - self.update_at > LOST_CONTACT_S,
                "data_age_s": round(now - self.update_at, 1) if self.update_at else None}

    def snapshot_copy(self) -> dict:
        return copy.deepcopy(self.snapshot)
