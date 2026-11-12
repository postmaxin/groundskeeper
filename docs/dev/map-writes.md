# Map, plan and schedule writes

The write commands the panel uses, and the ones it knows but does not use, with their payload shapes. Yarbo's SDK
has no map, plan or schedule writes at all. Everything here was **captured from the official app** (October 2026,
firmware 3.x) with the panel's "Record an app edit", one change per save, or is cited from public community projects
where noted. Locations are omitted from every payload below.

All of these went over the **cloud** broker. Every reply was `data_feedback` with `state: 0` unless noted.

Frame for every `range`: metres from the feature's own `ref` (`{latitude, longitude}`), **x west, y north**, each
point `{x, y, phi}` where `phi` is the direction of the segment.

## Rules that follow from the robot's behaviour

- **No id = create; the robot assigns the id** (except plans and schedules, where the app picks it). A save with an
  existing id changes that record in place.
- **Ids are referenced elsewhere.** Plans hold `areaIds`; pathways hold `start_id`/`end_id`; area mow settings are
  keyed by area id. Deleting and re-adding an area breaks all three, so areas are only ever edited in place.
- **Ids are global across kinds and the robot reuses freed ids.** A new area was given the id of a dead end deleted
  minutes earlier. Never key history on id alone.
- **The robot re-links pathways on every area save.** `start_id`/`end_id` can change (to 0 or −1) after an area is
  added, deleted or reshaped; check them on read-back.
- **After a map save or delete the app sends `upload_cloud_map_backup {"email": <account>}`**, which uploads a copy
  to the cloud only.
- Every write needs the robot **docked with no plan running**. None needed a `get_controller` handshake.

## Areas

### Save in place: `save_clean_area`

Payload = **the app's keys only**:

```
{id, name, algorithm_type, labels, leaf_piles, push_snow_dir, range, ref, slope_degrees, snowPiles, trimming_edges}
```

Do **not** send back the robot-computed keys (`area`, `mapping_module_type`, `mult_grid`, `humps`,
`area_max_low_cfd_dis`, `path_max_low_cfd_dis`, `first_finish`). The reply carries the stored record (18 keys).

- A rename in the app is exactly this, with the full range included: confirmation of the in-place edit.
- A reshape is the same command with a new `range`, converted through lat/lon onto the live area's `ref`.
  Counter-clockwise order is recommended (the edge-lap direction follows vertex order).
- Traps (from community projects): a bare record whose id is not an area's creates a NEW area;
  `{areas: [...]}` wrapping is acknowledged and ignored.
- `labels` are the app's head labels in app order (e.g. `[3]` = Lawn Mower, `[1]` = Snow Blower).

**Safe procedure, as the panel implements it:** docked, no plan; read `get_map`, `read_all_clean_area` and the plans
and keep them locally; change only `range` and/or `name`; send one `save_clean_area` with the same id; verify the
reply id, the range within 5 cm, no new area, and pathway links unchanged; undo = send the original record back.

### Add: `save_clean_area` without an id

```
{algorithm_type: 0, labels: [], leaf_piles: [], name: "Area 1", push_snow_dir: 9999.0, range: [...],
 ref: {...}, slope_degrees: 9999.0, snowPiles: [], trimming_edges: []}
```

The app's full flow when an area is recorded by driving:

1. `set_combined_odom_path_state` (start/stop; `{state, type}`, states 0 → 3 → 1; type 0 = area, 1 = pathway),
   `lidar_extend`, the joystick (`cmd_vel`), `set_auto_mapping_state`. The robot streams the driven trail on
   `device/combined_odom_path`.
2. `save_clean_area` without id → the robot assigns one.
3. `save_mower_area_params {"id": <new>, ...}` with the defaults for a new area (below).
4. `lidar_extend {"state": 0}`, `save_clean_area` again **with** the new id and the same range,
   `set_auto_mapping_state {"state": 0, "dir": 1}`.

Steps 1 and 4's driving commands are how the app records an outline; they are not needed when the outline is drawn.
The panel does not offer adding areas, because a new area changes plans and the robot's route.

One recorded app session sent two saves a second apart: the driven section saved **over an existing area's id**, then
the whole edited yard saved as a new area. The result broke the plan that used the old area. Whether that was the
app's intended flow or an app bug is not known; the panel never replicates it.

