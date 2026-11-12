"""Writing a Map Studio draft to the robot (spec §7.4).

Only commands captured from the Yarbo app in session C2 (2026-10-01) are used, with the payloads the app sent:
save_nogozone / del_nogozone, save_pathway / del_pathway, save_mower_pathway_params and upload_cloud_map_backup.
The robot assigns ids itself and returns the saved feature in its reply.

* Changing an existing no-go zone or pathway is sent as "add the new one, then delete the old one" (the app needs
  the robot moved to edit a shape in place, so that command is not known). Nothing refers to their ids.
* Areas are changed IN PLACE with `save_clean_area` carrying the area's id (captured 2026-10-07: the app's rename;
  docs/dev/map-writes.md). The live record is read (`read_all_clean_area`), only `range` and
  `name` change, and the id stays, so plans, pathway links and the area's mow settings stay attached. The safety
  copy is LOCAL (decided 2026-10-07: the robot has only 10 backup slots; robot backups are taken only on request):
  the map and the full area records are stored as a "prewrite" version before anything is sent, and every map
  version stays restorable from Map Studio → History. Adding or deleting an area is not offered: both change ids
  (plans and settings are keyed by them), so they stay in the app.
* No-vision zones are changed IN PLACE with `save_novisionzone` carrying the zone's id and its whole record (range,
  ref, name, id, enable, by_pass_level), as the app sent it on 2026-10-10 (capture app-edit-1791655059): switching one
  on or off, reshaping and renaming. The robot answers with the saved zone. A new zone is the same command with
  "id": -1 (the robot assigns one) and `del_novisionzone` {id} deletes one (app, 2026-10-10 22:29, captures 221-235).
  Adding one always needs the explicit OK (the robot ignores its cameras there). Obstacle bypass (by_pass_level) is
  shown, not changed, until the app's modes are matched (0, 1 and 3 seen).
* Plans are saved with `save_plan` {id, areaIds, name, enable_self_order}, as the app sent it on 2026-10-10 (capture
  app-edit-1791687834, a new plan): the app picks the id itself, the next one up. Editing one sends the same command
  with its id (app-edit-1791688547: a rename). `del_plan` {id} deletes one (same capture). See write_plan() and
  delete_plan().
* Dead ends are written like pathways (add; change = add the new, delete the old; delete) with `save_deadend` /
  `del_deadend`, as the app sent them on 2026-10-11 (capture app-edit-1791752041). A dead end's own settings
  (`read_deadend_params` / `save_deadend_params`) are carried over to its replacement when they were ever set (a dead
  end never configured reads back as id 0, the robot's defaults) and edited with write_deadend_params().
* Sidewalks and fences cannot be written (commands not captured); those changes stay in the draft.
* Adds go first and deletes last, so a failure part-way never leaves the yard without a zone.
* After the last command the map is read back and compared with the draft. Any failure or mismatch undoes
  everything that was sent (delete what was added, re-add what was deleted).
"""
import asyncio
import copy
import inspect
import json
import time

from . import geo, mapstudio
from .codec import decode_any

SENDABLE = ("nogozones", "pathways", "deadends")       # add / change (as add + delete) / delete
UPDATABLE = ("areas", "novisionzones")      # changed in place, same id
AREA_SAVE = "save_clean_area"
BACKUP_CMD = "save_map_backup_and_get_all_map_backup_nameandid"
# The keys the app sends in save_clean_area; the robot's computed keys (area, humps, mult_grid...) are never sent back.
AREA_KEYS = ("algorithm_type", "id", "labels", "leaf_piles", "name", "push_snow_dir", "range", "ref", "slope_degrees",
             "snowPiles", "trimming_edges")
SAVE = {"nogozones": "save_nogozone", "pathways": "save_pathway", "deadends": "save_deadend"}
DELETE = {"nogozones": "del_nogozone", "pathways": "del_pathway", "deadends": "del_deadend"}
DEADEND_PARAMS_SAVE = "save_deadend_params"
NOVIS_SAVE = "save_novisionzone"
NOVIS_DELETE = "del_novisionzone"
ADD_CMD = {**SAVE, "novisionzones": NOVIS_SAVE}         # kinds that can be added and deleted (not changed by add+delete)
DEL_CMD = {**DELETE, "novisionzones": NOVIS_DELETE}
NOVIS_SENT = ("range", "ref", "name", "id", "enable", "by_pass_level")     # the record the app saves
NOVIS_NEW_M2 = 0.5        # a no-vision zone taking in more new ground than this (or 5 %) needs the explicit OK
PLAN_SAVE = "save_plan"
PLAN_DELETE = "del_plan"                 # {"id": N}, captured 2026-10-10 (app-edit-1791688547)
PLAN_KEYS = ("id", "areaIds", "name", "enable_self_order")                 # what the app sends
PLAN_NAME_MAX = 40
WRITE_COMMANDS = frozenset({*SAVE.values(), *DELETE.values(), "save_mower_pathway_params", "upload_cloud_map_backup",
                            DEADEND_PARAMS_SAVE,
                            NOVIS_SAVE, NOVIS_DELETE, PLAN_SAVE, PLAN_DELETE,
                            AREA_SAVE, BACKUP_CMD, "get_all_map_backup",
                            "del_map_backup", "map_recovery", "save_mower_area_params", "rename_map_backup",   # backups.py
                            "change_to_wifi", "forget_wifi"})                                                  # network.py
# Backup bookkeeping: allowed during a plan (nothing the robot drives by changes); every other write needs it idle.
BOOKKEEPING = frozenset({BACKUP_CMD, "get_all_map_backup", "del_map_backup", "rename_map_backup",
                         "upload_cloud_map_backup"})
# What the Yarbo app sent for a new pathway's mowing settings (capture C2).
PATHWAY_DEFAULTS = {"blade_speed": 80, "blade_height": 0, "turn_type": 0, "land_push_pod_place": 50, "route_angle": 0,
                    "route_dis": 0.0, "offset_enable": False, "en_blade": False, "plan_speed": 0.5,
                    "pathway_obs_perception_sw": True, "sensor_switch": 11, "en_trimmer": False,
                    "offset_range": [-0.5, 0.5], "offset_step": 0.1}
INCH = 0.0254
_IN = {"unit": "in", "per": INCH, "step": 1, "adv_step": 0.1, "digits": 1}     # stored in metres, shown in inches
OFFSET_MAX_M = 1.0       # the app's own limit for a pathway: -39.4 to 39.4 in (the app's screenshot, 2026-10-11)
# The app moves offsets in 0.1 m (every value seen: -0.5, 0.5, 0.7, 0.1, 0.2): the sliders step 0.1 m, shown in inches,
# and the panel rounds to 0.1 m before checking and sending.
_OFF = {**_IN, "step": round(0.1 / INCH, 3), "adv_step": round(0.1 / INCH, 3), "fine": round(0.1 / INCH, 3)}
# A pathway's mowing settings as read_mower_pathway_params reports them (2026-10-06, all 6 pathways), in display
# order. Editable only where the meaning is clear from the robot's own data; the rest are shown as the robot reports
# them until their units are matched to the Yarbo app (a capture of a change made in the app settles each one).
PATHWAY_FIELDS = [
    {"key": "plan_speed", "label": "Speed", "type": "number", "unit": "m/s", "min": 0.1, "max": 0.7, "step": 0.05,
     "adv_step": 0.01, "levels": [[0.3, "Slowest"], [0.4, "Slow"], [0.5, "Medium"], [0.6, "Fast"], [0.7, "Fastest"]],
     "editable": True, "help": "Driving speed along the pathway, in the app's five levels; Advanced sets any speed up to "
                               "the robot's 0.7 m/s."},
    {"key": "pathway_obs_perception_sw", "label": "Obstacle sensing", "type": "bool", "editable": True,
     "help": "Stop for obstacles the cameras see on this pathway."},
    {"key": "en_trimmer", "label": "Trimmer", "type": "bool", "editable": True,
     "help": "Run the edge trimmer along the pathway."},
    {"key": "en_blade", "label": "Blades on", "type": "bool", "editable": True,
     "help": "Mow while driving the pathway (matched in the app 2026-10-10)."},
    {"key": "blade_height", "label": "Blade height", "type": "number", "editable": False, "known": {"0": "all the way up, 4.0 in"},
     "help": "The deck's lift position: 0 is all the way up (4.0 in, per the app), which every pathway has. The "
             "scale between is not pinned yet (an area's 25 is 3.3 in)."},
    {"key": "blade_speed", "label": "Blade speed", "type": "number", "editable": False, "simple": False,
     "help": "The robot's raw value, probably percent."},
    {"key": "turn_type", "label": "Turn type", "type": "number", "editable": False, "simple": False, "help": "Meaning not known yet."},
    # The area's Transit path offset controls (app, 2026-10-10: "the same settings as areas"), matched from the app on
    # Pathway 9 (offset on/off, range -0.5..0.5 -> -0.5..0.7 m, step 0.1 -> 0.2 m). The app's sliders run -39.4 to
    # 39.4 in for the range and 3.9 to 19.7 in for the step (screenshot of the app's Pathway Settings, 2026-10-11).
    {"key": "offset_enable", "label": "Transit path offset", "control": "bool", "type": "bool", "editable": True,
     "help": "Spreads the robot's trips across parallel tracks to prevent ruts."},
    {"key": "offset_range.0", "label": "Range offset, from", "control": "slider", "type": "number", "unit": "m",
     "show": _OFF, "round": 1, "min": -OFFSET_MAX_M, "max": 0.0, "editable": True,
     "help": f"How far the tracks spread to one side (the robot's default is -19.7 in; the app allows up to "
             f"{OFFSET_MAX_M / INCH:.1f} in)."},
    {"key": "offset_range.1", "label": "Range offset, to", "control": "slider", "type": "number", "unit": "m",
     "show": _OFF, "round": 1, "min": 0.0, "max": OFFSET_MAX_M, "editable": True,
     "help": f"How far the tracks spread to the other side (default 19.7 in; the app allows up to "
             f"{OFFSET_MAX_M / INCH:.1f} in)."},
    {"key": "offset_step", "label": "Step offset", "control": "slider", "type": "number", "unit": "m",
     "show": _OFF, "round": 1, "min": 0.1, "max": 0.5, "editable": True,
     "help": "How far each trip shifts (as for areas: 3.9-19.7 in)."},
]
PATHWAY_EDITABLE = {f["key"]: f for f in PATHWAY_FIELDS if f["editable"]}
# sensor_switch moves with obstacle sensing: 11 with it on, 10 with it off (Pathway 10, changed in the app). Only
# that observed pair is ever rewritten; any other combination leaves obstacle sensing read-only.
OBSTACLE_PAIRS = {True: 11, False: 10}
# Keys a settings read must carry (route_angle / route_dis are save-only) and the extra keys a read may carry.
READ_REQUIRED = set(PATHWAY_DEFAULTS) - {"route_angle", "route_dis"}
READ_EXTRA = {"id", "current_offset"}
COMPARE_KEYS = sorted(READ_REQUIRED)


