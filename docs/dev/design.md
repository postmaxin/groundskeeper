# Groundskeeper design

How the panel works and why. Section numbers are stable: the code cites them (for example `app/control.py` cites
§10.3, `app/cloud.py` §5.3, `app/mapwrite.py` §7.4).

Companion documents in this folder:

- [`protocol.md`](protocol.md): the robot's MQTT protocol, access rules and every known read command.
- [`map-writes.md`](map-writes.md): the map, area, plan and schedule write commands, as captured from the official app.
- [`robot-states.md`](robot-states.md): state codes, pause reasons, error codes and the thresholds the panel uses.
- [`ui-design.md`](ui-design.md): navigation, the colour tokens and the chart rules.
- [`imagery-catalog.md`](imagery-catalog.md): the aerial imagery source catalog.

Groundskeeper is not affiliated with Yarbo. It uses the commands Yarbo's own SDK documents, read commands the
community has documented, and write commands only as they were captured from the official app.

---

## 1. Goal

Everything the Yarbo app does for a household's own robot, plus what a power user wishes it did: self-hosted,
recorded, and guarded.

| Module | One line | Section |
|---|---|---|
| **Live** | Where the robot is, what it is doing, how healthy it is, the follow-cam and the controls, on one screen | §10, §11 |
| **Runs** | Every job recorded and replayable, any head: coverage, RTK/radio/battery/load/slope layers, problem areas, gaps | §6, §12 |
| **Map Studio** | Real imagery underneath; view, measure, edit, tidy, version, diff and restore areas, no-go zones and pathways | §7 |
| **Plans & Schedule** | Plans on the robot; one scheduler, gated on conditions, presence and quiet hours; mower and snow profiles | §8, §9 |
| **Conditions** | "Is the lawn dry?" from a weather station, Home Assistant sensors, the robot's rain sensor and the forecast | §9 |
| **Control** | The official run commands with interlocks, an audit log, and nothing else | §10 |
| **Yard safety & follow-cam** | Frigate yard cameras: a feed that follows the robot, and a "yard clear" gate on top of the robot's own | §11 |
| **Gap touch-ups** | Find patches a run missed (fixed-RTK only) | §12 |
| **Capture** | Record what the official app sends, so new commands are learned from evidence | §14 |
| **Health & Maintenance** | RTK/RTCM, HaLow, battery cells, motors, firmware, error dictionary, service timers | §15 |
| **Insights** | Aggregates across runs, seasons and years | §16 |
| **Integrations** | Home Assistant entities and alerts, webhooks, an MCP server for AI assistants, notifications | §17 |
| **Robot cameras** | Planned: a Cameras tile on Live, people only, off by default; see §13 | §13 |

### Non-goals

- **Manual driving** (`cmd_vel`). Never exposed, never sent. It is hard-blocked in the control module, refused in
  the capture console and not recorded.
- Multi-tenant hosting. **One install = one household.** All configuration lives in one place, with no hard-coded
  ids, hosts or serials.
- Firmware modification, or anything over the robot's SSH port. The panel leaves both alone.

---

## 2. What the robot exposes