### Delete: `del_clean_area {"id": N}`

Drops the plans that use the area and its mow settings. Never use it as part of an edit. Not offered by the panel.

### Area mow settings: `save_mower_area_params`

The full params record minus `clean_times_dis`, `current_blade_height`, `move2area_offset_count` (and
`mapping_module_type: null`). Reply `data: ""`. **Each settings save is followed by `save_clean_area` with the
unchanged area record**, then `upload_cloud_map_backup` and `read_all_clean_area`.

Fields: `id, gap, route_order, clean_times, buffer_dis, avoidance_level, auto_step_angle, sensor_switch,
m1pro_sensor_switch, mower_target_height_count, current_index, edge_circles, contract_dis, ngz_expand_dis,
mower_ngz_edge_sw, edge_area_first, en_trimmer, edge_direction_mode, edge_bypass_sw, move2area_offset_enable,
move2area_max_offset, move2area_step_offset, first_clean_params {blade_speed, blade_height, turn_type,
land_push_pod_place, route_angle, route_dis, plan_speed}, double_clean_params {same}, side_enable,
enable_clean_times_dis, parallel_offset_mode, parallel_angle_max, parallel_angle_min, enable_explore`.

Defaults the app sent for a new area: `gap 0.40, route_order 1, clean_times 1, buffer_dis 0.0, avoidance_level 1,
auto_step_angle 10, sensor_switch 8, m1pro_sensor_switch 136, mower_target_height_count 5, edge_circles 2,
contract_dis 0.10, ngz_expand_dis 0.30, mower_ngz_edge_sw false, edge_area_first true, en_trimmer true,
edge_direction_mode 0, edge_bypass_sw true, move2area_offset_enable true, move2area_max_offset 2.0,
move2area_step_offset 0.10`, first pass `{blade_speed 80, blade_height 0, turn_type 1, route_angle 0,
plan_speed 0.7}`, second pass `{blade_height 0, turn_type 1, route_angle 90, plan_speed 0.5}`, `side_enable true`.

### App setting ↔ robot field

Matched by changing one setting in the app per save.

| App (section) | Robot field | Status |
|---|---|---|
| Moving Speed: Slowest/Slow/Medium/Fast/Fastest | `first_clean_params.plan_speed` (m/s); Fastest = 0.7, default 0.5 | 0.7 matched; levels 0.3–0.7 inferred |
| Route Angle | `first_clean_params.route_angle` (degrees) | matched |
| Turning Mode: Smart Turn / U-Turn / Zero Turn | `turn_type`: 1 = Smart Turn | others not yet known |
| Obstacle Avoidance Mode | `avoidance_level`: 1 = Moderate bypass | others not yet known |
| Perimeter Direction: Follow Original Path / CCW / CW | `edge_direction_mode`: 0 = Follow Original Path | others not yet known |
| Overlap 0/4/8 in | `gap` = 0.5 m − overlap (8 in → 0.30; default 0.40) | matched (0.5 inferred) |
| Transit Path Offset, Range 0–78.7 in, Step 3.9–19.7 in | `move2area_offset_enable`, `move2area_max_offset` (m), `move2area_step_offset` (m) | matched |
| No-Go Zone Edges | `mower_ngz_edge_sw` | matched |
| Perimeter Mowing Laps 2/3/4 | `edge_circles` | matched |
| Perimeter Buffer 0–16 in | `contract_dis` (m) | matched |
| NGZ Buffer 0–16 in | `ngz_expand_dis` (m) | matched |
| Labels | area record `labels` | matched |
| Mowing Order (Boundary first) | `edge_area_first` | probable |
| Perimeter First / NGZ Edge First | `route_order` | probable |
| Extend Area Boundary 0–3.3 ft | `buffer_dis` | probable |
| Mowing Height (in) | `first_clean_params.blade_height` = the deck lift position, not mm | partial (below) |
| Module Gap: Loose/Medium/Tight | `land_push_pod_place`? | Not yet known |
| Route Pattern: Parallel/Spiral/Checkboard/Diamond/Adaptive | `parallel_offset_mode`? `auto_step_angle`? | Not yet known |
| Boundary Obstacle Avoidance | `edge_bypass_sw`? `m1pro_sensor_switch` bits? | Not yet known |
| Target Height (max → mowing height over N sessions) | `mower_target_height_count`, `enable_clean_times_dis`? | Not yet known |