def obstacle_lock(current: dict) -> str | None:
    """Why obstacle sensing cannot be switched here, or None."""
    obs, sw = current.get("pathway_obs_perception_sw"), current.get("sensor_switch")
    if not isinstance(obs, bool) or OBSTACLE_PAIRS.get(obs) != sw:
        return (f"its sensor value ({sw}) is not one seen before with obstacle sensing "
                f"{'on' if obs else 'off'}; change it in the app")
    return None


def read_problem(current: dict) -> str | None:
    """A settings read the editor cannot safely build a full save from (missing or unknown fields)."""
    missing = sorted(READ_REQUIRED - set(current))
    extra = sorted(set(current) - READ_REQUIRED - READ_EXTRA)
    if missing:
        return f"the robot's settings lack {', '.join(missing)}"
    if extra:
        return f"the robot reports fields this panel does not know ({', '.join(extra)}); a save could reset them"
    return None


def get_path(d, key: str):
    """d["a"]["b"] for "a.b"; a number part indexes a list ("offset_range.1")."""
    for part in key.split("."):
        if isinstance(d, list) and part.isdigit() and int(part) < len(d):
            d = d[int(part)]
        elif isinstance(d, dict):
            d = d.get(part)
        else:
            return None
    return d


def _set_path(d: dict, key: str, v) -> None:
    head, _, idx = key.partition(".")
    if not idx:
        d[head] = v
        return
    lst = d.get(head)
    if not isinstance(lst, list) or not idx.isdigit() or int(idx) >= len(lst):
        raise ValueError(f"the robot's {head} is not a list this panel can change")
    lst = list(lst)                                  # never the read's own list
    lst[int(idx)] = v
    d[head] = lst


def offset_problem(settings: dict) -> str | None:
    """The transit path offset as a whole, as it would be saved: both ends of the range and the step within what the
    app has been seen to send, the range around the pathway (from < to). Checked whenever any offset field changes,
    so switching the offset on never enables a wider range set elsewhere."""
    rng, step = settings.get("offset_range"), settings.get("offset_step")
    if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                             for x in rng)):
        return f"the robot's offset range ({rng}) is not a pair of numbers; change it in the app"
    lo, hi = rng
    if not -OFFSET_MAX_M - 1e-9 <= lo < hi <= OFFSET_MAX_M + 1e-9 or lo > 0 or hi < 0:
        return (f"Range offset: {lo} to {hi} m is outside what the panel sends (from -{OFFSET_MAX_M} to 0, to 0 to "
                f"{OFFSET_MAX_M} m, from below to); narrow it first")
    if not (isinstance(step, (int, float)) and not isinstance(step, bool) and 0.1 - 1e-9 <= step <= 0.5 + 1e-9):
        return f"Step offset: {step} m is outside 0.1-0.5 m"
    return None


def pathway_params_payload(current: dict, changes: dict, pid: int) -> dict:
    """The save payload for a settings change: everything as the robot has it now, plus the checked changes.
    Raises ValueError for an unknown or read-only field, or a value out of range."""
    why = read_problem(current)
    if why:
        raise ValueError(why)
    out = params_for_save(current, pid)
    if "pathway_obs_perception_sw" in changes:
        lock = obstacle_lock(current)
        if lock:
            raise ValueError(f"Obstacle sensing: {lock}")
    for k, v in changes.items():
        f = PATHWAY_EDITABLE.get(k)
        if f is None:
            raise ValueError(f"{k} cannot be changed here")
        if f["type"] == "bool":
            if not isinstance(v, bool):
                raise ValueError(f"{f['label']}: true or false")
        elif isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{f['label']}: a number")
        elif "round" in f and abs(v - round(v, f["round"])) <= 0.005:
            v = round(float(v), f["round"])              # a slider's inch steps land ~1 mm off the 0.1 m grid
        if f["type"] == "number" and not f["min"] <= v <= f["max"]:
            raise ValueError(f"{f['label']}: {f['min']} to {f['max']} {f.get('unit', '')}".rstrip())
        _set_path(out, k, round(float(v), 3) + 0.0 if f["type"] == "number" else v)       # + 0.0: never -0.0
    if any(k.startswith("offset_") for k in changes):
        why = offset_problem(out)
        if why:
            raise ValueError(why)
    if "pathway_obs_perception_sw" in changes:
        out["sensor_switch"] = OBSTACLE_PAIRS[out["pathway_obs_perception_sw"]]
    return out


def _same(a, b) -> bool:
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-3
    return a == b


VERIFY_M = 0.05
BIG_CHANGE = 0.25          # an area that grows or shrinks by more than this share needs an explicit OK
OVERLAP_M2 = 1.0           # an area may not be moved over another area by more than this
MANUAL_BACKUPS = 10        # the robot keeps 10 non-automatic backups; the app asks to delete one when full
REPLY_TIMEOUT_S = 15
WHY_NOT = {
    "areas": "new areas are recorded by driving the robot in the app",
}
WHY_NOT_AREA_DELETE = ("deleting an area also deletes the plans that use it and its mow settings; do it in the app "
                       "if you mean it")
WHY_NOT_RENAME = ("renaming needs the app's own command, not captured yet: rename one in the Yarbo app while this "
                  "panel runs and it appears under Captured writes")


class WriteRefused(Exception):
    pass


def payload_for(kind: str, f: dict) -> dict:
    """The save payload, shaped exactly like the app's (no id: the robot assigns one)."""
    base = {"range": copy.deepcopy(f.get("range") or []), "ref": copy.deepcopy(f.get("ref") or {}),
            "name": f.get("name") or ""}
    if kind == "novisionzones":
        return novis_new_payload(f)
    if kind == "nogozones":
        return {**base, "type": f.get("type", 0), "enable": bool(f.get("enable", True)),
                "trimming_edges": copy.deepcopy(f.get("trimming_edges") or [])}
    if kind == "deadends":                    # exactly the app's keys (2026-10-11); the robot links the ends itself
        return {"connectids": copy.deepcopy(f.get("connectids") or []), "name": base["name"], "range": base["range"],
                "ref": base["ref"], "trimming_edges": copy.deepcopy(f.get("trimming_edges") or [])}
    return {**base, **{k: copy.deepcopy(f.get(k) or []) for k in ("connectids", "snowPiles", "leaf_piles", "trimming_edges")}}


def novis_new_payload(f: dict) -> dict:
    """save_novisionzone for a NEW zone, as the app sent it (2026-10-10): id -1; on, bypass 0 unless the draft (or the
    zone being put back) says otherwise."""
    level = f.get("by_pass_level")
    return {**novis_payload({**f, "id": -1, "enable": f.get("enable") if isinstance(f.get("enable"), bool) else True,
                             "by_pass_level": level if isinstance(level, int) and not isinstance(level, bool) else 0})}


