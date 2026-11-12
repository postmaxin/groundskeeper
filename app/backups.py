"""The robot's own map backups: save one now, delete, restore (2026-10-07).

Commands, all captured from the Yarbo app on this robot (docs/dev/map-writes.md):
* list     get_all_map_backup {}                                   -> [{id, name, timestamp, is_auto_backup}]
* save     save_map_backup_and_get_all_map_backup_nameandid {}     -> the new list ("Manually Saved N")
* delete   del_map_backup {"ids": [id]}                            -> "Map backup has been successfully deleted."
* restore  map_recovery {"id": id}                                 -> "The map has been successfully restored."
* rename   rename_map_backup {"id": id, "name": "..."}             (captured 2026-10-10, app-edit-1791688836)
Robot backups are made only when asked
(the robot keeps 10); the panel's own copies are local and unlimited.

A restore replaces the whole map AND deletes every plan and resets every area's mow settings (seen 2026-10-07; area
ids come back as they were in the backup). So restore():
1. refuses unless it can read everything the restore wipes: the plan list and every area's settings (by id);
2. keeps the map, plans and settings locally ("prerestore" version);
3. sends map_recovery; from here on nothing raises - each phase records what it did or why it could not;
4. polls the map until it matches the backup (the robot may apply it a moment later);
5. only when it matches: puts each area's settings back where the backup has the SAME id (and the record on the
   robot is the backup's shape), using the app's sequence: save_mower_area_params, then save_clean_area unchanged;
6. makes the wiped plans again (save_plan, same id, name and areas, so schedule windows still point at them) when
   the map is confirmed and the backup has all of a plan's areas, reads the plans back, lists any it could not make,
   uploads the cloud copy and stores the result (map_last_restore).

Robot backups carry a second copy of every pathway and sidewalk: same name and points, fresh ids, other start/end ids
(seen in backups 4322 from 10-01 and 4354 from 10-07, both taken of a 6-pathway map; 14 pathways in each). The live
map after a restore has one of each (an app restore of 4331 at 09:31), so dedupe() drops the copies before any compare.
"""
import asyncio
import copy
import time

from . import mapstudio
from .codec import decode_any
from .mapwrite import (AREA_KEYS, AREA_SAVE, BACKUP_CMD, MANUAL_BACKUPS, PLAN_KEYS, PLAN_SAVE, VERIFY_M, WriteRefused,
                       _plan_differs, _same_shapes, link_problems)

LIST, DELETE, RESTORE, PARAMS_SAVE = "get_all_map_backup", "del_map_backup", "map_recovery", "save_mower_area_params"
RENAME = "rename_map_backup"
NAME_MAX = 40
COMMANDS = frozenset({LIST, DELETE, RESTORE, PARAMS_SAVE, RENAME})
# read_mower_area_params keys the app does not send back in save_mower_area_params
PARAMS_DROP = ("clean_times_dis", "current_blade_height", "move2area_offset_count")
SETTINGS_CHECK = ("edge_circles", "first_clean_params", "double_clean_params", "contract_dis", "ngz_expand_dis", "gap")
POLL_S, POLL_TRIES = 5, 12            # wait up to a minute for the restored map to show


def manual_count(listing: list) -> int:
    return sum(1 for b in listing if isinstance(b, dict) and not b.get("is_auto_backup"))


def params_payload(read: dict, area_id: int) -> dict:
    out = {k: copy.deepcopy(v) for k, v in read.items() if k not in PARAMS_DROP}
    out["id"] = area_id
    out["mapping_module_type"] = None            # as the app sends it
    return out


def _points(f) -> tuple:
    out = []
    for q in f.get("range") or []:
        if isinstance(q, dict):
            x, y = q.get("x"), q.get("y")
        elif isinstance(q, (list, tuple)) and len(q) >= 2:
            x, y = q[0], q[1]
        else:
            continue
        out.append((round(float(x or 0), 2), round(float(y or 0), 2)))
    return tuple(out)


def dedupe(raw: dict) -> dict:
    """The backup without its repeated copies: per kind, a feature with the same name and points (either direction)
    as an earlier one is dropped."""
    out = dict(raw)
    for kind in mapstudio.POLYGONS + mapstudio.LINES:
        if not isinstance(raw.get(kind), list):
            continue
        seen, keep = set(), []
        for f in raw[kind]:
            if isinstance(f, dict):
                pts = _points(f)
                key = (f.get("name") or "", min(pts, pts[::-1]))
                if key in seen:
                    continue
                seen.add(key)
            keep.append(f)
        out[kind] = keep
    return out