**Mowing height:** `blade_height` matches the deck's `mower_head_info01.lift_motor_place` while mowing (50 when
parked or in transit). Known pairs: 25 = 3.3 in, and 0 = blades up / 4.0 in on pathways. Whether 0 is a point on a
linear scale or a special "up" position is not yet known, so the panel keeps mowing height read-only. Yarbo's
published cutting range is 0.8–4.0 in (20–102 mm); the SDK's `mower_target_cmd` takes 20–100 mm. Yarbo advises not
cutting below 2.7 in.

## No-go zones

| Command | Payload |
|---|---|
| `save_nogozone` | `{range, ref, name, type: 0, enable: true, trimming_edges: []}`. No id: the robot assigns it |
| `del_nogozone` | `{"id": N}` |

Changing an existing zone's shape in place is not captured; the panel sends add-new then delete-old (nothing refers
to a no-go zone's id).

## Pathways

| Command | Payload |
|---|---|
| `save_pathway` | `{range, ref, name, connectids: [], snowPiles: [], leaf_piles: [], trimming_edges: []}`. No id, no `start_id`/`end_id`: the robot assigns and computes them |
| `save_mower_pathway_params` | Sent right after `save_pathway`, for the new id (below) |
| `del_pathway` | `{"id": N}` |

`save_mower_pathway_params` fields: `id, blade_speed (80), blade_height (0), turn_type (0), land_push_pod_place (50),
route_angle, route_dis, offset_enable, en_blade, plan_speed (0.5), pathway_obs_perception_sw, sensor_switch,
en_trimmer, offset_range ([-0.5, 0.5]), offset_step (0.1)`.

- `en_blade` is the blades switch; `plan_speed` the speed (m/s).
- Obstacle sensing off/on pairs with `sensor_switch` 10/11.
- `offset_enable`, `offset_range` and `offset_step` are the transit path offset in metres. The app's Range Offset
  slider runs ±1.0 m (−39.4 to 39.4 in) and Step Offset 0.1–0.5 m (3.9–19.7 in). A read-only `current_offset`
  moves to the start of the range when the offset is switched on.
- `blade_height` and `blade_speed` are not yet matched to the app (one save with blades on carried `blade_height` 3).

## Dead ends

| Command | Payload |
|---|---|
| `save_deadend` | `{connectids: [], name, range, ref, trimming_edges: []}`: the same shape as `save_pathway` |
| `save_deadend_params` | The pathway params without the offset fields: `{blade_speed, blade_height, turn_type, land_push_pod_place, route_angle, route_dis, en_blade, plan_speed, sensor_switch, en_trimmer, id}` |
| `read_deadend_params` | `{"id": N}`; an answer with id 0 = never configured (robot defaults) |
| `del_deadend` | `{"id": N}` |

Reshape in place is not captured; the panel sends add-new then delete-old and carries the settings over.

## No-vision zones

| Command | Payload |
|---|---|
| `read_novisionzone` | `{"id": N}` → `{area_id: -1, by_pass_level, enable, id, name, range, ref}`. The app reads before and after each save |
| `save_novisionzone` | `{range, ref, name, id, enable, by_pass_level}`: an existing id changes the zone in place; `id: -1` creates one. Reply = the saved zone |
| `del_novisionzone` | `{"id": N}` |

`by_pass_level` is the app's obstacle bypass and follows the area's obstacle avoidance modes: 1 = Moderate bypass.
Values 0 and 3 have been seen; their app names are not yet known. Inside a no-vision zone the robot ignores its
cameras, so the panel asks for an explicit confirmation before adding one.

## Plans

Plans are not part of the map: no map change, no `save_clean_area` or cloud backup follows a plan save.

| Command | Payload | Reply |
|---|---|---|
| `save_plan` | exactly `{id, areaIds, name, enable_self_order}` | `{"data": "", "msg": "The plan was saved successfully.", "state": 0}` |
| `del_plan` | `{"id": N}` | `"Plan deleted successfully."` |
| `read_all_plan` | `{}` | `{"data": {"data": [{areaIds, enable_self_order, id, name}, ...]}}`; empty robot `{"data": {"data": []}}` |