| Capability | Local broker (`<robot-ip>:1883`, anonymous) | Cloud (Yarbo SDK, account session) |
|---|---|---|
| Connect | Yes | Yes (EMQX; the SDK picks the broker per robot) |
| Telemetry (`get_device_msg`, `DeviceMSG`, `heart_beat`) | Yes | Yes |
| Map / plans / GPS reference / low-confidence config reads | Yes | Yes |
| Listen to `snowbot/{SN}/#` (including the app's commands) | Yes | Own robot only |
| Where the phone app's traffic goes | — | **Cloud, even on the home Wi-Fi** |
| Official run commands | Not used (see §10.2) | Yes; the panel's control route |
| Map / plan / schedule writes | Not used | Yes, as captured from the app (`map-writes.md`) |
| Camera video | No | Planned, not on MQTT: the switch is, the stream is Agora's (§13) |

Details and ACLs are in [`protocol.md`](protocol.md).

A known failure mode of the cloud route: the SDK can swallow a failed token refresh and keep reconnecting with a
stale session, which the broker answers with "Not authorized". §5.3 designs around it.

---

## 3. Users and modes

- **Owner:** everything. Switching control or map writing on needs a typed confirmation (`ENABLE`).
- **Automation identities** (scheduler, Home Assistant, MCP): each has its own entries in the audit log and its own
  allow-list of commands. The MCP identity can never switch a safety setting on (§17).
- A read-only household viewer role is a possible later addition.

---

## 4. Architecture

```
                         ┌──────────────────────── panel host (Docker) ─────────────────────────┐
  Yarbo robot            │  core (Python 3.12, FastAPI + asyncio)                               │
  <robot-ip>  ◀──MQTT────┤   transport_local.py (read-only) · cloud.py (yarbo-data-sdk)         │
                         │   recorder.py · runs.py · mapstudio.py · mapwrite.py · scheduler.py  │
  Yarbo cloud ◀──TLS─────┤   control.py (+interlocks) · conditions.py · frigate.py · ha.py      │
   EMQX + data-api       │   mcp_server.py · imagery/ · elevation.py · store.py                 │
                         │  SQLite in DATA_DIR: telemetry · runs · map/plan versions · captures │
                         │                      · audit · settings                              │
                         │  web/ (plain JS, MapLibre GL, three.js for 3D), served by core       │
                         └──────────────────────────────────────────────────────────────────────┘
                                   ▲ LAN only by default; put an authenticating proxy in front for remote use
```

- **Python**, because Yarbo's SDK is Python: the panel reuses its auth, broker routing and command catalogue.
- **SQLite**, one file in `DATA_DIR`, used only from the asyncio thread. Enough for one robot.
- **Frontend:** plain JavaScript, MapLibre GL for the map, three.js for the 3D robot, Server-Sent Events for live
  updates.
- **Configuration in one place:** a `settings` table, editable in the UI, with environment variables as defaults.
- **Data at rest** lives in `DATA_DIR`. It holds the home GPS position, the robot's serial, the SIM ICCID and the
  cloud session, so it is never committed, never synced and never copied into `docs/`.

---

## 5. Transport layer

### 5.1 One protocol, two brokers
Both brokers speak `snowbot/{SN}/app/{cmd}` (zlib JSON) → `snowbot/{SN}/device/data_feedback`, plus `DeviceMSG` and
`heart_beat` pushes. **Replies are matched by topic name only**: the reply's `topic` field names the command it
answers, and the robot carries no request id. Two requests for the same command in flight cannot be told apart, so
reads of the same kind are serialised, and a reply that carries another feature's id (a late answer to an earlier
read that timed out) is refused rather than trusted. A dead end whose settings were never saved answers with id 0,
which says nothing about which dead end it is; such an answer is trusted only when no earlier read is outstanding.

### 5.2 Routing by operation class

| Class | Route | Why |
|---|---|---|
| Telemetry, reads | local | Fast, no account needed |
| Control (run commands) | local, the cloud as a fallback | Proved locally 2026-10-21 (§10.2); `app/link.py` |
| Writes (map/plan/schedule) | cloud only for now | Local writes not proved yet (`link.LOCAL_WRITES_PROVED`) |
| Capture | both at once | The app talks to the cloud; other local clients talk to the robot |

The local client's `request()` publishes only the `READ_ONLY_COMMANDS` allow-list (plus named compute-only requests
such as the route preview) and raises before the network is touched for anything else. Its `command()` takes only
the run commands and captured writes, never `cmd_vel`, and only `app/link.py` calls it. Reads also fall back to the
cloud: after two unanswered local reads in a row they go through the cloud for 5 minutes. Transport health shows as chips in the top bar and as a Home Assistant sensor.

### 5.3 Cloud session
The rotated refresh token is persisted after **every** refresh. A failed refresh means **needs sign-in**: stop
reconnecting, raise a UI banner and a notification, and never connect with an expired JWT. Reconnects back off
exponentially from 5 s to 5 min. Sign-in is interactive and the password is never stored; the session lives in
`DATA_DIR` with owner-only permissions.

### 5.4 Keep-awake
The phone app sends `set_working_state {"state":1}` about every 10 s while it is open. Local reads are answered
while the robot is docked and idle without it, so the panel does not send it.

### 5.5 Sharing the robot with the phone app
The phone app keeps the robot awake (`set_working_state`) and sends its commands through the cloud broker. What
happens when both sides command the robot at once is not fully known. The rule: **a start is refused while the phone
app has been seen in the last 60 s** (`phone_app_quiet_s`). It is seen two ways: its keep-awake on the cloud, and,
locally, the robot streaming `DeviceMSG` about once a second, which it does only while the app is connected (seen
2026-10-21; the panel's own reads come back on `data_feedback`, so every `DeviceMSG` counts). A stream proves the app is open; without the cloud its absence is not yet taken as "closed" (unknown),
and right after a route comes up the answer is unknown for the quiet window. Unknown blocks the starts nobody is
watching (scheduler, assistant, guard resume); a start from the panel shows it. Observed so far: a pause from the app
shows as paused at once, and a panel resume after an app pause is confirmed within a second.

---

## 6. Recorder, runs and analytics

### 6.1 Head-agnostic
The recorder records whatever head is attached. Mow, snow and leaf runs share the same tables, with the head type on
every run.

### 6.2 Sampling
Idle: one `get_device_msg` per poll interval. Active run: about 1 Hz. Map, plans and low-confidence config: on
start, periodically, and after any write; a new version is stored only when the content hash changes.

### 6.3 Tables and tolerant ingestion
Telemetry rows (position, RTK status/satellites/heading DOP, odometry, wheel speed/current, pitch/roll, battery,
blade motors, HaLow, RTCM age, state flags, run id), raw snapshots, runs, run events, map and plan versions,
captures, the command audit, firmware seen, maintenance, settings.

The telemetry columns are a projection of a payload that firmware updates will change. A missing or renamed field
becomes NULL and a one-time warning, never a failed insert.

### 6.4 Run detection
Runs are plan sessions: the run follows the robot's own plan state (`on_going_planning`, `planning_paused`,
`on_going_recharging`; codes in [`robot-states.md`](robot-states.md)), so a charging break mid-plan stays one run.
A plan that stops showing for under 30 s while the robot was held mid-plan keeps the run open. Two runs can be
joined by hand when one mow was split.

