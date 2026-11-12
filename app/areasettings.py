"""An area's mowing settings: read_mower_area_params {id} / save_mower_area_params (2026-10-08).

Captured from the Yarbo app (docs/dev/map-writes.md, capture 2026-10-07 09:34): the save carries the
full record as a read returns it, minus clean_times_dis, current_blade_height and move2area_offset_count, with
mapping_module_type: null; the app then saves the area itself, unchanged (save_clean_area), and uploads the cloud copy.
An area whose settings were never saved reads back with id 0: the robot's defaults.

Editable here: only fields whose meaning and unit were matched to the app (the changes in that capture: edge laps 4,
edge offset 1 in, no-go buffer 4 in, speed 0.7 m/s, mowing angle 20 then 30) and the trimmer switch. The rest is shown
read-only. Blade height stays read-only until a second pair from the app pins its scale (25 shows as 3.2 in; that is
the only pair known).

A write reads the settings fresh, saves the full record with the changes, saves the area unchanged as the app does,
reads the settings back and compares every field; if the robot kept something else, the previous settings go back.
The pathway links are checked after the area save (the robot re-links pathways on every area save)."""
import copy
import time

import copy as _copy

from . import geo
from .mapstudio import AVOIDANCE_OPTIONS
from .backups import PARAMS_DROP, params_payload
from .mapwrite import AREA_KEYS, AREA_SAVE, WriteRefused, _same, link_problems

READ, SAVE = "read_mower_area_params", "save_mower_area_params"
PASS_KEYS = {"blade_height", "blade_speed", "land_push_pod_place", "plan_speed", "route_angle", "route_dis", "turn_type"}
# Every key a read returns on this robot (2026-10-08, firmware body 3.2.35). A read with others is refused: a save
# built from it could reset what this panel does not know about.
READ_KEYS = {"auto_step_angle", "avoidance_level", "buffer_dis", "clean_times", "clean_times_dis", "contract_dis",
             "current_blade_height", "current_index", "double_clean_params", "edge_area_first", "edge_bypass_sw",
             "edge_circles", "edge_direction_mode", "en_trimmer", "enable_clean_times_dis", "enable_explore",
             "first_clean_params", "gap", "id", "m1pro_sensor_switch", "move2area_max_offset", "move2area_offset_count",
             "move2area_offset_enable", "move2area_step_offset", "mower_ngz_edge_sw", "mower_target_height_count",
             "ngz_expand_dis", "parallel_angle_max", "parallel_angle_min", "parallel_offset_mode", "route_order",
             "sensor_switch", "side_enable"}
INCH = 0.0254
_IN = {"unit": "in", "per": INCH, "step": 1, "adv_step": 0.1, "digits": 1}     # stored in metres, shown in inches
# The app's five speed levels (Yarbo forum, "Settings to improve mowing efficiency": Slowest to Fastest). 0.5 m/s is the
# robot's default and 0.7 m/s is what the app wrote for All Yard; the levels between are the even steps.
SPEED_LEVELS = [[0.3, "Slowest"], [0.4, "Slow"], [0.5, "Medium"], [0.6, "Fast"], [0.7, "Fastest"]]