- **The app picks the id**: the lowest free integer in the captures (a deleted id was reused for the next new plan).
- Create, rename and change of areas are all `save_plan`; the same id replaces the plan in place. `areaIds` keeps the
  app's order (mowing order).
- `enable_self_order` was true on every save (presumably the app's "optimise order" toggle).
- The app re-reads after every save (not after a delete).
- Not yet known: plans with more fields (per-plan settings), and what happens when `areaIds` names a deleted area.

## Schedules

The robot keeps schedules only if the app saves them. The panel runs its own windows instead (see `design.md` §8.1),
because a robot-side schedule starts without the panel's gates.

| Command | Payload | Reply |
|---|---|---|
| `save_schedule` | 11 keys: `id, plan_id, start_time, end_time, enable, schedule_type, enable_last_progress, last_progress, name, week_day, return_method` | The stored record plus `completed_this_window false, interval_time 0, is_weather_schedule 0, times 0, timezone ""` |
| `enable_schedule` | `{id, enable}` | The full record |
| `del_schedule` | `{"id"}` | `"Schedule deleted successfully."` |
| `read_schedules` | `{}` | `{"data": [record, ...]}` (a list directly under `data`, unlike `read_all_plan`) |

- **Times are UTC, `"HH:MM:SS"`.** `timezone` comes back empty, so a client converts local ↔ UTC itself (and
  watches daylight-saving changes).
- **`week_day` is a 7-bit mask: Sun 1, Mon 2, Tue 4, Wed 8, Thu 16, Fri 32, Sat 64.** Mon/Wed/Fri = 42; every day =
  127.
- The app picks the id (lowest free) and re-saves with the same id to edit; the default name is "Schedule 1".
- Constant in the captures, meaning not yet known: `schedule_type 3`, `return_method 1`, `enable_last_progress
  false`, `last_progress 0` (presumably "continue from last progress"). Only weekly windows with one plan were seen;
  one-off, interval and weather-aware schedules (`is_weather_schedule`, `interval_time`, `times`) were not exercised.

## Map backups

| Command | Payload | Notes |
|---|---|---|
| `get_all_map_backup` | `{}` | List of `{id, name, timestamp, is_auto_backup}` |
| `get_map_buckup_from_id` | `{"id": N}` | One backup's map (sic) |
| `save_map_backup_and_get_all_map_backup_nameandid` | `{}` | A manual backup ("Manually Saved N"); reply = saved + the new list. 10 manual + 10 auto slots |
| `del_map_backup` | `{"ids": [N]}` | "Map backup has been successfully deleted." |
| `rename_map_backup` | `{"id": N, "name": "..."}` | |
| `map_recovery` | `{"id": N}` | "The map has been successfully restored." |
| `upload_cloud_map_backup` | `{"email": <account>}` | Cloud copy only |

**`map_recovery` replaces the whole map, deletes every plan and resets every area's mow settings**; area ids come
back as they were in the backup. A safe restore therefore reads and keeps the plans and all area settings first,
waits until `get_map` matches the backup, then puts settings back on same-id areas (`save_mower_area_params`, then
`save_clean_area` unchanged) and recreates the plans with the same ids.

## Route preview

`preview_snowbot_area_path {range, id, push_snow_dir, snowPiles, leaf_piles, ref, algorithm_type, slope_degrees}` →
`{path: [{x, y, phi}], total_time}` (~48 KB for a large area). Computes the mowing route with the area's saved
settings; it does not move the robot. The panel sends it through the cloud (it also worked on the local broker
before firmware V3.14.4x).

## Not yet known

- Sidewalks and electric fences: no write command captured.
- Reshaping a no-go zone, pathway or dead end in place.
- How area m² is recomputed and how a running plan resumes after an area save.
- Validation rules on the robot side (point limits beyond the 252 seen, self-intersection).

## Sources

- Captures of the official app, October 2026 (one change per save).
- Community projects: `yarbo-local/yarbo-local` (`protocol/commands.yaml`, `payloads.py`) and
  `martyndix/yarbo-control-panel` (`src/YarboMapBackup.php`, CHANGELOG 2.0.45–2.0.55).
- Yarbo forum threads "Settings to Improve Mowing Efficiency" (forum.yarbo.com/t/1406) and "Mowing height and speed"
  (forum.yarbo.com/t/2704); Yarbo product pages; the SDK's `devices/yarbo_Y.json`.