**The robot's `StateMSG.stuck` flag is unreliable**: it has stayed 0 through a stuck spell of over ten minutes.
The panel therefore runs its own stall rule: while the robot is mowing, between areas or heading home, staying
within 1 m for 3 minutes raises a stuck alert (`STALL_ALERT_S`, `STALL_RADIUS_M` in `app/recorder.py`). "No data
from Yarbo" fires after 2 minutes without data during a run.

Samples are stamped on arrival. After a link gap the robot can deliver a backlog in a burst, which looks like
position jumps; distance, speed and slip calculations skip those burst samples, and run totals are recomputed from
the stored rows rather than kept in memory.

### 6.5 Analytics
- **Per run:** replay with a scrubber, coverage (fixed-RTK track buffered by deck width, §12), area and time per
  zone, end reason, events on the timeline, energy used (battery % per 100 m²), planned vs driven.
- **Layers:** RTK fix quality, satellites, heading DOP, HaLow RSSI, battery, blade current (grass load), pitch/roll
  (slope), speed, wheel slip, edge passes.
- **Problem areas** (across runs, 2 m cells): stuck, place-bound errors, steep ground, weak positioning, hard going.
  The causes and thresholds are in [`robot-states.md`](robot-states.md).
- **Trends:** minutes per week, area per run, battery health, blade load by month.

---

## 7. Map Studio

### 7.1 Geometry
Each feature's `range` is a list of `{x, y, phi}` in **local metres from its own `ref`** lat/lon. Never assume one
origin for the whole map. In the SDK frame **x points west and y points north**. Conversions go through lat/lon
onto the target feature's `ref`, and round trips are unit-tested to under 1 cm.

### 7.2 Basemap: real imagery under the map
- **Sources** come from a data catalog (one JSON file per source, see [`imagery-catalog.md`](imagery-catalog.md)):
  national and regional public orthophotos, county and state services, and keyed worldwide services.
- **Privacy:** the panel contacts only sources whose coverage outline contains the yard, and it fetches imagery
  server-side, so the browser never sends the home location anywhere.
- **Alignment:** robot coordinates are centimetre-accurate RTK, but imagery is often off by 1–3 m. Align by points:
  pick a spot on the robot's map and the same spot in the photo. One pair fixes the shift, two or more fix rotation,
  three or more fix scale (least squares). The fit is stored per source and applied only to the imagery, never to
  robot data.
- **Terrain:** the 3D view raises the ground from the robot's own RTK heights over the mowed areas and a public
  elevation model around them.

### 7.3 Features
Layers for areas, no-go zones, no-vision zones, pathways, sidewalks, dead ends, charging points, snow piles, leaf
piles, humps, trimming edges and camera coverage (§11). Measure tools. **Version history** with a visual diff,
including the robot's own backups. Import and export as GeoJSON, KML and raw JSON. Overlays of the per-area
low-confidence config and the problem-area grid.

### 7.4 Editing: safeguards, all mandatory
1. Writes only while the robot is **docked and idle**, and only with the map-writing switch on.
2. Snapshot the current map (and the full area records and settings) locally before writing.
3. Validate: closed polygons, no new self-intersections, no-go zones inside an area, pathways joining areas, point
   counts within what the robot accepts (252 is the largest seen). Problems already on the robot are shown as
   existing, not blocking.
4. A before/after **diff preview**, confirmed by the user.
5. **Read back** with `get_map` after writing and compare (within 5 cm); on any mismatch, undo everything sent.
6. **Restore** any version.

Write rules that follow from the robot's behaviour (details in [`map-writes.md`](map-writes.md)):

- **Ids matter.** Plans hold `areaIds`, pathways hold `start_id`/`end_id`, area mow settings are keyed by id. So
  areas and no-vision zones are changed **in place** (same id). No-go zones, pathways and dead ends, which nothing
  refers to, are changed as "add the new one, then delete the old one".
- **The robot reuses freed ids.** History keyed by id alone can mistake a new feature for a deleted one.
- Adding or deleting an area changes ids and drops the plans and settings tied to it, so the panel does not offer
  it.