def novis_payload(f: dict) -> dict:
    """save_novisionzone for an existing zone, shaped like the app's (2026-10-10): the whole record, same id. plan()
    only builds one when enable and by_pass_level are really there (no defaults)."""
    pts = [p for p in f.get("range") or [] if "x" in p and "y" in p]
    return {"range": [{"x": round(p["x"], 4), "y": round(p["y"], 4), "phi": p.get("phi", 0.0)} for p in pts],
            "ref": copy.deepcopy(f.get("ref") or {}), "name": f.get("name") or "", "id": f.get("id"),
            "enable": f["enable"], "by_pass_level": f["by_pass_level"]}


def params_for_save(read: dict | None, new_id: int) -> dict:
    """Mowing settings for a saved pathway: the old path's (read_mower_pathway_params) or the app's defaults."""
    read = read or {}
    return {**{k: read.get(k, v) for k, v in PATHWAY_DEFAULTS.items()}, "id": new_id}


def _same_ref(a, b) -> bool:
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return False
    # ~1 cm; get_map and read_all_clean_area agree exactly on this robot (checked 2026-10-07), a real frame mismatch
    # is metres
    return all(isinstance(a.get(k), (int, float)) and isinstance(b.get(k), (int, float)) and abs(a[k] - b[k]) < 1e-7
               for k in ("latitude", "longitude"))


def _overlap_m2(ring, other, cell: float = 0.25) -> float:
    """Approximate overlap of two local rings by sampling a grid (no geometry library needed)."""
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    ox, oy = [p[0] for p in other], [p[1] for p in other]
    x0, x1, y0, y1 = max(min(xs), min(ox)), min(max(xs), max(ox)), max(min(ys), min(oy)), min(max(ys), max(oy))
    if x0 >= x1 or y0 >= y1:
        return 0.0
    n, x = 0, x0 + cell / 2
    while x < x1:
        y = y0 + cell / 2
        while y < y1:
            if mapstudio.point_in((x, y), ring) and mapstudio.point_in((x, y), other):
                n += 1
            y += cell
        x += cell
    return n * cell * cell


def _new_ground_m2(new, old) -> float:
    """Ground inside `new` that `old` did not cover: one sample grid over new's bounding box (so a no-op or a pure
    shrink counts 0), cells of 0.1 m, coarser for a big zone (at most ~40,000 samples)."""
    xs, ys = [p[0] for p in new], [p[1] for p in new]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    cell = max(0.1, (w * h / 40000) ** 0.5)
    n, x = 0, min(xs) + cell / 2
    while x < max(xs):
        y = min(ys) + cell / 2
        while y < max(ys):
            if mapstudio.point_in((x, y), new) and not mapstudio.point_in((x, y), old):
                n += 1
            y += cell
        x += cell
    return n * cell * cell


def area_payload(record: dict, feature: dict, gps_ref) -> dict:
    """save_clean_area for an in-place edit: the robot's live record (app keys only) with the draft's range and name.
    The range is put in the record's own frame (metres from its ref)."""
    out = {k: copy.deepcopy(record[k]) for k in AREA_KEYS if k in record}
    if not _same_ref(record.get("ref"), feature.get("ref")):
        raise WriteRefused(f"the draft's copy of area “{record.get('name')}” has a different reference point than "
                           "the robot's; discard the draft and redo the edit")
    pts = [p for p in feature.get("range") or [] if "x" in p and "y" in p]
    out["range"] = [{"x": round(p["x"], 4), "y": round(p["y"], 4), "phi": p.get("phi", 0.0)} for p in pts]
    out["name"] = feature.get("name") or record.get("name") or ""
    return out


def _pathway_breaks(current: dict, draft: dict, area_id, gps_ref) -> list[str]:
    """Pathway ends that sit in the area now but would fall outside it after the edit (the robot re-links pathways
    on every area save; an end outside every area breaks routes, e.g. WP005 from the dock)."""
    out = []
    for p in draft.get("pathways") or []:
        pts = mapstudio.local(p, gps_ref)
        if len(pts) < 2:
            continue
        for end, pt in (("start", pts[0]), ("end", pts[-1])):
            if (mapstudio._endpoint_id(current, pt, gps_ref) == area_id
                    and mapstudio._endpoint_id(draft, pt, gps_ref) != area_id):
                out.append(f"the {end} of “{p.get('name') or p.get('id')}”")
    return out


def plan(current: dict, draft: dict, gps_ref) -> dict:
    """The commands that turn `current` into `draft`, or why it cannot be done."""
    d = mapstudio.diff(current, draft, gps_ref)
    adds, deletes, blocked, updates = [], [], [], []

    def label(op, kind, name):
        return f"{op} {mapstudio.KIND_LABELS.get(kind, kind).lower()} “{name}”"

    for r in d["added"]:
        if r["kind"] in ADD_CMD:
            f = mapstudio.find(draft, r["kind"], r["id"])
            novis = r["kind"] == "novisionzones"
            on = novis and payload_for(r["kind"], f)["enable"]
            adds.append({"op": "add", "kind": r["kind"], "draft_id": r["id"], "replaces": None, "cmd": ADD_CMD[r["kind"]],
                         "payload": payload_for(r["kind"], f), "big": bool(on),
                         "label": label("add", r["kind"], r["name"]) +
                                  (": the robot will ignore its cameras inside it" if on else "")})
        else:
            blocked.append({**r, "why": WHY_NOT.get(r["kind"], "no command for this has been captured yet")})
    for r in d["changed"]:
        if r["kind"] == "novisionzones":
            old_f, new_f = mapstudio.find(current, r["kind"], r["id"]), mapstudio.find(draft, r["kind"], r["id"])
            if not _same_ref(old_f.get("ref"), new_f.get("ref")):
                blocked.append({**r, "why": "the draft's copy has a different reference point than the robot's; "
                                "discard the draft and redo the edit"})
                continue
            # The whole record is sent, so the robot's own values must all be there (get_map carries enable and
            # by_pass_level, as in the 2026-10-10 capture): a default here could quietly change obstacle bypass.
            if not all(isinstance(f.get("enable"), bool) and isinstance(f.get("by_pass_level"), int)
                       and not isinstance(f.get("by_pass_level"), bool) for f in (old_f, new_f)):
                blocked.append({**r, "why": "the robot's record of this zone lacks its on/off or obstacle bypass value; "
                                "change it in the app"})
                continue
            if new_f["by_pass_level"] != old_f["by_pass_level"]:
                blocked.append({**r, "why": "obstacle bypass is changed in the app for now"})
                continue
            other = sorted(k for k in set(old_f) | set(new_f) if k not in NOVIS_SENT and old_f.get(k) != new_f.get(k))
            if other:
                blocked.append({**r, "why": f"only on/off, shape and name can be sent (not {', '.join(other)})"})
                continue
            old_pts, new_pts = mapstudio.local(old_f, gps_ref), mapstudio.local(new_f, gps_ref)
            a0, a1 = mapstudio.ring_area(old_pts), mapstudio.ring_area(new_pts)
            # Wider is the less safe way (more ground where the robot ignores its cameras): switching a zone on, or
            # an outline that takes in ground the old one did not (grown or moved), needs the explicit OK, as a big
            # area change does. Shrinking or reshaping inside the old outline does not.
            reshaped = mapstudio.max_move(old_pts, new_pts, True) > 0.001 or len(old_pts) != len(new_pts)
            new_ground = _new_ground_m2(new_pts, old_pts) if reshaped else 0.0
            wider = [w for w, hit in (("switched on", new_f["enable"] and not old_f["enable"]),
                                      (f"{new_ground:.0f} m² of new ground", new_ground > max(NOVIS_NEW_M2, 0.05 * a0)))
                     if hit]
            updates.append({"op": "update", "kind": r["kind"], "robot_id": r["id"], "draft_id": r["id"], "cmd": NOVIS_SAVE,
                            "payload": novis_payload(new_f), "original": novis_payload(old_f), "big": bool(wider),
                            "label": label("update", r["kind"], r["name"]) + f" in place ({', '.join(r.get('changes') or [])})"
                                     + (f": the robot will ignore its cameras over more ground ({', '.join(wider)})"
                                        if wider else "")})
            continue
        if r["kind"] == "areas":
            old_f, new_f = mapstudio.find(current, "areas", r["id"]), mapstudio.find(draft, "areas", r["id"])
            old_pts, new_pts = mapstudio.local(old_f, gps_ref), mapstudio.local(new_f, gps_ref)
            reshaped = mapstudio.max_move(old_pts, new_pts, True) > 0.001 or len(old_pts) != len(new_pts)
            renamed = (old_f.get("name") or "") != (new_f.get("name") or "")
            if not (reshaped or renamed):
                blocked.append({**r, "why": "only an area's shape and name can be sent"})
                continue
            if not _same_ref(old_f.get("ref"), new_f.get("ref")):
                blocked.append({**r, "why": "the draft's copy has a different reference point than the robot's; "
                                "discard the draft and redo the edit"})
                continue
            breaks = _pathway_breaks(current, draft, r["id"], gps_ref)
            if breaks:
                blocked.append({**r, "why": "it would leave " + ", ".join(breaks) + " outside the area; keep "
                                "pathway ends inside it (or move the pathway too)"})
                continue
            over = []
            if reshaped:            # only what the edit ADDS counts (two areas may already share a border strip)
                for a in draft.get("areas") or []:
                    if a.get("id") == r["id"]:
                        continue
                    ring = mapstudio.local(a, gps_ref)
                    added = _overlap_m2(new_pts, ring) - _overlap_m2(old_pts, ring)
                    if added > OVERLAP_M2:
                        over.append((a.get("name") or a.get("id"), added))
            if over:
                blocked.append({**r, "why": "it would overlap " + ", ".join(f"“{n}” by {m2:.0f} m² more" for n, m2 in over)})
                continue
            a0, a1 = mapstudio.ring_area(old_pts), mapstudio.ring_area(new_pts)
            big = a0 > 0 and abs(a1 - a0) / a0 > BIG_CHANGE
            what = f"{a0:.0f} → {a1:.0f} m², {len(old_pts)} → {len(new_pts)} points" if reshaped else "rename"
            updates.append({"op": "update", "kind": r["kind"], "robot_id": r["id"], "draft_id": r["id"],
                            "cmd": AREA_SAVE, "payload": None, "big": big, "area_before": round(a0, 1),
                            "area_after": round(a1, 1),
                            "label": label("update", r["kind"], r["name"]) + f" in place ({what})"})
            continue
        if r["kind"] in SENDABLE:
            f = mapstudio.find(draft, r["kind"], r["id"])
            adds.append({"op": "add", "kind": r["kind"], "draft_id": r["id"], "replaces": r["id"], "cmd": SAVE[r["kind"]],
                         "payload": payload_for(r["kind"], f), "label": label("add the new", r["kind"], r["name"])})
            deletes.append({"op": "delete", "kind": r["kind"], "robot_id": r["id"], "cmd": DELETE[r["kind"]],
                            "payload": {"id": r["id"]}, "label": label("delete the old", r["kind"], r["name"])})
        elif all(n.startswith("renamed") for n in r.get("changes") or []):
            blocked.append({**r, "why": WHY_NOT_RENAME})
        else:
            blocked.append({**r, "why": WHY_NOT.get(r["kind"], "no command for this has been captured yet")})
    for r in d["removed"]:
        if r["kind"] in DEL_CMD:
            deletes.append({"op": "delete", "kind": r["kind"], "robot_id": r["id"], "cmd": DEL_CMD[r["kind"]],
                            "payload": {"id": r["id"]}, "label": label("delete", r["kind"], r["name"])})
        elif r["kind"] == "areas":
            blocked.append({**r, "why": WHY_NOT_AREA_DELETE})
        else:
            blocked.append({**r, "why": WHY_NOT.get(r["kind"], "no command for this has been captured yet")})
    # Areas first: the robot re-links pathways on each area save, so new pathways then link to the new shapes.
    return {"steps": updates + adds + deletes, "blocked": blocked, "diff": d}


