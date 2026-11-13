"""SQLite storage (one file in DATA_DIR). Used only from the asyncio thread.

Schema changes are additive: missing tables are created and missing telemetry/weather columns are added on start,
so an existing database upgrades in place.
"""
import hashlib
import json
import logging
import sqlite3
import time
import zlib
from pathlib import Path

TELEMETRY_COLS = [
    "ts", "run_id", "x", "y", "lon", "lat", "phi", "rtk_status", "sats", "heading_dop", "rtcm_age",
    "battery", "battery_health", "voltage_mv", "current_ma", "charging", "working_state", "planning",
    "error_code", "stuck", "obstacle", "speed_l", "speed_r", "wheel_cur_l", "wheel_cur_r", "pitch", "roll",
    "halow", "rain", "head_type", "chute_angle", "blade_rpm_l", "blade_rpm_r", "blade_cur_l", "blade_cur_r",
    "blade_temp_l", "blade_temp_r", "cell_temp_max", "cpu_temp", "odo_l", "odo_r",
    "planning_paused", "car_controller", "recharging", "odom_conf", "heading_obs", "heading_multi", "hgt",
    "odom_ts",          # the robot's own time for the pose: rows are stamped (ts) when they arrive, which bunches up
]                       # after a link gap; order and speeds use odom_ts where it is there
log = logging.getLogger(__name__)
# What working a run's totals out again reads (Store.recompute_run): positions, pose time, fix, battery, error and
# the state flags that say whether it was parked.
RECOMPUTE_COLS = ("ts", "x", "y", "odom_ts", "rtk_status", "battery", "error_code", "speed_l", "speed_r", "planning",
                  "planning_paused", "recharging", "charging", "working_state", "car_controller")
# Columns added to runs after the first release (name -> SQL type).
RUN_EXTRA_COLS = {"plan_session": "TEXT", "plan_progress_pct": "REAL", "working_s": "REAL"}

WEATHER_COLS = [
    "ts", "temp_c", "rh", "dew_c", "rain_mm", "precip_type", "solar", "uv", "wind_avg", "wind_gust", "pressure",
    "lightning", "battery_v", "soil_pct",
]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS telemetry ({", ".join(c + (" INTEGER" if c == "run_id" else " REAL") for c in TELEMETRY_COLS)});
CREATE INDEX IF NOT EXISTS telemetry_ts ON telemetry(ts);
CREATE INDEX IF NOT EXISTS telemetry_run ON telemetry(run_id);
CREATE INDEX IF NOT EXISTS telemetry_run_ts ON telemetry(run_id, ts);
CREATE TABLE IF NOT EXISTS snapshot_raw (ts REAL PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started REAL NOT NULL, ended REAL, head_type INTEGER, plan TEXT, end_reason TEXT,
  distance_m REAL, points INTEGER, rtk_fixed_pct REAL, battery_start REAL, battery_end REAL, max_error INTEGER);