# The settings in the app's own sections, order and words (screenshots of the app's All Yard, 2026-10-08 14:18, read
# against the robot's record of All Yard at the same time; nothing was saved in between). "control" says how the app
# shows it; an option whose robot value is not matched yet is listed with value None and cannot be chosen; a "pending"
# row names an app setting whose robot field is not identified yet. Section "Not in the app": the robot's other values.
# "fine": the panel's finer steps, shown with Fine control on.
AREA_FIELDS = [
    {"key": "first_clean_params.plan_speed", "section": "Area settings", "label": "Moving speed", "control": "levels",
     "type": "number", "unit": "m/s", "min": 0.1, "max": 0.7, "step": 0.05, "fine": 0.01, "levels": SPEED_LEVELS,
     "editable": True, "help": "Fastest is 0.7 m/s (All Yard); the other levels are the even steps down to 0.3. Fine "
                               "control sets any speed up to the robot's 0.7 m/s."},
    {"key": "avoidance_level", "section": "Area settings", "label": "Obstacle avoidance mode", "control": "choice",
     "type": "number", "options": AVOIDANCE_OPTIONS, "strict": True, "editable": False,
     "help": "1 is Moderate bypass; the app's other modes are not matched yet."},
    {"key": None, "section": "Area settings", "label": "Boundary obstacle avoidance", "control": "pending",
     "help": "A switch in the app (off on All Yard); its robot field is not identified yet."},
    {"key": "first_clean_params.blade_height", "section": "Area settings", "label": "Mowing height", "control": "stepper",
     "type": "number", "editable": False, "known": {"0": "all the way up, 4.0 in", "25": "3.3 in"},
     "help": "The robot stores the deck's lift position: 25 shows as 3.3 in. One more pair pins the scale and unlocks it "
             "(the Mower Pro cuts 0.8-4.0 in; Yarbo advises not below 2.7 in)."},
    {"key": None, "section": "Area settings", "label": "Target height", "control": "pending",
     "help": "Lowers the height from max to the mowing height over 5 sessions (mower_target_height_count is 5); the switch "
             "is probably enable_clean_times_dis (it came on in the 2026-10-08 recording), not confirmed yet."},
    {"key": None, "section": "Area settings", "label": "Route pattern", "control": "pending",
     "options": [[None, "Parallel"], [None, "Spiral"], [None, "Checkboard"], [None, "Diamond"], [None, "Adaptive"]],
     "help": "Checkboard on All Yard; which robot field holds it is not identified yet."},
    {"key": "first_clean_params.route_angle", "section": "Area settings", "label": "Route angle", "control": "stepper",
     "type": "number", "unit": "°", "min": 0, "max": 179, "step": 1, "integer": True, "editable": True,
     "help": "The direction of the mowing lines."},
    # the app's Advanced Settings
    {"key": "edge_direction_mode", "section": "Advanced settings", "label": "Perimeter direction", "control": "choice",
     "type": "number", "options": [[0, "Follow original path"], [None, "Counterclockwise"], [None, "Clockwise"]],
     "strict": True, "editable": False, "help": "0 is Follow original path; the other two are not matched yet."},
    {"key": "first_clean_params.turn_type", "section": "Advanced settings", "label": "Turning mode", "control": "choice",
     "type": "number", "options": [[1, "Smart Turn"], [None, "U-Turn"], [None, "Zero Turn"]], "strict": True,
     "editable": False, "help": "Smart Turn (a careful 3-point turn: gentlest, slowest) is 1. U-Turn (wide, quicker) and "
                                "Zero Turn (on the spot: quickest, hardest on grass) are not matched yet."},
    {"key": "gap", "section": "Advanced settings", "label": "Overlap", "control": "choice", "type": "number",
     "options": [[0.5, "0 in"], [0.4, "4 in"], [0.3, "8 in"]], "strict": True, "editable": True,
     "help": "The robot stores the spacing between lines: 0.5 m less the overlap (8 in = 0.30 m on All Yard, 4 in = the "
             "0.40 m default)."},
    {"key": "move2area_offset_enable", "section": "Advanced settings", "label": "Transit path offset", "control": "bool",
     "type": "bool", "editable": True,
     "help": "Spreads the robot's trips across parallel tracks to prevent ruts."},
    {"key": "move2area_max_offset", "section": "Advanced settings", "label": "Range offset", "control": "slider",
     "type": "number", "unit": "m", "show": {**_IN, "step": 1}, "min": 0, "max": 2.0, "editable": True,
     "help": "How far the tracks spread (the app: 0-78.7 in)."},
    {"key": "move2area_step_offset", "section": "Advanced settings", "label": "Step offset", "control": "slider",
     "type": "number", "unit": "m", "show": {**_IN, "step": 0.1}, "min": 0.1, "max": 0.5, "editable": True,
     "help": "How far each trip shifts (the app: 3.9-19.7 in)."},
    {"key": "first_clean_params.land_push_pod_place", "section": "Advanced settings", "label": "Module gap",
     "control": "choice", "type": "number", "options": [[50, "Loose"], [None, "Medium"], [None, "Tight"]], "strict": True,
     "editable": False, "known": {"30": "Medium or Tight"},
     "help": "Less clogging, slightly different cutting height. Loose is 50 (recorded 2026-10-08: it went 50 -> 30 -> 50 "
             "with the app's module gap); Medium and Tight are 30 and one other number, not told apart yet."},
    {"key": "edge_area_first", "section": "Advanced settings", "label": "Mowing order", "control": "choice", "type": "bool",
     "options": [[True, "Boundary, then area"], [False, "Area, then boundary"]], "editable": True,
     "help": "Boundary laps before the area, or after it (recorded 2026-10-08)."},
    {"key": "mower_ngz_edge_sw", "section": "Advanced settings", "label": "No-go zone edges", "control": "bool",
     "type": "bool", "editable": True, "help": "When on, the robot mows along the no-go zones' edges; off skips them."},
    {"key": "route_order", "section": "Advanced settings", "label": "Edges order", "control": "choice", "type": "number",
     "options": [[0, "Perimeter first"], [1, "NGZ edge first"]], "strict": True, "editable": True,
     "help": "Which edge laps go first: the perimeter's or the no-go zones' (recorded 2026-10-08)."},
    {"key": "edge_circles", "section": "Advanced settings", "label": "Perimeter mowing laps", "control": "choice",
     "type": "number", "options": [[2, "2"], [3, "3"], [4, "4"]], "min": 0, "max": 6, "integer": True, "editable": True,
     "help": "Laps around the boundary (the app offers 2, 3 or 4; Fine control 0-6). The app's on/off switch for the laps "
             "is not identified yet."},
    {"key": "contract_dis", "section": "Advanced settings", "label": "Perimeter buffer", "control": "slider", "type": "number",
     "unit": "m", "show": _IN, "min": 0, "max": 0.4064, "editable": True,
     "help": "The safety distance from the outer boundary (the app: 0-16 in)."},
    {"key": "ngz_expand_dis", "section": "Advanced settings", "label": "NGZ buffer", "control": "slider", "type": "number",
     "unit": "m", "show": _IN, "min": 0, "max": 0.4064, "editable": True,
     "help": "The safety distance from no-go zones inside the area (the app: 0-16 in)."},
    {"key": "buffer_dis", "section": "Advanced settings", "label": "Extend area boundary", "control": "slider",
     "type": "number", "unit": "m", "show": {"unit": "ft", "per": 0.3048, "step": 0.1, "fine": 0.1, "digits": 1},
     "min": 0, "max": 1.0, "editable": True,
     "help": "Lets turns go past the boundary while the mowing path stays the same (the app: 0-3.3 ft; recorded 2026-10-08)."},
    # the robot's own values the app does not show
    {"key": "first_clean_params.blade_speed", "section": "Not in the app", "label": "Blade speed", "control": "number",
     "type": "number", "unit": "%", "min": 50, "max": 100, "step": 1, "integer": True, "editable": True,
     "help": "The blade motors' speed (80 % by default; they ran at 80 on the 10-05 mow)."},
    {"key": "en_trimmer", "section": "Not in the app", "label": "Trimmer", "control": "bool", "type": "bool", "editable": True,
     "help": "The trimmer switch in the robot's record (on everywhere)."},
    {"key": "auto_step_angle", "section": "Not in the app", "label": "Direction step", "control": "value", "type": "number",
     "unit": "°", "editable": False, "help": "90 on All Yard, 10 by default; probably tied to the route pattern."},
    {"key": "parallel_offset_mode", "section": "Not in the app", "label": "Parallel offset mode", "control": "value",
     "type": "number", "editable": False, "help": "2 everywhere; perhaps the route pattern."},
    {"key": "side_enable", "section": "Not in the app", "label": "Side enable", "control": "value", "type": "bool",
     "editable": False, "help": "On everywhere; perhaps the perimeter laps switch."},
    {"key": "m1pro_sensor_switch", "section": "Not in the app", "label": "Sensor switches", "control": "value",
     "type": "number", "editable": False, "help": "Bit flags (139 on All Yard, 136 by default); perhaps boundary obstacle "
                                                  "avoidance and target height."},
    {"key": "clean_times", "section": "Not in the app", "label": "Passes", "control": "value", "type": "number",
     "editable": False, "help": "1 everywhere."},
    {"key": "double_clean_params.route_angle", "section": "Not in the app", "label": "Second pass angle", "control": "value",
     "type": "number", "unit": "°", "editable": False, "help": "90 everywhere."},
]
EDITABLE = {f["key"]: f for f in AREA_FIELDS if f.get("editable")}
LABELS = {f["key"]: f["label"] for f in AREA_FIELDS if f.get("key")}
AREA_LABELS = {1: "Snow Blower", 2: "Blower", 3: "Lawn Mower", 4: "SAM"}     # the area record's labels, in the app's order
MOWER_LABEL = 3