def verify(before: dict, after: dict, draft: dict, steps: list, new_ids: dict, gps_ref) -> list[str]:
    """Problems with `after` (the map read back): every add present where the draft put it, every delete gone,
    and nothing else changed."""
    problems = []
    touched = set()
    for s in steps:
        if s["op"] == "update":
            touched.add((s["kind"], s["robot_id"]))
            want, got = mapstudio.find(draft, s["kind"], s["draft_id"]), mapstudio.find(after, s["kind"], s["robot_id"])
            if got is None:
                problems.append(f"{s['label']}: it is gone from the robot")
                continue
            moved = mapstudio.max_move(mapstudio.local(want, gps_ref), mapstudio.local(got, gps_ref), True)
            if moved > VERIFY_M:
                problems.append(f"{s['label']}: the robot's copy is {moved:.2f} m off")
            if (got.get("name") or "") != (want.get("name") or ""):
                problems.append(f"{s['label']}: saved as “{got.get('name')}”")
            if s["kind"] == "novisionzones":
                if bool(got.get("enable", True)) != bool(want.get("enable", True)):
                    problems.append(f"{s['label']}: saved {'on' if got.get('enable') else 'off'}")
                if got.get("by_pass_level", 0) != want.get("by_pass_level", 0):
                    problems.append(f"{s['label']}: obstacle bypass saved as {got.get('by_pass_level')}")
            continue
        if s["op"] == "add":
            nid = new_ids.get((s["kind"], s["draft_id"]))
            touched.add((s["kind"], nid))
            want, got = mapstudio.find(draft, s["kind"], s["draft_id"]), mapstudio.find(after, s["kind"], nid)
            if got is None:
                problems.append(f"{s['label']}: not on the robot after writing")
                continue
            closed = s["kind"] in mapstudio.POLYGONS
            moved = mapstudio.max_move(mapstudio.local(want, gps_ref), mapstudio.local(got, gps_ref), closed)
            if moved > VERIFY_M:
                problems.append(f"{s['label']}: the robot's copy is {moved:.2f} m off")
            if (got.get("name") or "") != (want.get("name") or ""):
                problems.append(f"{s['label']}: saved as “{got.get('name')}”")
            if s["kind"] == "deadends":
                expect = mapstudio._endpoint_id(draft, mapstudio.local(want, gps_ref)[0], gps_ref)
                if got.get("start_id") != expect or not (isinstance(expect, int) and expect > 0):
                    problems.append(f"{s['label']}: the robot linked its start to {got.get('start_id')}, not area {expect}")
            if s["kind"] == "nogozones" and bool(got.get("enable", True)) != bool(want.get("enable", True)):
                problems.append(f"{s['label']}: saved {'enabled' if got.get('enable') else 'disabled'}")
            if s["kind"] == "novisionzones":
                sent = s["payload"]
                if bool(got.get("enable", True)) != sent["enable"]:
                    problems.append(f"{s['label']}: saved {'on' if got.get('enable') else 'off'}")
                if got.get("by_pass_level", 0) != sent["by_pass_level"]:
                    problems.append(f"{s['label']}: obstacle bypass saved as {got.get('by_pass_level')}")
        else:
            touched.add((s["kind"], s["robot_id"]))
            if mapstudio.find(after, s["kind"], s["robot_id"]) is not None:
                problems.append(f"{s['label']}: still on the robot")
    old = {(k, f.get("id")): f for k, f in mapstudio.iter_features(before)}
    new = {(k, f.get("id")): f for k, f in mapstudio.iter_features(after)}
    for key, f in old.items():
        if key in touched:
            continue
        g = new.get(key)
        if g is None:
            problems.append(f"{mapstudio.KIND_LABELS.get(key[0], key[0])} {key[1]} disappeared")
        elif mapstudio.max_move(mapstudio.local(f, gps_ref), mapstudio.local(g, gps_ref), key[0] in mapstudio.POLYGONS) > VERIFY_M:
            problems.append(f"{mapstudio.KIND_LABELS.get(key[0], key[0])} “{f.get('name')}” changed although the draft did not touch it")
    for key in new:
        if key not in old and key not in touched:
            problems.append(f"an unexpected {mapstudio.KIND_LABELS.get(key[0], key[0]).lower()} {key[1]} appeared")
    if any(s["op"] == "update" and s["kind"] == "areas" for s in steps):
        problems += link_problems(before, after, draft, gps_ref)
    return problems


def link_problems(before: dict, after: dict, draft: dict, gps_ref) -> list[str]:
    """The robot re-links pathways after an area save. A link may change only to the area the edit puts that end in
    (e.g. a loose 0 becoming the area); losing a link or moving to another area is a problem."""
    out = []
    was = {p.get("id"): p for p in before.get("pathways") or []}
    for p in after.get("pathways") or []:
        o = was.get(p.get("id"))
        if not o:
            continue
        pts = mapstudio.local(p, gps_ref)
        for end, pt in (("start_id", pts[0] if pts else None), ("end_id", pts[-1] if pts else None)):
            a, b = o.get(end), p.get(end)
            if a == b or pt is None:
                continue
            expected = mapstudio._endpoint_id(draft, pt, gps_ref)
            if isinstance(b, int) and b > 0 and b == expected:
                continue
            out.append(f"pathway “{p.get('name') or p.get('id')}” {end.split('_')[0]} link changed {a} → {b}")
    return out


def _audit_args(cmd: str, payload: dict) -> str:
    """What goes in the control log: names, counts and ids, never locations or the account email."""
    if cmd == AREA_SAVE:
        return json.dumps({"id": payload.get("id"), "name": payload.get("name"), "points": len(payload.get("range") or [])})
    if cmd == NOVIS_SAVE:
        return json.dumps({"id": payload.get("id"), "name": payload.get("name"), "points": len(payload.get("range") or []),
                           "enable": payload.get("enable"), "by_pass_level": payload.get("by_pass_level")})
    if cmd in SAVE.values():
        return json.dumps({"name": payload.get("name"), "points": len(payload.get("range") or [])})
    if cmd == NOVIS_SAVE:
        return json.dumps({"id": payload.get("id"), "name": payload.get("name"), "points": len(payload.get("range") or []),
                           "enable": payload.get("enable"), "by_pass_level": payload.get("by_pass_level")})
    if cmd == "upload_cloud_map_backup":
        return "{}"
    if cmd in ("change_to_wifi", "forget_wifi"):  # neither the password nor the network's name reaches the log
        return json.dumps({"wifi_name": "…", **({"password": "***"} if cmd == "change_to_wifi" else {})})
    return json.dumps(payload)