CREATE TABLE IF NOT EXISTS run_analysis (run_id INTEGER PRIMARY KEY, computed REAL NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, fetched REAL NOT NULL, sha TEXT NOT NULL, json TEXT NOT NULL,
  UNIQUE(kind, sha));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS firmware_seen (ts REAL NOT NULL, component TEXT NOT NULL, version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands_seen (
  name TEXT PRIMARY KEY, first REAL NOT NULL, last REAL NOT NULL, count INTEGER NOT NULL, sample TEXT);
CREATE TABLE IF NOT EXISTS weather ({", ".join(c + " REAL" for c in WEATHER_COLS)}, PRIMARY KEY (ts));
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS control_log (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor TEXT NOT NULL, route TEXT NOT NULL, cmd TEXT NOT NULL, args TEXT,
  sent INTEGER NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedule_log (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, window_id TEXT NOT NULL, date TEXT NOT NULL, outcome TEXT NOT NULL,
  detail TEXT);
CREATE TABLE IF NOT EXISTS captures (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wifi_seen (
  ts REAL NOT NULL, run_id INTEGER, x REAL NOT NULL, y REAL NOT NULL, name TEXT NOT NULL, signal REAL NOT NULL,
  saved INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS wifi_seen_ts ON wifi_seen(ts);
CREATE INDEX IF NOT EXISTS wifi_seen_run ON wifi_seen(run_id);
DELETE FROM events WHERE kind = 'command_seen';
DELETE FROM versions WHERE kind IN ('reply:get_wifi_list', 'reply:get_connect_wifi_name');
"""


class Store:
    def __init__(self, data_dir: str):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.path = str(Path(data_dir) / "yarbo.db")
        self.write_gen = 0          # bumped when stored runs are rewritten (merges), so caches keyed on runs notice
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._add_missing_columns("telemetry", TELEMETRY_COLS)
        self._add_missing_columns("weather", WEATHER_COLS)
        self.redact_stored()
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        for col, kind in RUN_EXTRA_COLS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} {kind}")

    def _add_missing_columns(self, table: str, cols: list[str]) -> None:
        have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")

    # -- telemetry -------------------------------------------------------------------------------------------
    def add_telemetry(self, row: dict) -> None:
        cols = [c for c in TELEMETRY_COLS if c in row]
        self.db.execute(f"INSERT INTO telemetry ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [row[c] for c in cols])

    def series(self, metric: str, since: float, max_points: int = 600) -> list[list[float]]:
        if metric not in TELEMETRY_COLS or metric in ("ts", "run_id"):
            raise ValueError(metric)
        rows = self.db.execute(f"SELECT ts, {metric} FROM telemetry WHERE ts >= ? AND {metric} IS NOT NULL "
                               "ORDER BY ts", (since,)).fetchall()
        step = max(1, len(rows) // max_points)
        return [[r[0], r[1]] for r in rows[::step]]

    def correction_rows(self, since: float) -> list[tuple]:
        """(ts, rtcm_age, rtk_status, halow, sats) since `since`, oldest first (app/corrections.py): only the late
        rows and the one after each, which is all the gap finder needs (30 days are a few hundred thousand rows)."""
        from .problems import RTCM_LATE_S
        q = ("SELECT ts, rtcm_age, rtk_status, halow, sats FROM (SELECT ts, rtcm_age, rtk_status, halow, sats, "
             "LAG(rtcm_age) OVER (ORDER BY ts) AS prev FROM telemetry WHERE ts >= ? AND rtcm_age IS NOT NULL) "
             "WHERE rtcm_age > ? OR prev > ? ORDER BY ts")
        return [tuple(r) for r in self.db.execute(q, (since, RTCM_LATE_S, RTCM_LATE_S))]

    def track(self, run_id: int | None = None, since: float | None = None) -> list[dict]:
        if run_id is not None:
            q, args = "SELECT ts, lon, lat, rtk_status FROM telemetry WHERE run_id = ? ORDER BY ts", (run_id,)
        else:
            q, args = "SELECT ts, lon, lat, rtk_status FROM telemetry WHERE ts >= ? ORDER BY ts", (since or 0,)
        return [dict(r) for r in self.db.execute(q, args).fetchall() if r["lon"] is not None]

    def run_points(self, run_id: int, since: float = 0) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM telemetry WHERE run_id = ? AND ts > ? ORDER BY ts",
                                                 (run_id, since))]

    def run_point_before(self, run_id: int, ts: float) -> dict | None:
        r = self.db.execute("SELECT * FROM telemetry WHERE run_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                            (run_id, ts)).fetchone()
        return dict(r) if r else None

    def track_rows(self, since: float) -> list[list]:
        """[ts, lon, lat, rtk_status, x, y] rows newer than `since`, for the live map (appended client-side)."""
        return [list(r) for r in self.db.execute(
            "SELECT ts, lon, lat, rtk_status, x, y, planning FROM telemetry WHERE ts > ? AND lon IS NOT NULL ORDER BY ts",
            (since,))]

    def track_rows_before(self, ts: float, n: int) -> list[list]:
        """The n rows up to and including ts (context for classifying the next batch), oldest first."""
        rows = self.db.execute("SELECT ts, lon, lat, rtk_status, x, y, planning FROM telemetry WHERE ts <= ? AND lon IS NOT NULL "
                               "ORDER BY ts DESC LIMIT ?", (ts, n)).fetchall()
        return [list(r) for r in reversed(rows)]

    def run_points_before(self, run_id: int, ts: float, n: int) -> list[dict]:
        rows = self.db.execute("SELECT * FROM telemetry WHERE run_id = ? AND ts <= ? AND lon IS NOT NULL "
                               "ORDER BY ts DESC LIMIT ?",
                               (run_id, ts, n)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def run_stamps(self, since: float, run_id: int | None = None) -> dict:
        """{run_id: (rows, last ts)} for one run, or every run that was still going at or after `since`."""
        ids = [run_id] if run_id is not None else [r[0] for r in self.db.execute(
            "SELECT id FROM runs WHERE COALESCE(ended, 1e18) >= ?", (since,))]
        out = {}
        for rid in ids:
            n, last = self.db.execute("SELECT COUNT(*), MAX(ts) FROM telemetry WHERE run_id = ?", (rid,)).fetchone()
            if n:
                out[rid] = (n, last)
        return out

    @staticmethod
    def read_run_rows(path: str, run_id: int, columns) -> list[dict]:
        """A run's rows on a private read-only connection (for worker threads; WAL lets it read alongside writes)."""
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            db.row_factory = sqlite3.Row
            have = {r["name"] for r in db.execute("PRAGMA table_info(telemetry)")}
            cols = ",".join(c for c in columns if c in have)
            return [dict(r) for r in db.execute(f"SELECT {cols} FROM telemetry WHERE run_id = ? ORDER BY ts", (run_id,))]
        finally:
            db.close()

    def signal_stamp(self, since: float) -> tuple:
        r = self.db.execute("SELECT COUNT(*), MAX(ts) FROM telemetry WHERE run_id IS NOT NULL AND ts >= ?", (since,)).fetchone()
        return r[0], r[1]

    def signal_points(self, since: float) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT x, y, halow, rtk_status, sats FROM telemetry WHERE run_id IS NOT NULL AND ts >= ? AND x IS NOT NULL",
            (since,))]

    # -- Wi-Fi survey: every network the robot heard during a run, and where (get_wifi_list answers) ------------
    def add_wifi_scan(self, ts: float, run_id: int | None, x: float, y: float,
                      networks: list[tuple[str, float, bool]]) -> None:
        """One scan. A scan that heard nothing is kept as one row named "" so the place still counts as silent."""
        rows = [(ts, run_id, x, y, name, signal, int(saved)) for name, signal, saved in networks] or \
               [(ts, run_id, x, y, "", 0.0, 0)]
        self.db.executemany("INSERT INTO wifi_seen (ts, run_id, x, y, name, signal, saved) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            rows)

    def prune_wifi(self, keep_days: float) -> int:
        return self.db.execute("DELETE FROM wifi_seen WHERE ts < ?", (time.time() - keep_days * 86400,)).rowcount

    def wifi_stamp(self, since: float) -> tuple:
        r = self.db.execute("SELECT COUNT(*), MAX(ts) FROM wifi_seen WHERE run_id IS NOT NULL AND ts >= ?", (since,)).fetchone()
        return r[0], r[1]

    def wifi_networks(self, since: float) -> list[dict]:
        """The networks heard during runs: saved ones (the robot would join them) first, then by signal summed over
        every scan, so a network heard all over the yard ranks above one heard strongly in a corner."""
        scans = self.db.execute("SELECT COUNT(DISTINCT ts) FROM wifi_seen WHERE run_id IS NOT NULL AND ts >= ?",
                                (since,)).fetchone()[0]
        rows = self.db.execute("SELECT name, COUNT(*) AS n, AVG(signal) AS avg, SUM(signal) AS total, MAX(saved) AS saved "
                               "FROM wifi_seen WHERE run_id IS NOT NULL AND ts >= ? AND name != '' GROUP BY name",
                               (since,)).fetchall()
        out = [{"name": r["name"], "heard_pct": round(100 * r["n"] / scans) if scans else 0, "avg": round(r["avg"]),
                "saved": bool(r["saved"]), "_rank": r["total"] / scans if scans else 0} for r in rows]
        out.sort(key=lambda n: (not n["saved"], -n["_rank"]))
        return [{k: v for k, v in n.items() if k != "_rank"} for n in out]

    def wifi_points(self, since: float, name: str) -> list[dict]:
        """One network's signal at every scan position; 0 where a scan did not hear it."""
        return [dict(r) for r in self.db.execute(
            "SELECT s.x AS x, s.y AS y, COALESCE(w.signal, 0) AS signal FROM "
            "(SELECT ts, MIN(x) AS x, MIN(y) AS y FROM wifi_seen WHERE run_id IS NOT NULL AND ts >= ? GROUP BY ts) s "
            "LEFT JOIN wifi_seen w ON w.ts = s.ts AND w.name = ?", (since, name))]

    def error_codes_seen(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT CAST(error_code AS INTEGER) AS code, COUNT(*) AS samples, MIN(ts) AS first, MAX(ts) AS last "
            "FROM telemetry WHERE error_code IS NOT NULL AND error_code != 0 GROUP BY 1 ORDER BY last DESC")]

    # -- raw snapshots ---------------------------------------------------------------------------------------
    def add_raw(self, ts: float, snapshot: dict) -> None:
        """Raw snapshots are ~10 KB of JSON; stored zlib-compressed (BLOB) to keep 90 days small."""
        blob = zlib.compress(json.dumps(snapshot, separators=(",", ":")).encode(), 6)
        self.db.execute("INSERT OR REPLACE INTO snapshot_raw (ts, json) VALUES (?, ?)", (ts, blob))

    def raw(self, ts_from: float, ts_to: float) -> list[tuple[float, dict]]:
        out = []
        for r in self.db.execute("SELECT ts, json FROM snapshot_raw WHERE ts BETWEEN ? AND ? ORDER BY ts",
                                 (ts_from, ts_to)):
            value = r["json"]
            out.append((r["ts"], json.loads(zlib.decompress(value) if isinstance(value, bytes) else value)))
        return out

    def height_points(self) -> tuple[list[tuple[float, float, float]], int]:
        """The robot's RTK-fixed heights while it worked, as (lon, lat, metres), and how many runs they come from.
        Telemetry has them from 2026-10-08 (column hgt); runs before that only in the raw snapshots (RTKMSG, one a
        minute, kept RAW_KEEP_DAYS).
        Kept between calls, since every look at the 3D view asks and a season of mowing is a lot of rows: telemetry
        is only ever added to (a merge moves rows between runs, never out of one), so while the runs and the kept
        snapshots are the same, only rows that arrived since the last call are read. Anything else reads it all
        again. The list is shared: callers must not change it."""
        key = (tuple(self.db.execute("SELECT COUNT(*), MAX(id), TOTAL(ended) FROM runs").fetchone()),
               self.db.execute("SELECT MIN(ts) FROM snapshot_raw").fetchone()[0])
        c = getattr(self, "_heights", None)
        if c is not None and c["key"] == key:
            new = self.db.execute(
                "SELECT ts, lon, lat, hgt, rtk_status FROM telemetry WHERE ts > ? AND hgt IS NOT NULL "
                "AND run_id IS NOT NULL AND lon IS NOT NULL ORDER BY ts", (c["ts"],)).fetchall()
            if new:
                c["pts"].extend((r[1], r[2], r[3]) for r in new if r[4] == 4)
                c["runs"].update(r[0] for r in self.db.execute(
                    "SELECT DISTINCT run_id FROM telemetry WHERE ts > ? AND hgt IS NOT NULL AND run_id IS NOT NULL",
                    (c["ts"],)))
                c["ts"] = new[-1][0]
            return c["pts"], len(c["runs"])
        pts, runs, last = self._read_heights()
        self._heights = {"key": key, "pts": pts, "runs": runs, "ts": last}
        return pts, len(runs)

    def _read_heights(self) -> tuple[list, set, float]:
        """All of `height_points` from the database: (points, run ids, the arrival time of the newest row)."""
        pts = [(r[0], r[1], r[2]) for r in self.db.execute(
            "SELECT lon, lat, hgt FROM telemetry WHERE hgt IS NOT NULL AND rtk_status = 4 AND run_id IS NOT NULL "
            "AND lon IS NOT NULL ORDER BY ts")]
        last = self.db.execute("SELECT MAX(ts) FROM telemetry").fetchone()[0]     # every row so far has been looked at
        last = float("-inf") if last is None else last
        runs = {r[0] for r in self.db.execute("SELECT DISTINCT run_id FROM telemetry WHERE hgt IS NOT NULL AND run_id IS NOT NULL")}
        first = self.db.execute("SELECT MIN(ts) FROM telemetry WHERE hgt IS NOT NULL").fetchone()[0]
        older: list = []                    # runs from before telemetry had heights (oldest first)
        for run in self.db.execute("SELECT id, started, ended FROM runs WHERE ended IS NOT NULL ORDER BY started").fetchall():
            if run[0] in runs or (first is not None and run[1] >= first):
                continue
            got = 0
            for _, snap in self.raw(run[1], run[2]):
                r = snap.get("RTKMSG") if isinstance(snap, dict) else None
                if isinstance(r, dict) and str(r.get("status")) == "4" and isinstance(r.get("hgt"), (int, float)) \
                        and isinstance(r.get("lan"), (int, float)) and isinstance(r.get("lon"), (int, float)) and r["lan"]:
                    older.append((float(r["lon"]), float(r["lan"]), float(r["hgt"])))
                    got += 1
            if got:
                runs.add(run[0])
        return older + pts, runs, last      # so a full read and a kept list topped up give the same order

    def prune_raw(self, keep_days: float) -> int:
        return self.db.execute("DELETE FROM snapshot_raw WHERE ts < ?", (time.time() - keep_days * 86400,)).rowcount

    # -- runs ------------------------------------------------------------------------------------------------
    def start_run(self, started: float, head_type, plan: str | None) -> int:
        cur = self.db.execute("INSERT INTO runs (started, head_type, plan) VALUES (?, ?, ?)",
                              (started, head_type, plan))
        return cur.lastrowid

    def end_run(self, run_id: int, ended: float, reason: str, stats) -> None:
        self.db.execute(
            "UPDATE runs SET ended=?, end_reason=?, distance_m=?, points=?, rtk_fixed_pct=?, battery_start=?, "
            "battery_end=?, max_error=? WHERE id=?",
            (ended, reason, round(stats.distance_m, 1), stats.points, stats.fixed_pct, stats.battery_start,
             stats.battery_end, stats.max_error, run_id))
        self.recompute_run(run_id)          # the stored rows are the whole run, the live counters maybe not

    def recompute_run(self, run_id: int):
        """Work a run's totals out again from its stored telemetry. The live counters (RunStats) are in memory and
        start again after a restart, so totals kept only from them lose everything since the last join (run 4:
        7.4 km recorded against ~8.5 km driven). Returns the RunStats, or None for a run without rows."""
        from .runs import RunStats, working_seconds
        try:
            rows = [dict(r) for r in self.db.execute(f"SELECT {', '.join(RECOMPUTE_COLS)} FROM telemetry "
                                                     "WHERE run_id = ? ORDER BY ts", (run_id,))]
            if not rows:
                return None
            working = working_seconds(rows)
            s = RunStats.from_rows(rows)
        except Exception:                   # best effort: a run's end, a join or the start must never fail on it
            log.exception("working out run %s's totals again failed", run_id)
            return None
        self.db.execute("UPDATE runs SET distance_m=?, points=?, rtk_fixed_pct=COALESCE(?, rtk_fixed_pct), "
                        "battery_start=COALESCE(?, battery_start), battery_end=COALESCE(?, battery_end), "
                        "max_error=MAX(?, COALESCE(max_error, 0)), working_s=? WHERE id=?",
                        (round(s.distance_m, 1), s.points, s.fixed_pct, s.battery_start, s.battery_end, s.max_error,
                         round(working), run_id))
        return s                            # the rows did not change, so caches keyed on write_gen stay valid

    def recompute_all_runs_once(self) -> int:
        """One time per version: every stored run's totals from its rows (v1 2026-10-11: runs cut by restarts; v2
        2026-10-28: working time, #62)."""
        if self.setting("runs_recomputed_v2"):
            return 0
        n = sum(1 for (rid,) in self.db.execute("SELECT id FROM runs WHERE ended IS NOT NULL").fetchall()
                if self.recompute_run(rid))
        self.set_setting("runs_recomputed_v2", True)
        return n

    def set_run_plan(self, run_id: int, session: str | None, progress_pct: float | None) -> None:
        self.db.execute("UPDATE runs SET plan_session = COALESCE(?, plan_session), "
                        "plan_progress_pct = COALESCE(?, plan_progress_pct) WHERE id = ?",
                        (session, progress_pct, run_id))

    def merge_runs(self, into_id: int, from_id: int, reopen: bool = False, from_stats=None) -> dict | None:
        """Join run `from_id` onto the earlier run `into_id` (one plan session that was recorded as two runs): its
        telemetry moves over, the totals add up and `from_id` is deleted. reopen=True leaves the result open (the
        recorder carries on recording into it); from_stats = the live RunStats of `from_id` when it is still being
        recorded (its row has no totals yet). Returns the merged run."""
        a, b = self.run(into_id), self.run(from_id)
        if not a or not b or into_id == from_id:
            return None
        if from_stats is not None:
            b.update(distance_m=round(from_stats.distance_m, 1), points=from_stats.points,
                     rtk_fixed_pct=from_stats.fixed_pct, battery_start=from_stats.battery_start,
                     battery_end=from_stats.battery_end, max_error=from_stats.max_error)
        if b["started"] < a["started"]:
            raise ValueError("merge into the earlier run")
        pa, pb = int(a.get("points") or 0), int(b.get("points") or 0)
        fixed = ((a.get("rtk_fixed_pct") or 0) * pa + (b.get("rtk_fixed_pct") or 0) * pb) / (pa + pb) if pa + pb else None
        battery_end = b["battery_end"] if b.get("battery_end") is not None else a.get("battery_end")
        self.db.execute("BEGIN")
        try:
            self.db.execute("UPDATE telemetry SET run_id = ? WHERE run_id = ?", (into_id, from_id))
            self.db.execute("UPDATE wifi_seen SET run_id = ? WHERE run_id = ?", (into_id, from_id))
            self.db.execute(
                "UPDATE runs SET ended=?, end_reason=?, distance_m=?, points=?, rtk_fixed_pct=?, battery_start=?, "
                "battery_end=?, max_error=?, plan_session=COALESCE(plan_session, ?), "
                "plan_progress_pct=COALESCE(?, plan_progress_pct) WHERE id=?",
                (None if reopen else b.get("ended"), None if reopen else b.get("end_reason"),
                 round((a.get("distance_m") or 0) + (b.get("distance_m") or 0), 1), pa + pb,
                 round(fixed, 1) if fixed is not None else None,
                 a["battery_start"] if a.get("battery_start") is not None else b.get("battery_start"), battery_end,
                 max(int(a.get("max_error") or 0), int(b.get("max_error") or 0)), b.get("plan_session"),
                 b.get("plan_progress_pct"), into_id))
            self.db.execute("DELETE FROM runs WHERE id = ?", (from_id,))
            # The robot's plan kept with the later piece goes with it (app/planned.py robot_plan reads it per run);
            # a copy into_id already has stays as it is.
            moving = (f"plan_feedback:{from_id}", f"plan\\_path:{from_id}:%")
            for vid, kind in self.db.execute("SELECT id, kind FROM versions WHERE kind = ? OR kind LIKE ? ESCAPE '\\'",
                                             moving).fetchall():
                bits = kind.split(":")
                bits[1] = str(into_id)                      # plan_feedback:<run>, plan_path:<run>:<area>[:<type>]
                self.db.execute("UPDATE OR IGNORE versions SET kind = ? WHERE id = ?", (":".join(bits), vid))
            self.db.execute("DELETE FROM versions WHERE kind = ? OR kind LIKE ? ESCAPE '\\'", moving)
            self.write_gen += 1
            self.db.execute("DELETE FROM run_analysis WHERE run_id IN (?, ?)", (into_id, from_id))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self.recompute_run(into_id)         # both pieces' rows are under into_id now: count them, don't add totals
        return self.run(into_id)

    def previous_run(self, run_id: int) -> dict | None:
        r = self.run(run_id)
        if not r:
            return None
        p = self.db.execute("SELECT * FROM runs WHERE started < ? ORDER BY started DESC LIMIT 1",
                            (r["started"],)).fetchone()
        return dict(p) if p else None

    def earlier_session_runs(self, run_id: int) -> list[dict]:
        """The runs before this one that carry the same plan session (pieces of one mow that were not joined),
        oldest first."""
        r = self.run(run_id)
        if not r or not r.get("plan_session"):
            return []
        return [dict(p) for p in self.db.execute(
            "SELECT * FROM runs WHERE plan_session = ? AND started < ? AND id != ? ORDER BY started",
            (r["plan_session"], r["started"], run_id))]

    def close_orphan_runs(self) -> int:
        ids = [r[0] for r in self.db.execute("SELECT id FROM runs WHERE ended IS NULL").fetchall()]
        n = self.db.execute("UPDATE runs SET ended = (SELECT MAX(ts) FROM telemetry t WHERE t.run_id = runs.id), "
                            "end_reason = 'recorder restarted' WHERE ended IS NULL").rowcount
        for rid in ids:                     # its totals were never written (the counters were in memory)
            self.recompute_run(rid)
        return n

    def runs(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,))]

    def run(self, run_id: int) -> dict | None:
        r = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(r) if r else None

    def analysis_get(self, run_id: int) -> dict | None:
        r = self.db.execute("SELECT json FROM run_analysis WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(r["json"]) if r else None

    def analysis_set(self, run_id: int, result: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO run_analysis (run_id, computed, json) VALUES (?, ?, ?)",
                        (run_id, time.time(), json.dumps(result)))

    def analysis_clear(self) -> None:
        self.db.execute("DELETE FROM run_analysis")

    # -- versions (map, plans, gps ref, low-confidence config) ----------------------------------------------
    def save_version(self, kind: str, obj) -> bool:
        """Stores a new version only if the content changed. Returns True when a new version was stored."""
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"))
        sha = hashlib.sha256(text.encode()).hexdigest()[:16]
        latest = self.db.execute("SELECT sha FROM versions WHERE kind=? ORDER BY fetched DESC LIMIT 1",
                                 (kind,)).fetchone()
        if latest and latest["sha"] == sha:
            return False
        # Content seen before (e.g. a map put back as it was) becomes the latest again; ignoring it would leave the
        # previous content marked as current.
        self.db.execute("INSERT INTO versions (kind, fetched, sha, json) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(kind, sha) DO UPDATE SET fetched = excluded.fetched", (kind, time.time(), sha, text))
        return True

    def latest_version(self, kind: str):
        r = self.db.execute("SELECT json FROM versions WHERE kind=? ORDER BY fetched DESC LIMIT 1", (kind,)).fetchone()
        return json.loads(r["json"]) if r else None

    def latest_sha(self, kind: str) -> str | None:
        """The newest version's sha, without reading any version's JSON."""
        r = self.db.execute("SELECT sha FROM versions WHERE kind=? ORDER BY fetched DESC LIMIT 1", (kind,)).fetchone()
        return r["sha"] if r else None

    def version(self, vid: int):
        r = self.db.execute("SELECT id, kind, fetched, sha, json FROM versions WHERE id=?", (vid,)).fetchone()
        return {**dict(r), "json": json.loads(r["json"])} if r else None

    def versions_like(self, pattern: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT id, kind, fetched, sha, length(json) AS bytes FROM versions WHERE kind LIKE ? ORDER BY fetched DESC",
            (pattern,))]

    # -- captures: full payloads of write commands other clients send (Protocol Lab, spec §14) ----------------
    def redact_stored(self) -> int:
        """Mask secrets already stored from other clients' commands (logged before the recorder redacted them, e.g. a
        Wi-Fi password from change_to_wifi on 2026-10-10). Idempotent; runs on every start."""
        from .codec import redact, redact_text
        n = 0
        for table, col, key in (("events", "detail", "id"), ("commands_seen", "sample", "name")):
            for r in self.db.execute(f"SELECT {key}, {col} FROM {table} WHERE {col} IS NOT NULL").fetchall():
                clean = redact_text(r[1])
                if clean != r[1]:
                    self.db.execute(f"UPDATE {table} SET {col}=? WHERE {key}=?", (clean, r[0]))
                    n += 1
        for r in self.db.execute("SELECT id, payload FROM captures").fetchall():
            try:
                raw = json.loads(r[1])
            except ValueError:
                continue
            clean = redact(raw)
            if clean != raw:
                self.db.execute("UPDATE captures SET payload=? WHERE id=?", (json.dumps(clean), r[0]))
                n += 1
        # the robot's replies kept as versions (a reply listing saved networks could carry a password); maps and the
        # other big versions are not replies and are left alone
        for r in self.db.execute("SELECT id, json FROM versions WHERE kind LIKE 'reply:%'").fetchall():
            try:
                raw = json.loads(r[1])
            except ValueError:
                continue
            clean = redact(raw)
            if clean != raw:
                text = json.dumps(clean, sort_keys=True, separators=(",", ":"))
                sha = hashlib.sha256(text.encode()).hexdigest()[:16]
                try:
                    self.db.execute("UPDATE versions SET json=?, sha=? WHERE id=?", (text, sha, r[0]))
                except sqlite3.IntegrityError:          # the cleaned content is already kept: this copy goes
                    self.db.execute("DELETE FROM versions WHERE id=?", (r[0],))
                n += 1
        return n

    def add_capture(self, name: str, ts: float, payload: str) -> bool:
        """Skips a repeat of the same command and payload within 5 s (it arrives on both brokers)."""
        dup = self.db.execute("SELECT 1 FROM captures WHERE name=? AND payload=? AND ts > ?",
                              (name, payload, ts - 5)).fetchone()
        if dup:
            return False
        self.db.execute("INSERT INTO captures (ts, name, payload) VALUES (?, ?, ?)", (ts, name, payload))
        self.db.execute("DELETE FROM captures WHERE id NOT IN (SELECT id FROM captures ORDER BY id DESC LIMIT 2000)")
        return True

    def captures(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT id, ts, name, length(payload) AS bytes FROM captures ORDER BY id DESC LIMIT ?", (limit,))]

    def capture(self, cid: int):
        r = self.db.execute("SELECT id, ts, name, payload FROM captures WHERE id=?", (cid,)).fetchone()
        return {**dict(r), "payload": json.loads(r["payload"])} if r else None

    def versions(self, kind: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT id, kind, fetched, sha, length(json) AS bytes FROM versions WHERE kind=? ORDER BY fetched DESC",
            (kind,))]

    # -- weather (Tempest) -------------------------------------------------------------------------------------
    def add_weather(self, row: dict) -> None:
        """A Tempest row and an HA row can share a second: they merge, rain adds up, and a value one source left
        out never erases the other's."""
        cols = [c for c in WEATHER_COLS if c in row]
        sets = ", ".join("rain_mm = COALESCE(weather.rain_mm, 0) + COALESCE(excluded.rain_mm, 0)" if c == "rain_mm"
                         else f"{c} = COALESCE(excluded.{c}, weather.{c})" for c in cols if c != "ts")
        self.db.execute(f"INSERT INTO weather ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                        f"ON CONFLICT(ts) DO {'UPDATE SET ' + sets if sets else 'NOTHING'}",
                        [row[c] for c in cols])

    def weather_since(self, since: float) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM weather WHERE ts >= ? ORDER BY ts", (since,))]

    def weather_series(self, metric: str, since: float, max_points: int = 600) -> list[list[float]]:
        """Thinned to max_points; rain is summed per bucket (dropping rows would drop rain)."""
        if metric not in WEATHER_COLS or metric == "ts":
            raise ValueError(metric)
        rows = self.db.execute(f"SELECT ts, {metric} FROM weather WHERE ts >= ? AND {metric} IS NOT NULL ORDER BY ts",
                               (since,)).fetchall()
        step = max(1, -(-len(rows) // max_points))
        if metric == "rain_mm":
            return [[rows[i][0], round(sum(r[1] for r in rows[i:i + step]), 3)] for i in range(0, len(rows), step)]
        return [[r[0], r[1]] for r in rows[::step]]

    # -- settings --------------------------------------------------------------------------------------------
    def setting(self, key: str, default=None):
        r = self.db.execute("SELECT v FROM settings WHERE k = ?", (key,)).fetchone()
        return json.loads(r["v"]) if r else default

    def set_setting(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings (k, v, updated) VALUES (?, ?, ?)",
                        (key, json.dumps(value), time.time()))

    def settings_like(self, prefix: str) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT k FROM settings WHERE k LIKE ?", (prefix + "%",))]

    def delete_setting(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE k = ?", (key,))

    # -- control audit and schedule log ----------------------------------------------------------------------
    def log_control(self, actor: str, route: str, cmd: str, args: str, result: dict) -> int:
        return self.db.execute("INSERT INTO control_log (ts, actor, route, cmd, args, sent, result) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (time.time(), actor, route, cmd, args, 1 if result.get("sent") else 0,
                                json.dumps(result))).lastrowid

    def update_control_result(self, rowid: int, result: dict) -> None:
        self.db.execute("UPDATE control_log SET result = ? WHERE rowid = ?", (json.dumps(result), rowid))

    def control_log(self, limit: int = 100, beats: bool = True) -> list[dict]:
        """`beats=False` leaves out the keep-awake beats that went (one every 4 s while the Cameras tile is open), so
        they cannot push everything else out of a page of the log; refused beats stay."""
        where = "" if beats else ("WHERE NOT (cmd = 'set_working_state' AND json_valid(result) "
                                  "AND json_extract(result, '$.sent') = 1) ")
        rows = [dict(r) for r in self.db.execute(f"SELECT * FROM control_log {where}ORDER BY ts DESC LIMIT ?",
                                                 (limit,))]
        for r in rows:
            r["result"] = json.loads(r["result"])
        return rows

    def log_schedule(self, window_id: str, date: str, outcome: str, detail: dict | None = None) -> None:
        self.db.execute("INSERT INTO schedule_log (ts, window_id, date, outcome, detail) VALUES (?, ?, ?, ?, ?)",
                        (time.time(), window_id, date, outcome, json.dumps(detail or {})))

    def schedule_outcome(self, window_id: str, date: str) -> str | None:
        r = self.db.execute("SELECT outcome FROM schedule_log WHERE window_id=? AND date=? ORDER BY ts DESC LIMIT 1",
                            (window_id, date)).fetchone()
        return r["outcome"] if r else None

    def schedule_log(self, limit: int = 100) -> list[dict]:
        rows = [dict(r) for r in self.db.execute("SELECT * FROM schedule_log ORDER BY ts DESC LIMIT ?", (limit,))]
        for r in rows:
            r["detail"] = json.loads(r["detail"] or "{}")
        return rows

    # -- events and firmware ---------------------------------------------------------------------------------
    def event(self, kind: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)", (time.time(), kind, detail))

    def events(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))]

    def firmware_last(self) -> dict:
        rows = self.db.execute("SELECT component, version FROM firmware_seen f WHERE ts = "
                               "(SELECT MAX(ts) FROM firmware_seen g WHERE g.component = f.component)").fetchall()
        return {r["component"]: r["version"] for r in rows}

    def firmware_history(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM firmware_seen ORDER BY ts DESC")]

    def firmware_add(self, component: str, version: str) -> None:
        self.db.execute("INSERT INTO firmware_seen (ts, component, version) VALUES (?, ?, ?)",
                        (time.time(), component, version))

    # -- commands other clients send the robot -----------------------------------------------------------------
    def command_seen(self, name: str, ts: float, sample: str) -> bool:
        """Count a command another client published. Returns True the first time a name is ever seen."""
        cur = self.db.execute("UPDATE commands_seen SET last=?, count=count+1, sample=? WHERE name=?",
                              (ts, sample, name))
        if cur.rowcount:
            return False
        self.db.execute("INSERT INTO commands_seen (name, first, last, count, sample) VALUES (?, ?, ?, 1, ?)",
                        (name, ts, ts, sample))
        return True

    def commands_seen(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM commands_seen ORDER BY last DESC")]

    def latest_replies(self) -> dict:
        rows = self.db.execute("SELECT kind, fetched, json FROM versions v WHERE kind LIKE 'reply:%' AND fetched = "
                               "(SELECT MAX(fetched) FROM versions w WHERE w.kind = v.kind)").fetchall()
        return {r["kind"][6:]: {"changed": r["fetched"], "data": json.loads(r["json"])} for r in rows}

    def counts(self) -> dict:
        one = lambda q: self.db.execute(q).fetchone()[0]  # noqa: E731
        return {
            "telemetry_rows": one("SELECT COUNT(*) FROM telemetry"),
            "raw_snapshots": one("SELECT COUNT(*) FROM snapshot_raw"),
            "runs": one("SELECT COUNT(*) FROM runs"),
            "map_versions": one("SELECT COUNT(*) FROM versions WHERE kind='map'"),
            "weather_rows": one("SELECT COUNT(*) FROM weather"),
            "first_ts": one("SELECT MIN(ts) FROM telemetry"),
        }