def other_head_block(labels) -> str | None:
    """Why the mower's settings must not be saved to this area, or None (ADR 0009, #128). An area labelled only for
    other heads (a snow blower's driveway) keeps its settings in a record that is not captured yet, so the mower's
    would be the wrong ones. No labels at all (new areas, the demo) stays editable: it is not tied to another head.
    Labels that could not be read fail closed."""
    if not isinstance(labels, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in labels):
        return "the robot's record of this area has no labels this panel can read, so it may not be a mowing area"
    if not labels or MOWER_LABEL in labels:
        return None
    names = ", ".join(AREA_LABELS.get(x, f"label {x}") for x in labels)
    return f"this area is labelled {names}, not Lawn Mower, and these are the mower's settings"


def get(rec: dict, key: str):
    for part in key.split("."):
        rec = rec.get(part) if isinstance(rec, dict) else None
    return rec


def _put(rec: dict, key: str, value) -> None:
    *path, last = key.split(".")
    for part in path:
        rec = rec[part]
    rec[last] = value


def flat(rec: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in rec.items():
        if isinstance(v, dict) and k in ("first_clean_params", "double_clean_params"):
            out.update(flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def read_problem(rec: dict) -> str | None:
    """A read the editor cannot safely build a full save from (missing or unknown fields)."""
    missing, extra = sorted(READ_KEYS - set(rec)), sorted(set(rec) - READ_KEYS)
    for name in ("first_clean_params", "double_clean_params"):
        sub = rec.get(name)
        if isinstance(sub, dict):
            missing += [f"{name}.{k}" for k in sorted(PASS_KEYS - set(sub))]
            extra += [f"{name}.{k}" for k in sorted(set(sub) - PASS_KEYS)]
        elif name in rec:
            missing.append(f"{name} (not a record)")
    if missing:
        return f"the robot's settings lack {', '.join(missing)}"
    if extra:
        return f"the robot reports fields this panel does not know ({', '.join(extra)}); a save could reset them"
    return None


def payload(current: dict, changes: dict, aid: int) -> dict:
    """The save: everything as the robot has it now, plus the checked changes. ValueError for an unknown or read-only
    field, a bad value, or a read that cannot be saved back whole."""
    why = read_problem(current)
    if why:
        raise ValueError(why)
    out = params_payload(current, aid)
    for k, v in changes.items():
        f = EDITABLE.get(k)
        if f is None:
            raise ValueError(f"{k} cannot be changed here")
        if f["type"] == "bool":
            if not isinstance(v, bool):
                raise ValueError(f"{f['label']}: true or false")
            _put(out, k, v)
            continue
        if f.get("strict"):                      # only the app's own choices
            allowed = [o[0] for o in f["options"] if o[0] is not None]
            if isinstance(v, bool) or not any(isinstance(v, (int, float)) and abs(v - a) < 1e-6 for a in allowed):
                raise ValueError(f"{f['label']}: one of {', '.join(o[1] for o in f['options'] if o[0] is not None)}")
            _put(out, k, next(a for a in allowed if abs(v - a) < 1e-6))
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not f["min"] <= v <= f["max"]:
            raise ValueError(f"{f['label']}: {f['min']} to {f['max']} {f.get('unit', '')}".rstrip())
        if f.get("integer"):
            if abs(v - round(v)) > 1e-9:
                raise ValueError(f"{f['label']}: a whole number")
            _put(out, k, int(round(v)))
        else:
            _put(out, k, round(float(v), 4))
    return out


def differs(read: dict, want: dict) -> list[str]:
    """The fields of a read that do not match the saved record (computed and save-only keys left out)."""
    a, b = flat(read), flat(want)
    skip = set(PARAMS_DROP) | {"id", "mapping_module_type"}
    return [k for k in sorted(b) if k not in skip and not _same(a.get(k), b.get(k))]


async def read_settings(read, aid: int) -> tuple[dict | None, str | None]:
    """(settings, None) or (None, why); never raises. A reply for another area is refused; id 0 is the defaults."""
    try:
        data = await read(aid)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if not isinstance(data, dict):
        return None, "the robot returned no settings"
    if data.get("id") not in (aid, 0):
        return None, f"the reply was for area {data.get('id')}"
    return data, None


def area_record(rec: dict) -> dict:
    return {k: copy.deepcopy(rec[k]) for k in AREA_KEYS if k in rec}


async def write(writer, aid: int, changes: dict, read, gps_ref, check=None) -> dict:
    """read: async (area id) -> settings dict (serialised by the recorder). Raises WriteRefused before anything is
    sent, ValueError for bad input; after the first send it returns what happened."""
    async with writer.lock:
        why = check() if check else None
        if why:
            raise WriteRefused(why)
        before, err = await read_settings(read, aid)
        if before is None:
            raise WriteRefused(f"could not read the area's settings first ({err})")
        new = payload(before, changes, aid)
        if not any(not _same(get(new, k), get(before, k)) for k in changes):
            return {"ok": True, "unchanged": True, "before": before, "after": before}
        try:
            record = (await writer.read_areas()).get(aid)
            map_before = await writer.read_map()
        except Exception as exc:
            raise WriteRefused(f"could not read the area itself first ({exc})")
        if record is None:
            raise WriteRefused("the robot has no record of this area")
        why = other_head_block(record.get("labels"))                        # the labels as they are now, under the lock
        if why:
            raise WriteRefused(why)
        what =", ".join(f"{k} {get(before, k)} -> {get(new, k)}" for k in changes)
        writer.store.save_version(f"area_params:{aid}", {"at": time.time(), "settings": before})   # kept before any send
        sent = await writer._send(SAVE, new)
        if not sent["sent"]:
            writer.store.event("area_settings_failed", f"area {aid}: not sent ({sent.get('error')})")
            return {"ok": False, "error": f"not sent ({sent.get('error')})", "before": before}
        area = await writer._send(AREA_SAVE, area_record(record))          # as the app does after a settings save
        after, err = await read_settings(read, aid)
        if after is not None and not differs(after, before) and differs(after, new):
            again, _ = await read_settings(read, aid)                       # a late reply can hand back the old state
            after = again or after
        problems = []
        try:
            problems = link_problems(map_before, await writer.read_map(), map_before, gps_ref)
        except Exception as exc:
            problems.append(f"could not read the map back to check the pathway links ({exc})")
        note = "" if sent["ok"] else f" (no confirmation from the robot: {sent.get('error') or sent.get('msg')})"
        if after is None:
            writer.store.event("area_settings_unverified", f"area {aid}: {what}; sent, but the read-back failed ({err})")
            return {"ok": False, "unverified": True, "before": before, "problems": problems,
                    "error": f"sent, but could not read it back ({err}); reload to see what the robot has"}
        wrong = differs(after, new)
        if not wrong:
            writer.store.save_version(f"area_params:{aid}", {"at": time.time(), "settings": after})
            writer.store.event("area_settings_written", f"area {aid}: {what}{note}"
                               + ("" if area["ok"] else "; the follow-up area save got no confirmation")
                               + (f"; {'; '.join(problems)}" if problems else ""))
            try:
                await writer._send("upload_cloud_map_backup", {"email": writer.account() or ""})
            except Exception:
                pass
            return {"ok": True, "before": before, "after": after, "problems": problems}
        old = params_payload(before, aid)
        back = await writer._send(SAVE, old, rollback=True)
        await writer._send(AREA_SAVE, area_record(record), rollback=True)
        restored, _ = await read_settings(read, aid)
        rollback_ok = bool(back["ok"] and restored is not None and not differs(restored, old))
        writer.store.event("area_settings_failed", f"area {aid}: read back differs ({', '.join(wrong)}); previous "
                                                   f"settings {'restored' if rollback_ok else 'NOT restored - check the app'}")
        return {"ok": False, "error": f"the robot kept different values for {', '.join(wrong)}", "before": before,
                "after": after, "rollback_ok": rollback_ok, "problems": problems}


# -- the route preview (preview_snowbot_area_path) -------------------------------------------------------------------
PREVIEW = "preview_snowbot_area_path"
PREVIEW_KEYS = ("algorithm_type", "id", "leaf_piles", "push_snow_dir", "range", "ref", "slope_degrees", "snowPiles")


def preview_payload(record: dict, outline: list | None = None) -> dict:
    """What the app sends: the area's record (read_all_clean_area), optionally with another outline (a draft's)."""
    out = {k: _copy.deepcopy(record.get(k)) for k in PREVIEW_KEYS}
    if outline is not None:
        out["range"] = _copy.deepcopy(outline)
    return out


def route_summary(data: dict, ref: dict) -> dict:
    """The reply as a GeoJSON line (in the robot's frame around the area's ref), its length and the robot's time."""
    pts = [p for p in data.get("path") or [] if isinstance(p, dict) and "x" in p and "y" in p]
    origin = (float(ref["latitude"]), float(ref["longitude"]))
    length = sum(((b["x"] - a["x"]) ** 2 + (b["y"] - a["y"]) ** 2) ** 0.5 for a, b in zip(pts, pts[1:]))
    return {"route": {"type": "Feature", "properties": {},
                      "geometry": {"type": "LineString", "coordinates": [geo.to_lonlat(origin, p["x"], p["y"]) for p in pts]}},
            "points": len(pts), "length_m": round(length, 1),
            "total_time_s": round(float(data["total_time"]), 1) if isinstance(data.get("total_time"), (int, float)) else None}


# -- what an app session changed (Studio -> More -> Record an app edit) ----------------------------------------------
def settings_changes(entries: list, before: dict, names: dict) -> list:
    """Each settings save the app made, as the fields it changed against the area's previous settings (the read at the
    start of the recording, then each earlier save). before: {area id: settings read}; names: {area id: name}."""
    last = {int(k): flat(v) for k, v in (before or {}).items() if isinstance(v, dict)}
    skip = set(PARAMS_DROP) | {"id", "mapping_module_type"}
    out = []
    for e in entries:
        if e.get("key") != f"app/{SAVE}" or not isinstance(e.get("data"), dict):
            continue
        rec = e["data"]
        aid = rec.get("id")
        new = flat(rec)
        old = last.get(aid)
        changed = [] if old is None else [
            {"key": k, "label": LABELS.get(k), "from": old.get(k), "to": new.get(k)}
            for k in sorted(new) if k not in skip and not _same(old.get(k), new.get(k))]
        out.append({"ts": e.get("ts"), "id": aid, "name": names.get(aid), "changes": changed, "baseline": old is not None})
        last[aid] = new
    return out