class _WriteLock:
    """The writer's lock. While it is held the write keeps the route it started on (there is only the Yarbo cloud;
    a write that loses it is refused rather than carried on some other way)."""

    def __init__(self, writer):
        self._lock, self._writer = asyncio.Lock(), writer

    async def __aenter__(self):
        await self._lock.acquire()
        self._writer.route, self._writer.cleared = None, False
        return self

    async def __aexit__(self, *exc):
        self._writer.route, self._writer.cleared = None, False
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


class MapWriter:
    """Runs a plan. The I/O comes in as functions so tests can stand in for the robot:
    publish(cmd, payload[, route]) -> the route it went (app.link.RobotLink.write_command); expect_reply(cmd) -> Future
    registered BEFORE publishing; read_map() -> raw map (a fresh get_map); read_params(pathway_id) -> dict | None;
    account() -> the cloud account email (only for the optional cloud backup after a write)."""

    def __init__(self, store, publish, expect_reply, read_map, read_params, account, read_areas=None,
                 read_deadend=None):
        self.store, self.publish, self.expect = store, publish, expect_reply
        self.read_map, self.read_params, self.account = read_map, read_params, account
        self.read_areas = read_areas            # async () -> {id: live area record} (read_all_clean_area)
        self.read_deadend = read_deadend        # async (id) -> a dead end's settings (read_deadend_params)
        self.lock = _WriteLock(self)
        self.start_block = lambda cmd: None     # (cmd) -> why no write may go out now (#69); set by Ops
        self.cleared = False                    # this lock hold passed start_block at its first send
        self.route: str | None = None           # the route this write goes by (set at its first send)
        self._routed = "route" in inspect.signature(publish).parameters

    async def _send(self, cmd: str, payload: dict, rollback: bool = False) -> dict:
        """rollback=True: putting things back after a failed write may take another route if the write's route is gone
        (the write itself never switches route)."""
        if cmd not in WRITE_COMMANDS:
            raise WriteRefused(f"{cmd} is not a map-write command")
        if not (rollback or self.cleared or cmd == "get_all_map_backup"):
            # Every write goes out here (#69). Asked once per hold of the lock, at its first send, so a write is never
            # stopped half-way: with the lock held no start can go out (control asks right before it sends). The
            # gates before the lock can be many seconds old by now (reads of the map, of each area), so this asks
            # again: no start settling and, for anything but backup bookkeeping, no plan running.
            why = self.start_block(cmd)
            if why:
                raise WriteRefused(why)
            self.cleared = True
        fut = self.expect(cmd)
        started = time.time()
        try:
            if not self._routed:
                self.publish(cmd, payload)
            else:
                try:
                    self.route = self.publish(cmd, payload, route=self.route) or self.route
                except ConnectionError as first:
                    if not (rollback and self.route):
                        raise
                    # Putting things back: try once more, by whatever route the link offers now (the same one if it
                    # is back, another if the write's route is gone).
                    was, self.route = self.route, self.publish(cmd, payload)
                    self.store.event("map_write_rerouted", f"putting back after a failed write: {cmd} failed through "
                                     f"{was} ({first}); sent again through {self.route}")
            reply = await asyncio.wait_for(fut, REPLY_TIMEOUT_S)
            result = {"sent": True, "ok": reply.get("state") == 0, "state": reply.get("state"), "msg": reply.get("msg"),
                      "data": reply.get("data")}
        except asyncio.TimeoutError:
            result = {"sent": True, "ok": False, "error": f"no reply in {REPLY_TIMEOUT_S} s"}
        except Exception as exc:  # not connected, etc.
            fut.cancel()
            result = {"sent": False, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result["latency_ms"] = round((time.time() - started) * 1000)
        self.store.log_control("map", self.route or ("cloud" if not self._routed else "none"), cmd, _audit_args(cmd, payload),
                               {k: v for k, v in result.items() if k != "data"})
        return result

    async def _save(self, kind: str, payload: dict, params: dict | None,
                    known: set | None = None) -> tuple[int | None, str | None]:
        r = await self._send(ADD_CMD[kind], payload)
        data = decode_any(r.get("data"))
        data = data if isinstance(data, dict) else {}
        if r["sent"] and not isinstance(data.get("id"), int) and kind == "novisionzones" and known is not None:
            data = {"id": await self._find_new(kind, payload, known)}     # the reply did not name it: look on the map
        if not r["ok"] or not isinstance(data.get("id"), int):
            return None, r.get("error") or r.get("msg") or f"state {r.get('state')}"
        if kind == "pathways":
            p = await self._send("save_mower_pathway_params", params_for_save(params, data["id"]))
            if not p["ok"]:
                return data["id"], f"saved, but its mowing settings failed: {p.get('error') or p.get('msg')}"
        if kind == "deadends" and deadend_configured(params):
            p = await self._send(DEADEND_PARAMS_SAVE, deadend_params_payload(params, data["id"]))
            if not p["ok"]:
                return data["id"], f"saved, but its settings failed: {p.get('error') or p.get('msg')}"
        return data["id"], None

    async def _find_new(self, kind: str, payload: dict, known: set) -> int | None:
        """The id of the one feature of `kind` named like `payload` that is not in `known`, from a fresh map."""
        try:
            now = await self.read_map()
        except Exception:
            return None
        hits = [f.get("id") for f in now.get(kind) or [] if f.get("id") not in known
                and (f.get("name") or "") == (payload.get("name") or "")]
        return hits[0] if len(hits) == 1 and isinstance(hits[0], int) else None

    async def write(self, current: dict, draft: dict, gps_ref, allow_big: bool = False) -> dict:
        async with self.lock:
            # Another write or a restore may have held the lock: plan only against the map as it is now.
            try:
                now = await self.read_map()
            except Exception as exc:
                raise WriteRefused(f"could not read the robot's map first ({exc})")
            if not _same_shapes(current, now, gps_ref):
                raise WriteRefused("the robot's map changed while this was waiting; reload Map Studio and try again")
            # Changes the robot cannot take yet (areas, no-vision zones, renames...) do not stop the rest: only the
            # steps are sent, verify() checks everything else on the robot stayed as it was, and the caller keeps the
            # unsent changes in the draft (carry_unsent).
            p = plan(current, draft, gps_ref)
            if not p["steps"]:
                raise WriteRefused("nothing to write" + (" the robot can take yet" if p["blocked"] else ""))
            updates = [s for s in p["steps"] if s["op"] == "update"]
            area_updates = [s for s in updates if s["kind"] == "areas"]
            big = [s for s in p["steps"] if s.get("big")]
            if big and not allow_big:
                raise WriteRefused("these changes need your explicit OK: " +
                                   "; ".join(f"{s['label']}" for s in big))
            for s in updates:
                if s["kind"] == "novisionzones":    # the whole record is sent: it must still be the one planned from
                    was, live = mapstudio.find(current, s["kind"], s["robot_id"]), mapstudio.find(now, s["kind"], s["robot_id"])
                    if live is None or any(live.get(k) != was.get(k) for k in ("enable", "by_pass_level", "name")):
                        raise WriteRefused(f"no-vision zone {s['robot_id']} changed on the robot since Map Studio read "
                                           "it; reload Map Studio and try again")
            records = {}
            if area_updates:
                if self.read_areas is None:
                    raise WriteRefused("area records cannot be read")
                try:
                    records = await self.read_areas()
                except Exception as exc:
                    raise WriteRefused(f"could not read the robot's area records first ({exc})")
                for s in area_updates:
                    rec = records.get(s["robot_id"])
                    if not rec or not all(k in rec for k in ("id", "range", "ref")):
                        raise WriteRefused(f"the robot did not return a full record for area {s['robot_id']}")
                    live = mapstudio.find(current, "areas", s["robot_id"])
                    if (not _same_ref(rec.get("ref"), live.get("ref")) or mapstudio.max_move(
                            mapstudio.local(rec, gps_ref), mapstudio.local(live, gps_ref), True) > VERIFY_M):
                        raise WriteRefused(f"the robot's record of area “{rec.get('name')}” differs from the map the "
                                           "draft was checked against; reload Map Studio and try again")
                    s["payload"] = area_payload(rec, mapstudio.find(draft, "areas", s["draft_id"]), gps_ref)
                    s["original"] = {k: copy.deepcopy(rec[k]) for k in AREA_KEYS if k in rec}
            # The mowing settings of every pathway about to be deleted, to carry over or restore.
            old_params = {}
            backup_note = None
            for s in p["steps"]:
                if s["op"] == "delete" and s["kind"] == "pathways":
                    # A pathway that is being replaced keeps its mow settings; if they cannot be read safely the
                    # write is refused rather than quietly resetting them to the defaults.
                    got, err = await _read(self, s["robot_id"])
                    why = err or read_problem(got)
                    if why:
                        raise WriteRefused(f"could not read the mow settings of pathway {s['robot_id']} ({why}); "
                                           "nothing was sent, so they are not reset - try again")
                    old_params[s["robot_id"]] = got
                if s["op"] == "delete" and s["kind"] == "deadends":
                    # Its settings go to the replacement (or back, on undo); refused rather than quietly lost.
                    got, err = await _read_deadend(self, s["robot_id"])
                    if not err and got.get("id") not in (0, s["robot_id"]):
                        err = f"the reply was for dead end {got.get('id')}"
                    if err:
                        raise WriteRefused(f"could not read the settings of dead end {s['robot_id']} ({err}); "
                                           "nothing was sent - try again")
                    old_params[s["robot_id"]] = got
            if updates:                              # the safety copy is local, unlimited (no robot backup slot)
                self.store.save_version("prewrite", {"at": time.time(), "map": current,
                                                     "area_records": {str(k): v for k, v in records.items()}})
                backup_note = "kept locally (Map Studio → History)"
            done, new_ids, error = [], {}, None
            known = {f.get("id") for f in now.get("novisionzones") or []}
            for s in p["steps"]:
                if s["op"] == "update":
                    r = await self._send(s["cmd"], s["payload"])
                    data = decode_any(r.get("data"))
                    got_id = data.get("id") if isinstance(data, dict) else None
                    if r["sent"]:
                        done.append(s)                  # it may have landed even without a reply: undo covers it
                    if r["ok"] and got_id not in (None, s["robot_id"]):
                        s["created"] = got_id           # the robot made a NEW one instead: never send more
                        what = mapstudio.KIND_LABELS.get(s["kind"], s["kind"]).lower()
                        error = (f"{s['label']}: the robot created a new {what} {got_id} instead of changing "
                                 f"{s['robot_id']}; delete {what} {got_id} in the app")
                        break
                    if not r["ok"]:
                        error = f"{s['label']}: {r.get('error') or r.get('msg') or 'state ' + str(r.get('state'))}"
                        break
                    continue
                if s["op"] == "add":
                    nid, err = await self._save(s["kind"], s["payload"], old_params.get(s["replaces"]), known)
                    if nid is not None:
                        known.add(nid)
                        new_ids[(s["kind"], s["draft_id"])] = nid
                        done.append({**s, "new_id": nid})
                    if err:
                        error = f"{s['label']}: {err}"
                        break
                else:
                    r = await self._send(s["cmd"], s["payload"])
                    if not r["ok"]:
                        error = f"{s['label']}: {r.get('error') or r.get('msg')}"
                        break
                    done.append(s)
            result = {"steps": [{"label": s["label"], "cmd": s["cmd"], "new_id": s.get("new_id")} for s in done],
                      "planned": len(p["steps"]), "error": error, "backup": backup_note}
            if error is None:
                after = await self._read_back()
                if after is None:
                    error = result["error"] = "the map could not be read back after writing; undoing to be safe"
            if error is None:
                problems = verify(current, after, draft, p["steps"], new_ids, gps_ref)
                # Settings carried to a replacement dead end are read back too.
                for s in p["steps"]:
                    if s["op"] == "add" and s["kind"] == "deadends" and deadend_configured(old_params.get(s["replaces"])):
                        nid = new_ids.get((s["kind"], s["draft_id"]))
                        got, err = await _read_deadend(self, nid)
                        want = old_params[s["replaces"]]
                        if got is None or got.get("id") != nid or any(not _same(got.get(k), want.get(k))
                                                                      for k in DEADEND_READ_KEYS):
                            problems.append(f"{s['label']}: its settings did not carry over ({err or 'read back differs'})")
                result["verified"] = not problems
                result["problems"] = problems
                if not problems:
                    up = await self._send("upload_cloud_map_backup", {"email": self.account() or ""})
                    result["cloud_backup"] = bool(up["ok"])
                    result["ok"] = True
                    self.store.event("map_written", f"{len(done)} change(s) written and verified")
                    return result
            result["ok"] = False
            result["rollback"] = await self._rollback(done, current, old_params)
            after = await self._read_back()
            if after is None:
                result["rollback_ok"] = False
                result["after_rollback"] = ["the map could not be read back after undoing; check it in the app"]
            else:
                leftovers = verify(current, after, current, [], {}, gps_ref)
                # re-added features come back with new ids; judge the rollback by shape (and links), not id
                # a zone put back must also be on/off and bypass as before (the shapes alone do not show it)
                for s in done:
                    if s["op"] == "update" and s["kind"] == "novisionzones" and not s.get("created"):
                        got, want = mapstudio.find(after, s["kind"], s["robot_id"]), s["original"]
                        diff = [k for k in ("enable", "by_pass_level", "name") if got is None or (
                                (got.get(k) or "") != (want.get(k) or "") if k == "name" else got.get(k) != want.get(k))]
                        if diff:
                            leftovers.append(f"no-vision zone {s['robot_id']}: {', '.join(diff)} not as before")
                result["rollback_ok"] = (_same_shapes(current, after, gps_ref)
                                         and not link_problems(current, after, current, gps_ref)
                                         and not any(x.startswith("no-vision zone") for x in leftovers)
                                         and not any("settings failed" in n for n in result["rollback"]))
                result["after_rollback"] = leftovers if not result["rollback_ok"] else []
            self.store.event("map_write_failed", f"{error or 'verification failed'}; rolled back "
                                                 f"({'ok' if result['rollback_ok'] else 'CHECK THE MAP'})")
            return result

    async def _read_back(self):
        """get_map after a write; one retry. None when the robot cannot be read."""
        for attempt in (0, 1):
            try:
                return await self.read_map()
            except Exception as exc:
                if attempt:
                    self.store.event("map_read_failed", f"could not read the map back: {exc}")
                    return None
                await asyncio.sleep(3)

    async def _rollback(self, done: list, before: dict, old_params: dict) -> list[str]:
        notes = []
        for s in reversed(done):
            if s["op"] == "update":
                what = mapstudio.KIND_LABELS.get(s["kind"], s["kind"]).lower()
                if s.get("created"):
                    notes.append(f"NOT undone automatically: the robot created {what} {s['created']} - delete it in "
                                 f"the app (the original {what} was not changed)")
                    continue
                r = await self._send(s["cmd"], s["original"], rollback=True)
                notes.append(f"put back the {what} as it was ({s['label']}): "
                             f"{'sent' if r['ok'] else r.get('error') or r.get('msg')} - checked by the read-back below "
                             f"(shape and links{', on/off, bypass and name' if s['kind'] == 'novisionzones' else ''})")
                continue
            if s["op"] == "add":
                r = await self._send(DEL_CMD[s["kind"]], {"id": s["new_id"]}, rollback=True)
                notes.append(f"removed what was added ({s['label']}): {'ok' if r['ok'] else r.get('error') or r.get('msg')}")
            else:
                f = mapstudio.find(before, s["kind"], s["robot_id"])
                nid, err = await self._save(s["kind"], payload_for(s["kind"], f), old_params.get(s["robot_id"]),
                                            {g.get("id") for g in before.get("novisionzones") or []})
                notes.append(f"put back what was deleted ({s['label']}): {'ok' if not err else err}")
        return notes


async def _read(writer, pid: int) -> tuple[dict | None, str | None]:
    """A settings read that never raises: (settings, None) or (None, why)."""
    try:
        data = await writer.read_params(pid)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if not data:
        return None, "the robot returned no settings"
    if data.get("id") != pid:
        return None, f"the reply was for pathway {data.get('id')}"
    return data, None


# -- dead ends ---------------------------------------------------------------------------------------------------------
# read_deadend_params answers {blade_height, blade_speed, en_blade, en_trimmer, id, land_push_pod_place, plan_speed,
# sensor_switch}; a dead end never configured answers id 0 (the robot's defaults: blades on, 0.5 m/s, height 0).
# save_deadend_params sends those plus turn_type, route_angle and route_dis, in the app's order (2026-10-11). The
# read does not report those three; the app sent 0 for each and has no control for them on a dead end that we know of,
# so the panel sends 0 too (if the app ever sets them, a panel save would put them back to 0).
DEADEND_READ_KEYS = ("blade_height", "blade_speed", "en_blade", "en_trimmer", "land_push_pod_place", "plan_speed",
                     "sensor_switch")
DEADEND_FIELDS = [
    {"key": "plan_speed", "label": "Speed", "type": "number", "unit": "m/s", "min": 0.1, "max": 0.7, "step": 0.05,
     "adv_step": 0.01, "levels": [[0.3, "Slowest"], [0.4, "Slow"], [0.5, "Medium"], [0.6, "Fast"], [0.7, "Fastest"]],
     "editable": True, "help": "Driving speed along the dead end, in the app's five levels."},
    {"key": "en_blade", "label": "Blades on", "type": "bool", "editable": True, "help": "Mow while driving it."},
    {"key": "en_trimmer", "label": "Trimmer", "type": "bool", "editable": True, "help": "Run the edge trimmer along it."},
    {"key": "blade_height", "label": "Blade height", "type": "number", "editable": False,
     "known": {"0": "all the way up, 4.0 in"}, "help": "The deck's lift position, as for pathways; set in the app."},
    {"key": "blade_speed", "label": "Blade speed", "type": "number", "editable": False, "simple": False,
     "help": "The robot's raw value, probably percent."},
    {"key": "sensor_switch", "label": "Sensors", "type": "number", "editable": False, "simple": False,
     "help": "The robot's raw value (0 on every dead end seen)."},
]
DEADEND_EDITABLE = {f["key"]: f for f in DEADEND_FIELDS if f["editable"]}


def deadend_configured(read: dict | None) -> bool:
    """Settings worth carrying to a replacement: read, complete, and not just the robot's defaults (id 0)."""
    return bool(read) and read.get("id") not in (0, None) and all(k in read for k in DEADEND_READ_KEYS)


def deadend_params_payload(read: dict, did: int, changes: dict | None = None) -> dict:
    """save_deadend_params for dead end `did`: everything as read, plus the checked changes. ValueError for an unknown
    or read-only field, a value out of range, or a read that lacks a field."""
    missing = [k for k in DEADEND_READ_KEYS if k not in (read or {})]
    if missing:
        raise ValueError(f"the robot's settings lack {', '.join(missing)}")
    vals = {k: read[k] for k in DEADEND_READ_KEYS}
    for k, v in (changes or {}).items():
        f = DEADEND_EDITABLE.get(k)
        if f is None:
            raise ValueError(f"{k} cannot be changed here")
        if f["type"] == "bool":
            if not isinstance(v, bool):
                raise ValueError(f"{f['label']}: on or off")
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or not f["min"] - 1e-9 <= v <= f["max"] + 1e-9:
            raise ValueError(f"{f['label']}: {f['min']}-{f['max']} {f.get('unit', '')}".strip())
        else:
            v = round(float(v), 2)
        vals[k] = v
    return {"blade_speed": vals["blade_speed"], "blade_height": vals["blade_height"], "turn_type": read.get("turn_type", 0),
            "land_push_pod_place": vals["land_push_pod_place"], "route_angle": 0, "route_dis": 0.0,
            "en_blade": vals["en_blade"], "plan_speed": vals["plan_speed"], "sensor_switch": vals["sensor_switch"],
            "en_trimmer": vals["en_trimmer"], "id": did}


async def _read_deadend(writer, did: int) -> tuple[dict | None, str | None]:
    """(settings, None) or (None, why). id 0 in the reply means the robot's defaults for a dead end never set (the
    recorder only lets one through when no earlier read can still be answering). On/off values come back as booleans
    even if the robot ever sends 1 / 0."""
    if writer.read_deadend is None:
        return None, "dead-end settings cannot be read"
    try:
        data = await writer.read_deadend(did)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if not data:
        return None, "the robot returned no settings"
    if data.get("id") not in (0, did):
        return None, f"the reply was for dead end {data.get('id')}"
    data = dict(data)
    for k in ("en_blade", "en_trimmer"):
        if data.get(k) in (0, 1) and not isinstance(data.get(k), bool):
            data[k] = bool(data[k])
    if data.get("id") == did:
        missing = [k for k in DEADEND_READ_KEYS if k not in data]
        if missing:
            return None, f"the robot's settings lack {', '.join(missing)}"
    return data, None


async def _deadend_exists(writer, did: int) -> str | None:
    """Why `did` is not a dead end on the robot right now (a fresh get_map), or None. The robot reuses freed ids and
    answers id 0 for any id it has no settings for, so the cached map is not enough."""
    try:
        now = await writer.read_map()
    except Exception as exc:
        return f"could not read the robot's map first ({exc})"
    if not any(f.get("id") == did for f in now.get("deadends") or []):
        return f"dead end {did} is not on the robot's map any more; reload Map Studio"
    return None


async def write_deadend_params(writer: "MapWriter", did: int, changes: dict, check=None) -> dict:
    """Change a dead end's settings: read fresh, save the full set with the changes, read back and compare every
    field; if the robot kept something else, put the previous values back. Every outcome is an event."""
    async with writer.lock:
        why = check() if check else None
        if why:
            raise WriteRefused(why)
        gone = await _deadend_exists(writer, did)
        if gone:
            raise WriteRefused(gone)
        before, err = await _read_deadend(writer, did)
        if before is None:
            raise WriteRefused(f"could not read dead end {did}'s settings first ({err})")
        payload = deadend_params_payload(before, did, changes)          # ValueError on bad input
        if all(_same(payload[k], before.get(k)) for k in changes):
            return {"ok": True, "unchanged": True, "before": before, "after": before}
        r = await writer._send(DEADEND_PARAMS_SAVE, payload)
        what = ", ".join(f"{k} {before.get(k)} -> {payload[k]}" for k in changes)
        if not r["sent"]:
            writer.store.event("deadend_settings_failed", f"dead end {did}: not sent ({r.get('error')})")
            return {"ok": False, "error": f"not sent ({r.get('error')})", "before": before}
        after, err = await _read_deadend(writer, did)
        if after is None:
            writer.store.event("deadend_settings_unverified", f"dead end {did}: {what}; sent, but the read-back failed "
                                                              f"({err}) - check it in the app")
            return {"ok": False, "unverified": True, "before": before,
                    "error": f"sent, but could not read it back ({err}); reload to see what the robot has"}
        # After a save the robot answers with the dead end's own id; id 0 means it did not keep it (or is someone
        # else's late answer): either way not saved.
        wrong = (["id"] if after.get("id") != did else []) + \
            [k for k in DEADEND_READ_KEYS if not _same(after.get(k), payload[k])]
        note = "" if r["ok"] else f" (no confirmation from the robot: {r.get('error') or r.get('msg')})"
        if not wrong:
            writer.store.save_version(f"deadend_params:{did}", before)
            writer.store.save_version(f"deadend_params:{did}", after)
            writer.store.event("deadend_settings_written", f"dead end {did}: {what}{note}")
            return {"ok": True, "before": before, "after": after}
        back = await writer._send(DEADEND_PARAMS_SAVE, deadend_params_payload(before, did))
        restored, _ = await _read_deadend(writer, did)
        rollback_ok = back["ok"] and restored is not None and all(_same(restored.get(k), before.get(k))
                                                                  for k in DEADEND_READ_KEYS)
        writer.store.event("deadend_settings_failed", f"dead end {did}: read back differs ({', '.join(wrong)}); previous "
                                                      f"settings {'restored' if rollback_ok else 'NOT restored - check the app'}")
        return {"ok": False, "error": f"the robot kept different values for {', '.join(wrong)}", "before": before,
                "after": after, "rollback_ok": rollback_ok}


def _differs(a: dict, b: dict) -> list[str]:
    return [k for k in COMPARE_KEYS if not _same(a.get(k), b.get(k))]


async def write_pathway_params(writer: "MapWriter", pid: int, changes: dict, check=None) -> dict:
    """Change a pathway's mowing settings: read them fresh, save the full set with the changes, read back and
    compare EVERY field; if the robot ended up with something else, put the previous settings back. A save whose
    reply is lost is still read back (it may have been applied). Every outcome is logged as an event."""
    async with writer.lock:
        why = check() if check else None      # the gate again, now that no other write can run
        if why:
            raise WriteRefused(why)
        before, err = await _read(writer, pid)
        if before is None:
            raise WriteRefused(f"could not read pathway {pid}'s settings first ({err})")
        payload = pathway_params_payload(before, changes, pid)       # ValueError on bad input or unsafe read
        if not any(not _same(get_path(payload, k), get_path(before, k)) for k in changes):
            return {"ok": True, "unchanged": True, "before": before, "after": before}
        r = await writer._send("save_mower_pathway_params", payload)
        what = ", ".join(f"{k} {get_path(before, k)} -> {get_path(payload, k)}" for k in changes)
        if not r["sent"]:
            writer.store.event("pathway_settings_failed", f"pathway {pid}: not sent ({r.get('error')})")
            return {"ok": False, "error": f"not sent ({r.get('error')})", "before": before}
        after, err = await _read(writer, pid)
        if after is None:
            writer.store.event("pathway_settings_unverified", f"pathway {pid}: {what}; sent, but the read-back "
                                                              f"failed ({err}) - check it in the app")
            return {"ok": False, "unverified": True, "before": before,
                    "error": f"sent, but could not read it back ({err}); reload to see what the robot has"}
        target = {**before, **{k: payload[k] for k in COMPARE_KEYS}}
        wrong = _differs(after, target)
        note = "" if r["ok"] else f" (no confirmation from the robot: {r.get('error') or r.get('msg')})"
        if not wrong:
            writer.store.save_version(f"pathway_params:{pid}", before)
            writer.store.save_version(f"pathway_params:{pid}", after)
            writer.store.event("pathway_settings_written", f"pathway {pid}: {what}{note}")
            return {"ok": True, "before": before, "after": after}
        if not _differs(after, before):
            again, _ = await _read(writer, pid)       # a late reply can hand back the pre-save state: look once more
            if again is not None and not _differs(again, target):
                writer.store.save_version(f"pathway_params:{pid}", before)
                writer.store.save_version(f"pathway_params:{pid}", again)
                writer.store.event("pathway_settings_written", f"pathway {pid}: {what}{note}")
                return {"ok": True, "before": before, "after": again}
            writer.store.event("pathway_settings_failed", f"pathway {pid}: not applied{note}")
            return {"ok": False, "error": f"the robot did not apply it{note}", "before": before, "after": after}
        back = await writer._send("save_mower_pathway_params", params_for_save(before, pid))
        restored, err = await _read(writer, pid)
        rollback_ok = back["ok"] and restored is not None and not _differs(restored, before)
        writer.store.event("pathway_settings_failed", f"pathway {pid}: read back differs ({', '.join(wrong)}); "
                                                      f"previous settings {'restored' if rollback_ok else 'NOT restored - check the app'}")
        return {"ok": False, "error": f"the robot kept different values for {', '.join(wrong)}", "before": before,
                "after": after, "rollback_ok": rollback_ok}


def carry_unsent(after: dict, draft: dict, blocked: list) -> dict:
    """The robot's map after a write, with the draft's changes that were not sent laid back on top, so they stay in
    the draft. Untouched kinds and ids are the robot's own."""
    out = copy.deepcopy(after)
    for b in blocked:
        kind, fid = b["kind"], b["id"]
        lst = out.setdefault(kind, [])
        idx = next((i for i, f in enumerate(lst) if f.get("id") == fid), None)
        want = mapstudio.find(draft, kind, fid)
        if want is None:                        # removed in the draft
            if idx is not None:
                lst.pop(idx)
        elif idx is None:                       # added in the draft
            lst.append(copy.deepcopy(want))
        else:                                   # changed in the draft
            lst[idx] = copy.deepcopy(want)
    return out


def _same_shapes(a: dict, b: dict, gps_ref) -> bool:
    """True when every feature of a has a twin in b (same kind and name, shape within VERIFY_M) and vice versa."""
    def items(m):
        return [(k, f.get("name") or "", mapstudio.local(f, gps_ref)) for k, f in mapstudio.iter_features(m)]
    left, right = items(a), items(b)
    if len(left) != len(right):
        return False
    pool = list(right)
    for k, name, pts in left:
        match = next((r for r in pool if r[0] == k and r[1] == name and
                      mapstudio.max_move(pts, r[2], k in mapstudio.POLYGONS) <= VERIFY_M), None)
        if match is None:
            return False
        pool.remove(match)
    return True


def plan_payload(plans: list, area_ids, body: dict, pid: int | None = None) -> dict:
    """save_plan for a new plan (pid None: the next id up, as the app does) or an existing one. Raises ValueError."""
    known = {p.get("id"): p for p in plans if isinstance(p.get("id"), int)}
    old = known.get(pid) if pid is not None else None
    if pid is not None and old is None:
        raise ValueError(f"plan {pid} is not on the robot")
    name = str(body.get("name", old.get("name") if old else "") or "").strip()
    if not name:
        raise ValueError("give the plan a name")
    if len(name) > PLAN_NAME_MAX:
        raise ValueError(f"keep the name to {PLAN_NAME_MAX} characters")
    if any(str(p.get("name") or "").strip().lower() == name.lower() for i, p in known.items() if i != pid):
        raise ValueError(f"there is already a plan called “{name}”")
    ids = body.get("areaIds", old.get("areaIds") if old else None)
    if not isinstance(ids, list) or not ids:
        raise ValueError("pick at least one area")
    if any(not isinstance(a, int) or isinstance(a, bool) for a in ids) or len(set(ids)) != len(ids):
        raise ValueError("areaIds must be distinct area ids")
    missing = [a for a in ids if a not in set(area_ids)]
    if missing:
        raise ValueError(f"no area {', '.join(map(str, missing))} on the robot")
    order = body.get("enable_self_order", old.get("enable_self_order", True) if old else True)
    if not isinstance(order, bool):
        raise ValueError("enable_self_order is true or false")
    return {"id": pid if pid is not None else max(known, default=0) + 1, "areaIds": ids, "name": name,
            "enable_self_order": order}


def _plan_differs(got: dict | None, want: dict) -> bool:
    """The robot may keep the areas in its own order; the set, the name and self-order must match."""
    return not got or got.get("name") != want.get("name") or \
        sorted(got.get("areaIds") or []) != sorted(want.get("areaIds") or []) or \
        got.get("enable_self_order", True) != want.get("enable_self_order", True)


async def write_plan(writer: "MapWriter", read_plans, area_ids, body: dict, pid: int | None = None, check=None) -> dict:
    """Create (pid None) or change a plan: read the plans fresh, save, read them back and compare. An edit the robot
    got wrong is put back as it was; a new plan cannot be taken back (no delete command), so that is only reported.
    Every outcome is logged as an event."""
    async with writer.lock:
        why = check() if check else None      # the gate again, now that no other write can run
        if why:
            raise WriteRefused(why)
        try:
            before = await read_plans()
        except Exception as exc:
            raise WriteRefused(f"could not read the plans first ({str(exc) or type(exc).__name__})")
        payload = plan_payload(before, area_ids, body, pid)              # ValueError on bad input
        old = next((p for p in before if p.get("id") == payload["id"]), None)
        if old and not _plan_differs(old, payload) and old.get("areaIds") == payload["areaIds"]:
            return {"ok": True, "unchanged": True, "plan": old, "plans": before}
        what = f"plan {payload['id']} “{payload['name']}” ({'new' if old is None else 'changed'}), areas {payload['areaIds']}"
        r = await writer._send(PLAN_SAVE, payload)
        if not r["sent"]:
            writer.store.event("plan_save_failed", f"{what}: not sent ({r.get('error')})")
            return {"ok": False, "error": f"not sent ({r.get('error')})", "plans": before}
        note = "" if r["ok"] else f" (no confirmation from the robot: {r.get('error') or r.get('msg')})"
        try:
            after = await read_plans()
        except Exception as exc:
            writer.store.event("plan_save_unverified", f"{what}: sent, but the read-back failed{note}")
            return {"ok": False, "unverified": True, "plans": before,
                    "error": f"sent, but could not read the plans back ({str(exc) or type(exc).__name__}); reload to see"}
        got = next((p for p in after if p.get("id") == payload["id"]), None)
        if not _plan_differs(got, payload):
            writer.store.event("plan_saved", f"{what}{note}")
            return {"ok": True, "plan": got, "plans": after, "created": old is None}
        if old is None:
            writer.store.event("plan_save_failed", f"{what}: not on the robot afterwards{note}")
            return {"ok": False, "error": f"the robot does not list the new plan{note}", "plans": after}
        back = await writer._send(PLAN_SAVE, {k: old.get(k) for k in PLAN_KEYS})
        try:
            restored = next((p for p in await read_plans() if p.get("id") == old["id"]), None)
        except Exception:
            restored = None
        rollback_ok = back["ok"] and not _plan_differs(restored, old)
        writer.store.event("plan_save_failed", f"{what}: read back differs{note}; previous plan "
                                               f"{'restored' if rollback_ok else 'NOT restored - check the app'}")
        return {"ok": False, "error": f"the robot kept something else{note}", "plans": after, "rollback_ok": rollback_ok}


async def delete_plan(writer: "MapWriter", read_plans, pid: int, check=None) -> dict:
    """Delete a plan (del_plan) and read the plans back to confirm it is gone. The plan as it was is in the event
    detail, so it can be made again with the same areas."""
    async with writer.lock:
        why = check() if check else None
        if why:
            raise WriteRefused(why)
        try:
            before = await read_plans()
        except Exception as exc:
            raise WriteRefused(f"could not read the plans first ({str(exc) or type(exc).__name__})")
        old = next((p for p in before if p.get("id") == pid), None)
        if old is None:
            raise ValueError(f"plan {pid} is not on the robot")
        what = f"plan {pid} “{old.get('name')}” (areas {old.get('areaIds')})"
        r = await writer._send(PLAN_DELETE, {"id": pid})
        if not r["sent"]:
            writer.store.event("plan_delete_failed", f"{what}: not sent ({r.get('error')})")
            return {"ok": False, "error": f"not sent ({r.get('error')})", "plans": before}
        note = "" if r["ok"] else f" (no confirmation from the robot: {r.get('error') or r.get('msg')})"
        try:
            after = await read_plans()
        except Exception as exc:
            writer.store.event("plan_delete_unverified", f"{what}: sent, but the read-back failed{note}")
            return {"ok": False, "unverified": True, "plans": before,
                    "error": f"sent, but could not read the plans back ({str(exc) or type(exc).__name__}); reload to see"}
        if any(p.get("id") == pid for p in after):
            writer.store.event("plan_delete_failed", f"{what}: still on the robot{note}")
            return {"ok": False, "error": f"the robot still lists it{note}", "plans": after}
        writer.store.event("plan_deleted", f"{what}{note}")
        return {"ok": True, "deleted": old, "plans": after}