- Sidewalks and fences have no captured write commands; edits to them stay in the draft.
- Robot backups hold a second copy of every pathway and sidewalk (fresh ids), so they are de-duplicated before any
  comparison with the live map.
- A whole-map restore (`map_recovery`) deletes every plan and resets area settings. The panel refuses to restore
  unless it can first read all of that, keeps it locally, waits for the map to match, then puts settings back on
  same-id areas and recreates the plans.

### 7.5 Tidy tools
Clean up, Straighten, Round, Smooth and Square work on a feature's own points and obey the same rules
(`app/tidy.py`):

- **Tolerance cap:** no point of the outline moves further than the tolerance (default 10 cm, 2–30 cm), measured as
  the Hausdorff distance between old and new outlines sampled every 5 cm.
- **Direction bias:** a **no-go zone may only grow** (every old point stays inside the new shape); an **area or
  no-vision zone may only shrink** (the robot never mows, or ignores its cameras, past where it was taught).
  Pathways have no bias. The bias is applied per edge, not as a global offset.
- **No new crossings:** a shape never comes out crossing itself more than the robot's own copy did.
- **Refuse rather than worsen:** a tool that cannot meet the rules says why and, when more tolerance would help,
  how much.
- Every result is previewed as a ghost outline and lands in the draft as one undoable edit; nothing is sent until
  Send.

---

## 8. Plans, schedules and seasons

### 8.1 One scheduler
**The panel owns scheduling.** The robot itself keeps no schedules unless the app saves one (`read_schedules` →
`[]` on a robot that has none), so nothing fires twice. Home Assistant supplies gate inputs (weather, presence) and
receives notifications.

The trade-off is explicit: **if the panel is down, nothing runs.** Mitigations: a scheduler heartbeat sensor in Home
Assistant with an alert when it goes stale, an external uptime monitor, and the phone app as the manual fallback.

Robot-side schedules can be written (`save_schedule`, see `map-writes.md`), but a robot-side schedule starts without
the panel's gates, so the panel runs its windows itself and **never writes them**. It reads them (`read_schedules`, at
each map refresh) and shows any it finds on the Schedule page, with an event the first time one appears.

### 8.2 Plans
Plans live on the robot (`read_all_plan`). The panel can create, rename, change the areas of and delete a plan
(`save_plan`, `del_plan`), and preview its route area by area.

### 8.3 Windows and gates
A window is days, a start and end time, a plan and a head. Inside a window the checks run every minute; when they
all pass, the panel raises one confirmation (valid 30 minutes) and notifies with a link and a camera snapshot.
Gates: Conditions (§9), battery, presence (an alarm panel, `zone.home` or person entities), yard clear (§11), and
"phone app not active" (§5.5). **Auto-start** is opt-in per window and skips the confirmation, never the checks.
A skipped window records why and what would fix it.

### 8.4 Season profiles by head type
- **Mower:** the windows and gates above.
- **Snow:** a first-class profile. Snow runs are the harder ones (night, cold, worse RTK, a real deadline). The
  recorder, Runs and Health work unchanged on snow runs; snow piles and push directions come from the map.
- **Leaf:** manual.
- The UI hides controls for heads that are not attached, and a head change triggers the maintenance checklist (§15).

---

## 9. Conditions: "is the lawn dry?"

**Inputs (each optional):**

| Source | Signals | Notes |
|---|---|---|
| Tempest weather station | rain rate/accumulation, temperature, humidity, dew point | Read from the hub's local UDP broadcast (port 50222); no cloud |
| Home Assistant sensors | rain (rate, total or on/off), temperature, humidity, soil moisture | Picked from lists matched by device class |
| Robot | `RunningStatusMSG.rain_sensor_data`, mower-head rain sensor | Lower = wetter |
| Forecast | `weather.*` entities | Rain probability, temperature, wind |

**Rules, all visible in the UI:**
1. **Raining now** → Wet.
2. **Rain in the last N hours** (default 6, weighted by amount) → Wet, decaying to Damp.
3. **Dew:** (temperature − dew point) under about 2 °C overnight → Damp until sunrise + X (default 2 h).
4. **Robot rain sensor** reading wet → veto.
5. **Soil moisture** above the set level holds Damp or Wet, however long ago it rained.

Output: Dry / Damp / Wet plus "estimated dry by", with the contributing inputs listed. Conditions is a schedule
gate and a Live card. A wet lawn is a soft check a person can override by hand; the scheduler cannot.

**Deliberately not built:** a drying credit from solar radiation and wind, and self-calibration against wheel slip
and blade load. Wheel slip confounds wet grass with slope and turning; without a season of data it is guesswork.

---

## 10. Control