def preview(current: dict, backup: dict, plans: list) -> dict:
    """What a restore would do, for the confirmation: areas kept / replaced / removed / added, and the plans."""
    backup = dedupe(backup)
    cur = {a.get("id"): a for a in current.get("areas") or []}
    bak = {a.get("id"): a for a in backup.get("areas") or []}
    return {
        "areas_kept": [{"id": i, "name": bak[i].get("name"), "was": cur[i].get("name")} for i in bak if i in cur],
        "areas_removed": [{"id": i, "name": cur[i].get("name")} for i in cur if i not in bak],
        "areas_added": [{"id": i, "name": bak[i].get("name")} for i in bak if i not in cur],
        "plans_deleted": [{"name": p.get("name"), "area_ids": p.get("areaIds"),
                           "remade": all(a in bak for a in p.get("areaIds") or [])} for p in plans],
        "counts": {k: (len(current.get(k) or []), len(backup.get(k) or [])) for k in ("areas", "nogozones", "pathways")},
    }


class Backups:
    """I/O comes in as functions (tests stand in for the robot): writer = the MapWriter (cloud sends, its lock);
    read(cmd, payload) -> reply dict through the link (reads only); read_map() -> fresh raw map;
    read_areas() -> {id: full area record} (Recorder.read_area_records)."""

    def __init__(self, store, writer, read, read_map, read_areas):
        self.store, self.writer, self.read, self.read_map, self.read_areas = store, writer, read, read_map, read_areas

    async def listing(self) -> list:
        r = await self.writer._send(LIST, {})
        data = decode_any(r.get("data")) if r.get("ok") else None
        if not isinstance(data, list):
            raise WriteRefused("could not read the robot's backup list")
        return [b for b in data if isinstance(b, dict)]

    async def save(self) -> dict:
        async with self.writer.lock:
            before = await self.listing()
            if manual_count(before) >= MANUAL_BACKUPS:
                raise WriteRefused(f"all {MANUAL_BACKUPS} backup slots are used; delete one first")
            r = await self.writer._send(BACKUP_CMD, {})
            made = decode_any(r.get("data"))
            made = [b for b in made if isinstance(b, dict)] if isinstance(made, list) else []
            new = [b for b in made if b.get("id") not in {x.get("id") for x in before}]
            if not r["ok"] or not new:
                raise WriteRefused(f"the robot did not make a backup ({r.get('error') or r.get('msg') or 'no new entry'})")
            newest = max(new, key=lambda b: b.get("timestamp") or 0)
            self.store.event("backup_saved", f"robot backup “{newest.get('name')}” (id {newest.get('id')})")
            return {"ok": True, "backup": newest, "list": made}

    async def delete(self, bid: int) -> dict:
        async with self.writer.lock:
            before = await self.listing()
            b = next((x for x in before if x.get("id") == bid), None)
            if b is None:
                raise WriteRefused(f"the robot has no backup {bid}")
            r = await self.writer._send(DELETE, {"ids": [bid]})
            try:
                after = await self.listing()
                gone = not any(x.get("id") == bid for x in after)
            except WriteRefused:
                after, gone = None, None
            what = {True: "deleted", False: f"still listed ({r.get('msg') or r.get('error')})",
                    None: "sent, but the list could not be read back"}[gone]
            self.store.event("backup_deleted" if gone else "backup_delete_unconfirmed",
                             f"robot backup “{b.get('name')}” (id {bid}): {what}")
            return {"ok": bool(gone), "deleted": b, "list": after, "error": None if gone else what}

    async def rename(self, bid: int, name) -> dict:
        name = str(name or "").strip()
        if not name:
            raise ValueError("give the backup a name")
        if len(name) > NAME_MAX:
            raise ValueError(f"keep the name to {NAME_MAX} characters")
        async with self.writer.lock:
            before = await self.listing()
            b = next((x for x in before if x.get("id") == bid), None)
            if b is None:
                raise WriteRefused(f"the robot has no backup {bid}")
            if b.get("name") == name:
                return {"ok": True, "unchanged": True, "backup": b, "list": before}
            r = await self.writer._send(RENAME, {"id": bid, "name": name})
            try:
                after = await self.listing()
                now = next((x for x in after if x.get("id") == bid), None)
                done = bool(now) and now.get("name") == name
            except WriteRefused:
                after, done = None, None
            what = {True: "renamed", False: f"the robot kept another name ({r.get('msg') or r.get('error')})",
                    None: "sent, but the list could not be read back"}[done]
            self.store.event("backup_renamed" if done else "backup_rename_unconfirmed",
                             f"robot backup {bid} “{b.get('name')}” → “{name}”: {what}")
            return {"ok": bool(done), "list": after, "error": None if done else what}

    async def _plans(self) -> list:
        pr = await self.read("read_all_plan", {})
        plans = (pr.get("data") or {}).get("data") if isinstance(pr.get("data"), dict) else None
        if pr.get("state") != 0 or not isinstance(plans, list):
            raise ValueError("no plan list in the reply")
        return [p for p in plans if isinstance(p, dict)]

    async def _remake_plans(self, plans: list, area_ids: set, confirmed: bool, keep: bool, res: dict) -> None:
        """Step 6: save the wiped plans again as they were; res gets plans_made and plans_lost (with why)."""
        try:
            now = await self._plans()
        except Exception as exc:
            res["problems"].append(f"reading the plans back: {exc}")
            res["plans_lost"] = [{"name": p.get("name"), "area_ids": p.get("areaIds"),
                                  "why": "the plans could not be read back"} for p in plans]
            return
        kept = {p.get("id") for p in now}
        lost, sent = [], []
        for p in (p for p in plans if p.get("id") not in kept):
            why = ("not asked to" if not keep else "the restored map could not be confirmed first" if not confirmed
                   else "the backup does not have all of its areas"
                   if any(a not in area_ids for a in p.get("areaIds") or []) else None)
            if why is None:
                want = {**{k: p.get(k) for k in PLAN_KEYS}, "enable_self_order": p.get("enable_self_order", True)}
                s = await self.writer._send(PLAN_SAVE, want)
                if s["sent"]:
                    sent.append(want)
                else:
                    why = f"not sent ({s.get('error')})"
            if why:
                lost.append({"name": p.get("name"), "area_ids": p.get("areaIds"), "why": why})
        made = []
        if sent:
            try:
                after = {p.get("id"): p for p in await self._plans()}
            except Exception as exc:
                after = None
                res["problems"].append(f"reading the plans back after making them again: {exc}")
            for want in sent:
                if after is not None and not _plan_differs(after.get(want["id"]), want):
                    made.append({"id": want["id"], "name": want["name"]})
                else:
                    lost.append({"name": want["name"], "area_ids": want["areaIds"],
                                 "why": "saved, but not listed by the robot" if after is not None else "saved, not confirmed"})
        res["plans_made"], res["plans_lost"] = made, lost

    async def restore(self, bid: int, backup_map: dict, gps_ref, keep_settings: bool = True,
                      keep_plans: bool = True) -> dict:
        backup_map = dedupe(backup_map)
        async with self.writer.lock:
            listing = await self.listing()
            target = next((x for x in listing if x.get("id") == bid), None)
            if target is None:
                raise WriteRefused(f"the robot has no backup {bid}")
            # 1. everything the restore wipes must be readable, or nothing is sent
            try:
                pr = await self.read("read_all_plan", {})
                current = await self.read_map()
            except Exception as exc:
                raise WriteRefused(f"could not read the plans and the map first ({exc}); nothing was sent")
            plans = (pr.get("data") or {}).get("data") if isinstance(pr.get("data"), dict) else None
            if pr.get("state") != 0 or not isinstance(plans, list):
                raise WriteRefused("could not read the plan list first; nothing was sent")
            plans = [p for p in plans if isinstance(p, dict)]
            settings = {}
            for a in current.get("areas") or []:
                try:
                    rp = await self.read("read_mower_area_params", {"id": a["id"]})
                except Exception as exc:
                    raise WriteRefused(f"could not read the mow settings of area “{a.get('name')}” first ({exc}); "
                                       "nothing was sent")
                d = rp.get("data")
                # id 0: never saved, the robot's defaults (a restore resets to them anyway)
                if rp.get("state") != 0 or not isinstance(d, dict) or d.get("id") not in (a["id"], 0):
                    raise WriteRefused(f"could not read the mow settings of area “{a.get('name')}” first; nothing was sent")
                settings[a["id"]] = d
            # 2. the local copy
            self.store.save_version("prerestore", {"at": time.time(), "map": current, "plans": plans,
                                                   "area_settings": {str(k): v for k, v in settings.items()},
                                                   "restored_from": target})
            # 3. the restore
            r = await self.writer._send(RESTORE, {"id": bid})
            if not r["ok"]:
                why = r.get("error") or r.get("msg") or f"state {r.get('state')}"
                self.store.event("backup_restore_failed", f"“{target.get('name')}” ({bid}): {why}")
                return self._keep({"ok": False, "error": why, "restored_from": target})
            self.store.event("backup_restored", f"map restored from “{target.get('name')}” (id {bid}); checking it")
            res = {"ok": True, "restored_from": target, "problems": [], "settings": [], "settings_not_applied": [],
                   "plans_made": [], "plans_lost": [{"name": p.get("name"), "area_ids": p.get("areaIds")} for p in plans]}
            # 4. read back until it matches the backup
            after, matches = None, None
            for _ in range(POLL_TRIES):
                try:
                    after = await self.read_map()
                    matches = _same_shapes(backup_map, after, gps_ref)
                except Exception as exc:
                    res["problems"].append(f"reading the map back: {exc}")
                if matches:
                    break
                await asyncio.sleep(POLL_S)
            res["map_matches_backup"] = matches
            if matches is not True:
                res["problems"].append("the map read back does not match the backup" if after is not None
                                       else "the map could not be read back")
            # 5. settings, only onto the backup's areas with the same id, once the map is confirmed
            bak = {a.get("id"): a for a in backup_map.get("areas") or []}
            for a in current.get("areas") or []:
                if a["id"] in settings and a["id"] not in bak:
                    res["settings_not_applied"].append({"id": a["id"], "name": a.get("name"),
                                                        "why": "the backup has no such area"})
            if keep_settings and matches:
                try:
                    records = await self.read_areas()
                except Exception as exc:
                    records = {}
                    res["problems"].append(f"reading the area records: {exc}")
                for aid, s in settings.items():
                    if aid not in bak:
                        continue
                    rec, want = records.get(aid), bak[aid]
                    name = (rec or want).get("name")
                    if s.get("id") == 0:                 # it was on the defaults, which is what a restore leaves
                        res["settings"].append({"id": aid, "name": name, "ok": True, "defaults": True})
                        continue
                    try:
                        if rec is None or mapstudio.max_move(mapstudio.local(rec, gps_ref),
                                                              mapstudio.local(want, gps_ref), True) > VERIFY_M:
                            res["settings_not_applied"].append({"id": aid, "name": name,
                                                                "why": "the robot's record is not the backup's shape yet"})
                            continue
                        p = await self.writer._send(PARAMS_SAVE, params_payload(s, aid))
                        a = await self.writer._send(AREA_SAVE, {k: copy.deepcopy(rec[k]) for k in AREA_KEYS if k in rec})
                        back = await self.read("read_mower_area_params", {"id": aid})
                        got = back.get("data") if isinstance(back.get("data"), dict) else {}
                        same = got.get("id") == aid and all(got.get(k) == s.get(k) for k in SETTINGS_CHECK)
                    except Exception as exc:
                        p, a, same = {"ok": False}, {"ok": False}, False
                        res["problems"].append(f"settings of “{name}”: {exc}")
                    res["settings"].append({"id": aid, "name": name, "ok": bool(p["ok"] and a["ok"] and same)})
            elif keep_settings:
                for aid in settings:
                    if aid in bak:
                        res["settings_not_applied"].append({"id": aid, "name": bak[aid].get("name"),
                                                            "why": "the restored map could not be confirmed first"})
            # 6. the plans, what is left to do by hand, and the cloud copy
            final = None
            try:
                final = await self.read_map()
                res["problems"] += link_problems(after or final, final, backup_map, gps_ref) if res["settings"] else []
            except Exception:
                pass
            area_ids = {a.get("id") for a in (final or after or backup_map).get("areas") or []}
            await self._remake_plans(plans, area_ids, matches is True, keep_plans, res)
            try:
                up = await self.writer._send("upload_cloud_map_backup", {"email": self.writer.account() or ""})
                res["cloud_backup"] = bool(up.get("ok"))
            except Exception:
                res["cloud_backup"] = False
            lost = ", ".join(p["name"] for p in res["plans_lost"]) or "none"
            made = ", ".join(p["name"] for p in res["plans_made"]) or "none"
            self.store.event("backup_restore_checked" if matches else "backup_restore_unconfirmed",
                             f"“{target.get('name')}” ({bid}): map {'matches' if matches else 'NOT confirmed'}; settings put "
                             f"back on {sum(1 for x in res['settings'] if x['ok'])} area(s); plans made again: {made}; "
                             f"plans to make by hand: {lost}")
            return self._keep(res)

    def _keep(self, res: dict) -> dict:
        res["at"] = time.time()
        self.store.set_setting("map_last_restore", res)
        return res