### 10.1 Catalogue
The source of truth for official commands is the SDK's `devices/yarbo_Y.json` → `control_topics`. The panel exposes
exactly five run commands: `start_plan`, `pause`, `resume`, `stop`, `cmd_recharge` (return to dock), plus
`smart_vision_control {"state":1}` (cameras on; people only, cloud only, never off; §13). Everything else
is refused, and **`cmd_vel` (manual drive) is hard-blocked for every actor**.

The robot sends no reply to run commands on either broker, so a command is **confirmed by the robot's state
changing** the way the command should change it (for example `planning_paused` becoming 1 after `pause`). An
unconfirmed command is still watched for a few minutes in case the robot acts late, and a late confirmation is
logged as such.

### 10.2 First-command proofs
Before a route is trusted for control it is proved with a harmless command:

1. **Cloud control:** `cmd_recharge` (return to dock) through the SDK while the robot is already docked. Nothing can
   move.
2. **Local control:** a harmless setting whose result the robot reports, on the local broker. Done 2026-10-21 with
   `set_sound_param` (as the app sends it): accepted, with no handshake and no cloud (`docs/dev/protocol.md` →
   Access control). Run commands work locally too (start, pause, stop and return to dock, each seen in the robot's
   state within about 2 s, against up to a minute through the cloud on a weak link). The panel still sends them
   through the cloud and `transport_local.py` still publishes only reads: moving commands to the local broker is a
   design change of its own (#31 would then mostly disappear).

The control route follows the result; it is a code-level decision, not a setting a user can flip.

### 10.3 Interlocks (enforced in core, not just the UI)
Every request passes, in order: the catalogue, the master switch (`control_enabled`, off on a new install), then the
interlocks for that command.

- **All commands:** the robot is online (data under 2 minutes old) and the cloud is connected.
- **Start plan:** the plan exists; no active error; battery ≥ the start threshold; not already running; presence
  rule; Conditions gate; yard clear; phone app not active (§5.5). For a start or resume requested by an AI
  assistant, a check that cannot tell (camera, weather or Home Assistant down) counts as failed.
- **RTK is not a panel-side gate.** A docked robot often has no RTK fix and no RTCM, and acquires the fix after
  leaving. The robot enforces its own RTK precondition; the panel sends the command and **surfaces the robot's
  refusal** verbatim.
- **Hard checks are never overridable.** Soft checks (a wet lawn) can be overridden by a person, never by the
  scheduler or an assistant.
- **During a run (guards, every 5 s):** a person or pet on a yard camera, rain starting, or everyone leaving → pause,
  dock or notify, per setting. With control off they only notify. **Resuming after a guard pause is off unless the
  owner turns it on** (`guard_resume`), because resuming spins the blades.
- Every command, from any actor, sent or refused, goes into the audit log with its route, payload, checks, result
  and latency.

### 10.4 Capture console
Read commands (`get_*`, `read_*`) can be sent freely. Write commands are used only after they have been captured
from the app and replayed safely (§14). **`cmd_vel` is hard-blocked**, and motor commands seen from the app (such
as `cmd_roller`) are never sent.

---

## 11. Yard safety and follow-cam (Frigate)

- **The Frigate gate is additional, never a replacement.** The robot's own person detection stays on and stays
  primary. Frigate misses things (a child behind a tree, a dog at the frame edge); it adds a second opinion and a
  snapshot.
- **Camera coverage polygons are drawn by hand.** The robot's RTK position inside a polygon means that camera sees
  it. No recognition is needed for the follow-cam to work.
- **Follow-cam:** Live shows the camera whose polygon contains the robot and switches as it moves (with a short
  hold so one stray position does not flip the picture). The stream comes from Frigate/go2rtc, with Frigate's latest
  JPEG as a fallback.
- **Confirmation by detection** (optional): `robot_lawnmower` is a built-in Frigate+ label; a detection inside the
  predicted camera confirms the feed. Detection reliability at yard distance is unproven, so the follow-cam does not
  depend on it.
- **Safety gate:** before a run, no `person`/`dog`/`cat` in the yard cameras for the last N minutes (default 10).
  During a run, a detection inside the robot's current area → pause, dock or notify, with the snapshot.
- **Yard snapshot in the confirm notification:** "Start <plan>?" with the current frame.

---

## 12. Gap touch-ups

- **Coverage is only known where RTK was fixed.** A track segment with float or single-point positioning has
  metre-scale error; it counts as **unknown**, never as covered or as a gap. Otherwise touch-ups would re-mow mowed
  grass and the Runs page would cry wolf beside every float segment.
- **Detection:** rasterise the fixed-RTK coverage (track × deck width) against the area polygon minus no-go zones.
  Candidates are uncovered cells over a minimum size (default 1 m²) that were not unknown.
- **Presentation:** gaps on the Runs map, each with size and a likely cause (missed, boundary margin, next to RTK
  loss).
- **Execution options**, best first: a robot-side spot or waypoint mow if one exists (`auto_plan_status` has
  waypoint states and the map has `mul_points`, which suggests it does; not yet captured); a temporary area and
  plan, created, run once and deleted (read-back verified); or a notification so the gap is fixed in the app.
  Touch-ups obey every gate and never enter a no-go zone.

---

## 13. Robot cameras

**Status: built, not yet proved on a robot** (the Live tile is `web/cameras.js`). Wire details, with what is confirmed
and what is not, are in
[`protocol.md`](protocol.md#robot-video-agora).

What is known:
- The robot's cameras sit on a network **internal to the robot**, not on the household LAN.
- The robot's LAN address exposes SSH, the MQTT broker ports (1883, 8883, 8083 WebSocket, 8084 TLS) and a few HTTP
  ports; none of them serves video on common stream or API paths.
- Opening the app's live view sends `smart_vision_control {"state":1}` over the **cloud** broker. The robot
  acknowledges it, and **no stream URL, SDP or token appears on MQTT**.
- The video itself goes through **Agora's real-time network**. The app asks Yarbo's cloud for a token (one channel
  per camera) and joins with it; the response also carries the stream's encryption key and salt. There is no
  official RTSP, ONVIF or streaming API, and no LAN path: a viewer at home still receives from Agora.

The panel does not get into the robot, and no video passes through the panel. It follows the app's route:

1. **Token.** A new read, `app/video.py` (not MQTT, not a publish), asks the cloud's robot service for a token, with
   the sign-in `app/cloud.py` already holds. The server chooses a camera from a fixed allowlist (front, left, right,
   back) and a random viewer uid (never the robot's uid 1), and returns the join details to the browser. The route is
   `POST /api/video/token {camera}`: it needs a person's session (`_person`; an API token is refused, it is not in
   `TOKEN_WRITES`), answers 403 while `robot_video_enabled` is off (the default; only a person can switch it on),
   is not in the MCP `ROUTES`, sends `Cache-Control: no-store`, and logs and records nothing that contains the token,
   key or salt. The robot service's address is not in this repository: `app/video.py` has no default and the panel
   reads it only from `YARBO_ROBOT_SERVICE_URL` (never from Settings: the route sends the Yarbo session JWT to that address, so a signed-in person who could set it could collect the JWT); unset, the route answers 503 "not configured".
2. **Playback.** The browser plays the stream with Agora's Web SDK, **vendored** under `web/vendor/` with a pinned
   version and a checksummed Dockerfile download, like MapLibre and three.js. Nothing loads from a CDN at runtime, and
   the script is fetched only when the setting is on and someone opens the viewer.
3. **Switching on.** The Cameras panel sends `smart_vision_control {"state":1}` when it opens, through
   `control.CATALOGUE` and `control.check`, marked as the panel's own send so `app/ops.py` does not mistake its echo
   for the phone app (`PHONE_APP_COMMANDS`). It is its own PR for separate review. **Switching off is not decided:**
   what `{"state":0}` does to someone watching in the official app, and during a run (obstacle detection), is untested,
   so a first version sends only `1` and lets the robot or the app turn the view off.
4. **Placement.** A **Cameras** tile on Live (`web/cameras.js`, a card in Live's panel, shown only while the setting is
   on and the service address is set; the demo shows a placeholder and never loads the SDK). One camera at a time.
   Opening it asks the controller for `{"state":1}` (with Control off it says so and still tries to play, since the app
   or a run may have the cameras on), fetches a token, lazy-loads the vendored SDK, calls
   `AgoraRTC.setParameter` (forced, third argument true, because the SDK ignores a plain call for a key a cached or
   remote configuration set first) for `ENABLE_EVENT_REPORT`, `UPLOAD_LOG`, `ENABLE_CONFIG_DISTRIBUTE` (a remote push
   can switch the first two back on mid-session), `ENABLE_PRELOAD` (re-requests the access point with the token every
   two minutes), `ENABLE_CREATE_CLIENT_PRECONNECT` and a `CREATE_CLIENT_PRECONNECT_MODE` of `report` (`request` fetches
   Agora hosts), and a random `INSTALL_ID`, all before creating the client (never `enableLogUpload`). The SDK's own
   `websdk_ng_*` localStorage items (device id, cached settings, preload cache) are cleared when it has loaded and on
   leaving. A load of the SDK script that hangs gives up after 20 s so the next try loads it again. It joins as
   an audience member with `aes-128-gcm2`, the key and the base64-decoded salt, and plays uid 1. It leaves the channel
   on close, camera change, leaving Live, a hidden tab and page close. A token's expiry event rejoins with a fresh token
   (the route picks the viewer uid, so a token cannot be renewed in place). No CSP is set: `connect-src` cannot admit
   `*.agora.io` and block `statscollector*.agora.io`, so an allowlist of exact gateway hosts needs a network capture.
   Live's existing "Camera" layout is the Frigate yard cameras (§11), so the tile's title carries a tooltip saying it is
   the robot's own; a rename of that layout is raised with the maintainers first. A `no-referrer` policy was
   considered to hide the panel's address from Agora and rejected: a browser then sends `Origin: null` on the panel's
   own POSTs, which `auth.same_origin` refuses.

Rules:
- **People only.** The token route is a POST (not a safe method, so API tokens are refused by default and it is
  deliberately not added to `TOKEN_WRITES`) **and** checks for a person's session explicitly with `_person`. The
  `TOKEN_WRITES` exclusion alone would not do: API tokens may make any GET. The MCP server does not offer the route,
  because token, key and salt together are a live, decryptable view of the yard.
  The response is `Cache-Control: no-store`.
- **Off by default.** A setting, off, with the person's confirmation on turning it on: the viewer's browser then
  connects to Agora, which sees the device's address and the camera's channel name. Off means the route refuses, the
  SDK is never downloaded and the button is hidden. The docs say "encrypted in transit", not "private from Yarbo or
  Agora", since the key comes from Yarbo's cloud.
- **No secrets in logs.** Token, key, salt, App ID, channel, uid and the sign-in JWT never reach a log, the event log,
  the URL, `localStorage` or the console. A log line says only which camera and ok or the error class.
- **A good citizen.** Agora bills per minute of video received, per stream, to the App ID's owner (Yarbo). So: one
  camera at a time, video only while the panel is open, leave the channel when the tab is hidden or closed. A 2x2 grid
  would be four streams and four uploads from the robot, so it is deferred.
- **Demo.** Demo mode shows a synthetic test pattern and never contacts the cloud or Agora.
- **Not testable offline:** decryption and real video. Tests use fakes and a stubbed SDK; the real stream is checked by
  the owner on a robot, and PRs say "not tried on a robot" until then.

Open until tested on a robot: token lifetime, whether the robot streams without `{"state":1}` (for example during a
run), H.264 only, the browser SDK's decryption with a bytes salt, and any cap on viewers.

---

## 14. Capture (learning commands from the app)

- **Record an app edit:** listen-only on both brokers while the owner does one thing in the official app; every
  non-read command is kept with its full payload, labelled, and shown with the map changes it caused.
- **Decoder:** zlib/JSON/binary detection; known commands annotated.
- **Command explorer:** every command seen or documented, its payloads, replies, route, and safety class
  (read / control / write / blocked).
- **Schema diff:** after a firmware update, the `get_device_msg` field set is diffed against the last version.
- **Replay rule:** reads replay freely. A write is used by the panel only after it has been captured from the app
  and replayed against a harmless test object (create and delete a small test feature), with read-back.

One capture per change type, one change per save, so each field can be matched to its meaning. The captures behind
the panel's write support are summarised in [`map-writes.md`](map-writes.md).

---

## 15. Health and maintenance

- **Positioning:** RTK status, satellites, heading DOP, RTCM age and per-source stats, low-confidence config. A
  docked robot often shows no fix and no corrections; recorded starts show whether the fix arrives after leaving the
  dock (normal) or never (a base station fault).
- **Link:** HaLow RSSI and connection state over time. An optional Wi-Fi survey maps which Wi-Fi networks the
  robot hears across the yard (`get_wifi_list`, a ~4 s scan, at most every 30 s while driving).
- **Power:** battery capacity, health, cell temperatures, voltage and current; charging history.
- **Drive and deck:** wheel and blade motor currents and temperatures, fault flags; wheel slip as a Runs layer, not a
  gate.
- **Firmware tracker:** versions recorded on change (in the database, never in docs), with an alert. A change
  should trigger the schema diff and a re-check of local access.
- **Error dictionary:** state and error codes mapped to plain English and what to do
  ([`robot-states.md`](robot-states.md)).
- **Maintenance timers:** by blade-motor hours, distance, mows, runs, charge cycles, days, or once per head change.
  Items start at Yarbo's published intervals where Yarbo gives a number. Due soon at 85 %, overdue and alerted at
  100 %.
- **Blade dullness index: experimental.** Blade current is itself the grass-load proxy, so "current at a given load"
  is circular until grass height or wetness can be controlled for. The data is recorded; no reminder fires from it.

---

## 16. Insights

- **Aggregates:** per week, month, season and year; per zone (time, passes, RTK quality, gaps); robot time vs
  conditions-skipped time.
- **Problem spots that keep coming back** across mows.
- **Exports:** CSV and the API; an MCP query path (§17).

---

## 17. Integrations

- **Home Assistant, out:** state, battery, RTK quality, current plan, last run, transport health, needs-sign-in,
  scheduler heartbeat; alerts (stuck, error, guard action, run finished, firmware changed, needs sign-in, scheduler
  stale, maintenance due). Via HA's REST API with a long-lived token.
- **Home Assistant, in:** weather and soil sensors, presence.
- **Webhooks:** run and alert events with a JSON payload.
- **MCP server** (`/mcp`, streamable HTTP, bearer token, off until a token is created). Three tiers:
  - **read:** status, runs, conditions, schedule, settings, imagery, maintenance, events, the help pages;
  - **setup:** imagery choice and alignment, schedule windows, maintenance, notify target, and run rules **only in
    the stricter direction**;
  - **control:** the same five commands, only when the separate *Assistant commands* switch and control are both
    on, through the same interlocks.

  An assistant can never switch control, map writing or assistant commands on, loosen a rule, write the map, touch
  robot backups or drive the robot. What it reads has coordinates, the serial and the account removed. Every call is
  audited.
- **Do not run three things that wake the robot.** The official and community Home Assistant integrations poll the
  same robot; one client polling is enough.

---

## 18. Security

- **The robot's local broker is unauthenticated.** Anything on the same network can read the map and the robot's
  live position. Put the robot on a segment where only the panel host (and Home Assistant, if it talks to the robot)
  can reach its broker ports. The phone app uses the cloud, so such a rule does not break it.
- **The panel:** LAN only by default. For remote access use an authenticating reverse proxy or a VPN; never a raw
  port forward.
- **Secrets:** the Yarbo password is never stored; the cloud session is kept with owner-only permissions in
  `DATA_DIR`; tokens come from the environment or settings. Captures and the database contain the home GPS
  position and stay on the panel host. Secret fields in captured payloads (such as a Wi-Fi password in
  `change_to_wifi`) are masked before they are stored as events.

---

## 19. Risks

| Risk | Mitigation |
|---|---|
| Cloud control stops working for third-party clients | Routing is isolated in `cloud.py`; the SDK is pinned; the recorder needs only the local broker |
| The phone app and the panel fight for the robot | §5.5 guard; confirmation by state change |
| RTK gating would block every start | §10.3: the robot gates, the panel surfaces the refusal |
| Gap detection flags RTK-float segments | §12: fixed-RTK only |
| The panel is down, so nothing mows | §8.1: heartbeat alert, uptime monitor, phone app as fallback |
| Frigate misses a person or pet | §11: additional to the robot's detection, never a replacement |
| A firmware update closes the local broker | Reads degrade, history is unaffected, the firmware tracker alerts |
| A firmware update renames telemetry fields | §6.3 tolerant ingestion; schema diff |
| A bad map write | §7.4: docked only, validation, diff, read-back, undo, restore |
| Session-expiry loops | §5.3 |
| The robot's stuck flag misses a real stuck | §6.4: the panel's own stall rule |
| A reply is attributed to the wrong request | §5.1: serialised reads; answers for another id refused |

---

## 20. Build order

The panel was built in this order; each step works without the next.

| Step | Scope |
|---|---|
| 1a | Read-only transports, recorder, Live map on imagery |
| 1b | Runs analytics and layers, gap detection, imagery alignment, Health, Conditions |
| 2 | Control catalogue, interlocks and audit; scheduler, windows and gates; Frigate polygons, follow-cam and guards; notifications; maintenance timers |
| 3 | Capture of app writes |
| 4 | Map Studio editing with read-back; plans; robot backups; tidy tools |
| 5 | Integrations: webhooks, MCP server, Home Assistant inputs |
| 6 | Setup wizard, in-panel help, light theme, phone layout |

---

## 21. Distribution

- **Shape:** one Docker image (core + web), one SQLite file in a mounted `DATA_DIR`; a setup guide on first run.
- **Setup guide:** robot address (local broker check) → Yarbo cloud sign-in (optional; only for commands and map
  writes) → AI assistant (optional) → imagery → cameras → Home Assistant → weather → safety (recommended: control
  off, yard must be clear, no resume after a guard pause) → notifications. Every step skippable.
- **No household data in the repository:** no ids, addresses, serials or coordinates.
- **Licence:** MIT (the Yarbo SDK is MIT). Not affiliated with Yarbo. Writes stay behind explicit opt-in and the
  safeguards above.

---

## 22. Open questions

- Not yet known: whether the robot has a spot or waypoint mow command usable for gap touch-ups (§12).
- Not yet known: exactly who wins when the phone app and the panel command the robot at the same moment (§5.5).
- Not yet known: how the app's remaining area settings map to the robot's numbers (mowing height scale, turning
  mode, route pattern and others; see `map-writes.md`).
- Of the robot's three headings (RTK heading, odometry `phi`, IMU yaw), the panel draws odometry `phi` (the 2D arrow
  and the 3D model): it was checked against the direction actually driven and matched it (`telemetry.heading_deg`).
  RTK heading and IMU yaw have not been compared.
