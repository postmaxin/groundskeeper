# Architecture

How the panel is put together now: modules, features, configuration and API. Each subsystem's rules are in
[design.md](design.md), the reasons for the larger decisions in [decisions/](decisions/), and user help in
[docs/user](../user/).

## What it does

- Listens to the robot through the Yarbo cloud broker (`snowbot/{SN}/#`) and keeps the latest full state.
- Records telemetry (1/min idle, ~1 Hz during a run) and a raw snapshot every minute (kept 90 days) into SQLite.
- Detects runs (one plan session each, design §6.4) for any head, mower or snow, and stores each track.
- Reads the map, plans, GPS reference and low-confidence config on start and every 6 h, versioned on change.
- Passively keeps the reply to **every** read request seen on the broker, including other clients' requests (this
  is how `read_schedules`, `get_all_map_backup`, `read_recharge_point` etc. show up without us sending them).
- Counts commands other clients publish; an event is logged the first time a new one appears.
- Tracks firmware versions and logs changes.
- UI: the yard on aerial imagery (see Map imagery; fetched and cached by the server) with zones, labels, the robot,
  and tracks coloured by RTK quality; Status, Runs, Data (browse every field) and Events tabs; live updates over SSE.

## Look and navigation

Six nav items: Live, Runs, Map Studio, Schedule, **Robot** (tabs: Health, Maintenance, Events) and Settings; Help is
the **?** by each title and the link at the foot of the nav. One palette as CSS tokens in `web/style.css`
(`docs/dev/ui-design.md`): status, map shapes, analysis and neutral data never share a hue; the JS reads the same
tokens (`tok()` in `web/app.js`). A light theme follows the device or Settings → This panel → Theme (per browser).
Control and map writing are switches in the top bar; status chips link to where you act on them. Charts have a value
scale and a hover readout.

## Help, version and changelog

- **Help** (`#help`, `web/help.js`): the user docs in `docs/user/*.md` and `CHANGELOG.md`, served by `/api/help` and
  rendered in the panel (a small markdown subset, DOM only). The **?** beside each view's title opens its page.
  Keep pages short; `docs/user/not-yet.md` lists what the panel cannot do yet, and a feature landing removes its line.
  `tests/test_help.py` checks every link in the pages.
- **Version** in `app/version.py` (`/api/about`, `/health`, Settings → This panel, under the robot card); the build is
  the git commit, passed by the deploy scripts. A release that changes behaviour bumps it, moves the changelog's
  *Unreleased* entry under the new number, and is tagged `v<version>`.

## Set up

Settings → Set up (`secSetup` in `web/settings.js`) walks a fresh install through robot, cloud, AI assistant,
imagery, cameras, Home Assistant, weather, safety and notifications. Each step reuses its section's own controls;
`GET /api/setup/status` (also MCP `setup_status`) says which are done from state the panel already keeps, so the
wizard stores nothing. It opens itself once per browser session while the panel is not signed in to the Yarbo
cloud or has no map (never on the demo). Help page: `docs/user/setup.md`.

## Why it cannot run

`app/why.py` turns every cause into one reason with a fix and a link: failed start checks (control preview and
refusals), no plans, the wrong head, a skipped schedule window (logged with its reasons and notified), the robot
pausing itself (alert once per pause) and a run that ended before its plan finished (`end_why` on runs). Live,
Schedule, a run's page and notifications all use these words. Maintenance items start at Yarbo's own intervals
(`app/maintenance.py` `RECOMMENDED`, with sources); older installs are upgraded once, keeping intervals the user set.

## AI assistants (MCP)

`app/mcp_server.py` serves the panel to MCP clients at `/mcp` (streamable HTTP, stateless). Off until a token is made
in Settings → AI assistant (only its SHA-256 is kept); every request needs `Authorization: Bearer <token>`. Tools call
the REST handlers in-process, so validation, interlocks and logging are the UI's. Tiers: read; setup (each needs
`confirm=true`, and says what it would do without it); control (`robot_command`: needs the **Assistant commands**
switch in Settings → Safety, plus control and every interlock; no override; for actor `mcp` an unknown check is a
fail on start and resume). `confirm` is set by the assistant, so it is a prompt, not consent: anything that loosens
safety, blinds a check or sends the yard somewhere new is simply not offered (run rules only tighten; no safety
switches, guard auto-resume, auto-start windows, robot/Frigate/HA/weather connections, presence entity, camera
coverage, webhook, non-notify HA services, trying or saving a new imagery source, map writes, backups or cloud
sign-in; discovery refuses private addresses). In-process calls are limited to an allowlist of routes (`ROUTES`)
and ids must match `SAFE_ID`. Results lose places and identities (`scrub`: keys, coordinate-like number lists, URL
queries and decimals in text); imagery tools describe the yard by catalog regions. The server instructions carry
the method for finding the most detailed imagery (compare `native_m` in cm, look at samples, search the region for a
county or state service, hand the address back), and `catalog/FINDING.md` (help page `imagery-finding`) has the
cm scale. User page: `docs/user/mcp.md`; the token and connect snippets live in Settings → AI assistant.

## Runs, analysis and health

- **Runs:** the overview (`#runs`) is every run in one table (mows with coverage, gaps, pace and problems; short
  runs dimmed), then totals, trends and the problem spots that keep coming back (what used to be the Insights page;
  `#insights` still lands here). A run (`#runs/<id>`) shows its stats (duration, distance, RTK %, coverage of the known
  area, unknown area, gaps, battery and energy per 100 m²), a deck-width swath, replay with a scrubber and coverage by
  area. The map's "Colour" control colours the track (live: RTK fix or edge passes; a run also satellites, HaLow,
  battery, blade load, slope, speed or wheel slip) and the legend at the map's foot explains it.
- **Gap detection** (`app/coverage.py`): 0.25 m grid in the robot's own metres. **Only RTK-fixed track counts.**
  Float/single-point stretches are "unknown", never gaps. No-go zones are excluded, and gaps under 1 m² are ignored.
  Each gap is labelled missed, boundary margin, or next to RTK loss. What earlier runs of the same plan session mowed
  counts as done (a resume that was not joined onto them). Computed when a run ends and cached.
- **Planned vs driven** (`app/planned.py`, a run's page): the robot's own plan kept during the run (every pass of
  each area, edge and fill, from where the run took it up), else the robot plans the run's areas (route preview,
  compute only); the mowing track is compared with it — near the plan AND heading along it counts as on plan; the stretches off it
  are drawn pink and listed (across the stripes, or away from the plan). Older runs use today's outlines and settings.
- **3D map** (`app/elevation.py`, map tool "3D"): MapLibre terrain from one ground made of the robot's own RTK
  heights (telemetry `hgt` from 2026-10-08, the one-a-minute raw snapshots before) over the lawns and the USGS 3DEP
  1 m lidar (flown March 2015, fetched once for the map plus 15 m, kept in `DATA_DIR/elevation/`) around them, shifted
  to agree — the lidar is ~0.35 m off the robot (median; the yard was likely regraded). Served as Terrarium tiles
  (`/api/elevation/ground/{z}/{x}/{y}.png`); the photo, zones, track and replay lie on it. (It replaced the flat
  Terrain layer, 2026-10-10.)
- **Problem areas** (`app/problems.py`, map tool "Problem areas"): where the robot really has trouble, per 2 m cell
  and ranked spots — stuck / no progress, place-related robot errors, steep ground, weak positioning, hard going —
  from recorded runs (or one run), with the evidence. Weak positioning names the link when it was weak there too
  (HaLow ≤ −77 dBm, corrections late or missing). Background: `docs/dev/robot-states.md`.
- **Signal** (map tool): HaLow, RTK fixed share or satellites per 2 m cell over 30 days, and the **Wi-Fi survey**
  (Settings → Robot: off / next run only / every run): `get_wifi_list` every 30 s while a run drives (one ask at a
  time, none while parked), each network's signal stored mid-scan where the robot was (table `wifi_seen`, 90 days;
  an answer later than 12 s is counted, not placed: while mowing the robot often takes 30-60 s),
  one network per 4 m cell on the map. The scan takes ~4 s on the robot, so "next run only" surveys one run (10 min
  of driving), reports in Events (answers, reply time, pauses > 3 s in the robot's own messages within 10 s of a scan
  and otherwise) and switches itself off.
- **Imagery alignment** (Settings → Map imagery → Align on the map): drag or nudge the imagery and rotate it, with an
  opacity slider.
  Stored per install; the robot's map never moves.
- **Conditions v1** (`app/conditions.py`): Dry / Damp / Wet from five visible rules (raining now, decayed rain,
  dew, robot rain-sensor veto once calibrated, soil moisture), with an estimated dry-by time. Inputs come from the
  Tempest and/or Home Assistant sensors, chosen per input (`app/weather.py`, Settings → Lawn conditions) from lists
  that `GET /api/ha/sensors` matches by HA device class (the same call lists presence candidates, `app/ha.py`; `GET /api/ha/notify_services` lists the notify services for Settings → Notifications). The weather comes from the
  **Tempest hub's local UDP broadcast** (port 50222), so no account, cloud or HA is needed. That is why the
  container uses host networking.
- **Health** (Robot → Health): the robot now and over time: positioning (fix now, fix share, satellites, DOP, correction age),
  link, power (charge, voltage, cell temperatures), deck and CPU, firmware history, error codes plus a status
  dictionary; 24 h / 7 d / 30 d. The weather station lives in Settings → Lawn conditions and on Live.
- **Events:** one log by day with filters (commands, robot and runs, map, panel) and search; it includes every
  command the panel sent or refused (the control log). Raw JSON from the robot and the panel is in Settings → Raw data.
- **Home Assistant (optional):** with `HA_URL` and `HA_TOKEN` (a long-lived token) set, it publishes
  `sensor.yarbo_panel_{state,battery,rtk,lawn,local_link,last_run}` and `binary_sensor.yarbo_panel_running` every
  minute. Put the token in Settings → Home Assistant, or in the `.env` file, never in the compose file.
- **Demo mode:** `DEMO=1` (`DEMO_SEED=1` is the older name) with its own `DATA_DIR` runs the panel against a made-up
  robot instead of the Yarbo cloud. `app/demo_robot.py` has the yard (three lawns, a flower bed and a tree as
  no-go zones, a no-vision zone, two pathways and a dock, laid out on a public park's lawn in Portland, Oregon, so
  the bundled keyless city imagery shows grass under it), `DemoCloud`, which stands in for `CloudTransport`, refuses
  anything outside `READ_ONLY_COMMANDS` and takes no commands, and `DemoTransport`, which answers the recorder's
  reads (`get_map`, `get_device_msg`, `get_plan_feedback`, `read_all_plan`, area and pathway settings, backups,
  route previews...) and
  publishes `heart_beat` and `DeviceMSG` in the robot's own shapes. Its robot mows plans for real: an edge lap, then
  stripes around the no-go zones at 0.5 m/s (`DEMO_SPEED` speeds it up), the battery draining, RTK float and single
  under the tree, then home to charge. `app/demo.py` seeds a fresh `DATA_DIR` once: three mows over the last week
  (one with a gap), the first 45 % of today's mow (the recorder joins the robot's carrying-on onto it, as after a
  restart mid-plan), events, Wi-Fi scans, weather, two schedule windows and maintenance items. Demo mode ignores the
  robot serial, Home Assistant, Frigate and Tempest settings, never restores or opens a cloud session, and shows a
  "Demo data" chip. `scripts/deploy_demo.sh` starts one as `yarbo-panel-demo` on port 8426, so changes and the
  browser tests can be tried without touching the live panel.
- **Screenshots:** `python scripts/screenshots.py` starts the panel in demo mode (127.0.0.1, a free port, a temporary
  `DATA_DIR`, `PANEL_AUTH=off`), waits for the robot to be mid-mow and the imagery to be in, captures every page with
  headless Edge through Playwright (WebGL from SwiftShader for the 3D view) into `docs/screenshots/`, optimises the
  PNGs and stops the panel. Re-runnable; only the clock differs between runs.

## Control, schedule, cameras and alerts

- **Yarbo cloud** (`app/cloud.py`, Settings → Robot). You sign in in the panel; the password goes straight to
  Yarbo's SDK (which RSA-encrypts it) and is never stored or logged. Only the session tokens are kept, in
  `DATA_DIR/cloud_session.json` (chmod 600), and the rotated refresh token is saved after every refresh. A failed
  refresh stops reconnecting and shows "needs login" instead of retrying with a dead JWT (the HA integration's bug).
  The SDK's migration lookup picks the NEW or LEGACY broker per robot. Credentials **and the client id** come from
  the SDK's own providers: the brokers refuse any client id that is not the SDK's `HA_{email}_...` scheme.
- **Control** (`app/control.py`): see "What can command the robot" below. Live has a control card (plan picker with
  the plan's areas, a readiness line that runs the interlocks without sending, Start/Pause/Resume/Stop/Return to dock;
  only the buttons that make sense in the robot's state are on). Schedule shows the same readiness check unfolded.
- **Scheduler** (`app/scheduler.py`, Schedule view). The panel owns scheduling (the robot keeps no schedules of its
  own: `read_schedules` returns `[]`). A window is days + start/end + plan + head (mower/snow/any). Inside it the
  gates are checked every minute; when they all pass the panel raises **one** pending confirmation (30 min TTL) and
  notifies with a link and a camera snapshot. Confirm or dismiss on Live or Schedule. `auto_start` skips the
  confirmation but still goes through every interlock as the scheduler. Each window's daily outcome (pending,
  started, dismissed, expired, skipped with the last reason, failed) is logged and shown as history.
- **Guards during a run** (every 5 s): a person or pet on a yard camera, rain starting, or everyone leaving (Alarmo
  armed away) each trigger pause, dock or notify (Settings → Run rules). Actions go through the controller, so with
  control off they only notify.
- **Frigate** (`app/frigate.py`): tick the cameras that see the yard (they feed the yard-clear gate and the guard),
  and draw each camera's coverage on the map (Settings → Cameras → Draw coverage; click points, double-click to finish). The
  **follow-cam** on Live shows the camera whose polygon contains the robot, refreshed every 3 s, with pins to
  override. Live's **Camera** layout (`.layout.cam`, chosen per browser) puts it in the map's slot and moves `#map`
  into a card; the panel then leads with two-tap Pause/Resume, Stop and Return to dock through `/api/control/send`. Snapshots are proxied at `/api/frigate/snapshot/{camera}.jpg` so phones can load them.
- **Notifications and alerts** (`app/notify.py`): every alert is written to Events, then sent to the HA notify
  service named in Settings (needs `HA_URL`/`HA_TOKEN`) and/or a webhook. Alerts: robot error code, stuck, head
  changed, cloud needs a new sign-in, maintenance due (checked daily).
- **Maintenance** (`app/maintenance.py`, Robot → Maintenance): items counted in blade-motor hours (from run samples
  with blade rpm > 0), km of runs, mows, runs, charge cycles, days, or head changes. Set the intervals from Yarbo's
  manual; "Mark done" restarts the count.
- **Presence**: an HA entity chosen in Settings (Alarmo `armed_away`/`armed_vacation` = nobody home). Needs the HA
  token.

## Map Studio (design §7)

The Map Studio view (`app/mapstudio.py`, `web/studio.js`) works on the robot's map without writing to it.

The side panel has four tabs: **Yard** (the draft on top when there is one: changes, checks, Send; then the selected
shape with its tools, and one list of every shape: click to select, the pencil or F2 to rename in place), **Plans**
(make, edit and delete plans), **History** (this panel's versions
and the robot's backups, newest five with "Show all") and **More** (import/export, captured writes). Explanations
are tooltips: hover a button or an ⓘ.

- **Inspect:** click any area, no-go zone, no-vision zone, pathway or sidewalk (on the map or in the list) for its
  points (against the robot's 252-point limit), area, perimeter or length, on/off state, which areas a path joins,
  the per-area low-confidence limits, and the robot's own area figure (it measures some areas about 0.3 m inside
  the drawn boundary).
- **Measure** distances and areas; **export** GeoJSON, KML or the raw map JSON; **import** a GeoJSON or KML file
  as a display-only reference layer (a survey, an old export).
- **History:** every map version the recorder has seen, plus **the robot's own backups** (it lists 20: 10 automatic and 10 manual slots; auto and
  manual). Fetching a backup is a read (`get_map_buckup_from_id`, the robot's spelling); its singular keys
  (`area`, `nogozone`, …) are mapped to `get_map`'s names. **Compare** any version or backup with the current map
  (added / removed / changed, with how far each shape moved) and see the differences drawn on the map.
- **Editing** happens straight on the map. The bar under the map tools has Select, + Area, + No-go, + No-vision,
  + Pathway, Measure and Undo. Click a shape and its corners are editable at once: drag a corner, drag or click an
  orange midpoint to add one, right-click to remove one, drag a no-go or no-vision zone whole to move it; Delete
  removes the shape, Ctrl+Z undoes, Esc deselects. Rename and enable/disable in the panel; "Simplify to 252 points"
  appears when a shape is over the robot's limit. New areas and pathways copy the structure of the robot's existing
  ones; a pathway's ends are joined to the areas they sit in.
- **Drafts:** every edit goes into one draft kept on this server; the first edit starts it (from the current map),
  and "Draft" on any version or backup starts one from that ("Restore" on a robot backup replaces the robot's map). New ids start above every id the robot has used in
  any stored version or backup. The draft is shared by every open window, so each edit carries the draft stamp it
  was based on: an edit from a window that is out of date is refused (409) and that window reloads instead of
  overwriting someone else's work.
- **Checks** (§7.4): too few or too many points, self-crossing outlines, tiny shapes, duplicate ids, repeated
  points, zones outside every area, paths joining unknown areas. Problems the robot's map already has (it has a few
  tiny kinks in "All Yard") are shown as "already on the robot" and never block; only what a draft introduces does.
- **Sending a draft to the robot** (`app/mapwrite.py`) uses only the commands captured from the Yarbo app on
  2026-10-01 (capture C2), with the app's own payload shapes: `save_nogozone` / `del_nogozone`, `save_pathway` /
  `del_pathway`, `save_mower_pathway_params`, `upload_cloud_map_backup`. The robot assigns ids and returns the saved
  feature. **No-go zones and pathways** can be added, deleted and changed (a change is sent as add-the-new,
  delete-the-old; a replaced pathway keeps its mowing settings via `read_mower_pathway_params`). **Areas** and
  **no-vision zones** are changed in place, same id (`save_clean_area`, 2026-10-07; `save_novisionzone` with the
  whole zone record, 2026-10-10: on/off, shape, name). Adding or deleting either is not offered (not captured for
  no-vision zones; areas are recorded by driving), so the draft lists such changes as not sendable with a Revert
  button.
  Safeguards: a separate **map writing switch, off by default** (typing ENABLE switches it on); Yarbo cloud
  connected; robot online, **docked and idle**; draft valid and based on the robot's current map (re-read just
  before sending); a confirmation listing every command. Adds go first and deletes last. Afterwards the map is
  **read back and compared** (shape within 5 cm, name, on/off, nothing else touched); on any failure or mismatch
  everything sent is **undone** (added features deleted, deleted ones re-added). Every command is in the control
  log (actor `map`, names and counts only).
- The recorder keeps the full payload of every write command other apps send (Map Studio → More → Captured writes); that
  is how the formats above were learned. Not captured yet: adding or deleting a no-vision zone, and editing a no-go
  zone or pathway in place.
- `scripts/e2e_studio.py` drives the Studio in headless Edge with real mouse and keyboard events (23 checks: select,
  drag, move, insert and remove corners, rename, delete and undo, draw a zone, a pathway and an area, measure,
  refused send). **Run it against the demo instance**, which is its default: it refuses to touch a draft that has
  edits. `scripts/deploy_demo.sh` builds the working tree as `yarbo-panel:demo` and recreates only the demo (:8426),
  so changes can be tried without restarting the live panel.

- **Tidy** (`app/tidy.py`): select a shape for Clean up (duplicate and in-line points), Straighten (wobbly edges to
  straight lines, real corners kept), Round (a near-circle to a true circle), Smooth (gentle curves, sharp corners
  kept) and Square (four corners to a rectangle), each previewed as a dashed outline before it goes into the draft.
  **Tidy all…** (on the Shapes list) suggests one cleanup per shape and applies the ticked ones as one draft edit. Rules: no part
  of an outline moves further than the tolerance (2–30 cm, default 10); a no-go zone only grows and an area or
  no-vision zone only shrinks; nothing comes out crossing itself more than before. A tool that needs more tolerance
  says how much. Tidied no-go zones, pathways, areas and no-vision zones can be sent (areas and zones in place).
- **Drawing aids:** new points land on nearby corners (10 px) and edges (8 px); Shift gives 15° steps from the
  previous segment; Alt places freely. The add tools also draw circles (centre, then edge; a no-go circle is drawn
  just outside so all of it stays out of bounds) and rectangles (two corners, then depth), and can straighten a
  point-drawn shape within 5 cm when it is finished.

## Map imagery

`app/imagery/` fetches one image of the yard (the map's bounds plus 60 m) on the server, at the chosen source's
own resolution (at most 4096 px a side), and keeps it.
- **The catalog is data**, not code: **one JSON file per source** under `catalog/sources/<country>/`, shared
  coverage outlines in `catalog/coverage/`, and the zoomed-out world layer in `catalog/context/world.json`.
  **Adding a source: [catalog/README.md](catalog/README.md)**; what is bundled: [catalog/SOURCES.md](catalog/SOURCES.md)
  (generated). Each entry has a **coverage outline**, a structured licence (SPDX id, commercial yes/no/unknown,
  credit), `auth` for keyed services, and the **public landmark** it was checked at. Bundled (each rendered at its
  landmark on 2026-10-03):
  - United States: USGS NAIP (lower 48, 60 cm), plus state and county services in every state (Maryland's MD iMAP,
    7.6 and 15 cm, for one).
  - Netherlands: PDOK (8 cm). Switzerland: swisstopo SWISSIMAGE (10 cm). France: IGN BD ORTHO (20 cm).
  - Spain: PNOA (25 cm). Austria: basemap.at (about 30 cm). Luxembourg: geoportail.
  - Belgium: Flanders (2025 mosaic) and Wallonia. Germany: North Rhine-Westphalia (10 cm) and Bavaria (40 cm).
  - Czechia: ČÚZK. Poland: GUGiK. Japan: GSI. Australia: New South Wales.
  - Free with the user's own key: New Zealand (LINZ) and Finland (NLS).
  - Worldwide with the user's own key: Esri World Imagery, Mapbox Satellite, MapTiler Satellite.
  The Flanders layer is a yearly mosaic (`omwrgb25vl`) and needs bumping when a new year is published.
- **Local sources:** the same files in `DATA_DIR/imagery/sources/` (outlines in `DATA_DIR/imagery/coverage/`); a
  local id equal to a bundled one replaces it. The custom source in Settings is one of these (`custom.json`). Bad
  files are skipped and listed in Settings. The first catalog's `DATA_DIR/imagery_catalog.json` and the old custom
  source setting are moved there on start (the old file is renamed `.migrated-v1`).
- **Kinds** are the protocols, in `app/imagery/kinds/`: `tiles` (a `{z}/{x}/{y}` template in the Web Mercator grid;
  XYZ, WMTS, TMS, 512 px tiles), `wms` (GetMap; 1.3.0 or 1.1.1, EPSG:3857 or EPSG:4326, with each version's axis
  order), `arcgis_image` (ImageServer exportImage) and `arcgis_map` (MapServer export). Each declares its fields;
  `catalog/schema.json` is generated from them (`python -m app.imagery schema --write`). Exports larger than a
  server's `max_px` are fetched in chunks and joined; tiles are stitched.
- **Contributor tools:** `python -m app.imagery verify [file] --live` (schema, outline, landmark claims; renders at
  the landmark and checks `max_zoom`, `max_px`, WMS layers and links), `sheet` (every keyless source at its
  landmark on one image), `table`, `schema`, `coverage --country CC` (an outline from Natural Earth).
- **Privacy:** the panel decides on its own which entries cover the yard, and contacts only those, plus the custom
  source and worldwide sources the user has given a key. Probing every source would send the home location to all
  of them. A source that does not cover the yard is refused before any request is made.
- **Settings → Map imagery** is a picker:
  - **one card per source that covers the yard, showing that source's own 40 m sample of the yard** at its own
    resolution (at most 512 px), with resolution, licence (Public domain / Free · credit / Your key /
    Non-commercial) and season badges; click the sample for its details (provider, vintage, licence, credit line,
    the landmark its contributor checked it at);
  - **Automatic** (the default for a new install) uses the sharpest free source with real imagery at the yard, looks
    again when an update changes the free sources there, and notes a switch in Events; **My choice** keeps one. A
    choice saved before automatic mode existed stays the user's own;
  - keyed services sit in their own row and are contacted only once a key is saved (then sampled at once); keys stay
    on the server and are never sent back;
  - the samples come from an **upkeep task**: 30 s after start and hourly, it re-samples sources whose sample is
    missing, older than their `cache_days` (90 at most) or taken more than 25 m from today's yard centre. Like every
    other request it only contacts sources whose outline contains the yard; "Check again" does the same on demand;
  - **Browse the whole catalog** lists every bundled source by country, with search and filters, contacting none;
  - **Add a source** is a guided flow: paste an address; the panel asks only that address what it serves (ArcGIS
    `?f=json`, WMS GetCapabilities, or one world-scale tile; never with a location) and drafts the entry: type,
    layer, resolution (State Plane feet and other projections converted by asking the server for its own extent in
    Web Mercator), credit, year, licence where the service states one. **Try it at your yard** is the first request
    that carries the yard to that service (for tiles it walks down from zoom 21 to find the deepest one there). Save
    keeps it in `DATA_DIR/imagery/sources/` as a source of your own;
  - **Export** (on your own sources) makes the catalog file, and its outline, for a pull request. The yard stays out:
    the landmark must be a public place at least 2 km from the yard and inside the coverage; an outline must be at
    least 20 by 10 km; keys in the address become `{key}`. The protocol-specific fields of the form come from the
    kinds, so a new kind gets its form without UI work.
- **Alignment is kept per source**, with scale. **Align by points** (map tools → Align imagery):
  - pick a spot on the robot's map (it snaps to zone corners), or use the robot's own position, then the same spot
    in the photo;
  - one pair fixes the shift; two or more give a least-squares fit of shift and rotation (scale too with 3+), with
    each pair's leftover error in cm.
- **Around the yard**, two map-tile layers, both fetched and cached by the server:
  - **The chosen source itself, from zoom 12 in**, as `/tiles/src/{z}/{x}/{y}.jpg`. Tile sources are passed
    through; ArcGIS and WMS sources are rendered per tile. It is drawn only within 5 km of the yard and inside the
    source's coverage, and other tiles get 204 with no request upstream. Blank tiles are remembered.
  - **A worldwide layer when zoomed out**, `/tiles/world/...`, set in the catalog's `context.world`. The default
    is **Sentinel-2 cloudless 2025 by EOX** (about 10 m), asked only for zoom 12 and out, so it sees roughly which
    city is in view and never the yard. Its licence is **non-commercial (CC BY-NC-SA 4.0)**: fine for someone
    running the panel for their own yard; a commercial distribution must swap it in the catalog (or remove it
    with `"world": null` in the local catalog). Settings has a switch to turn it off.
  The map shows the yard source's credit and resolution; the world layer's credit is in the map attribution.

## What can command the robot

- **One route, the Yarbo cloud:** `app.link.RobotLink` alone reaches `CloudTransport.publish`. Its `read()` takes
  only `READ_ONLY_COMMANDS` in `app/commands.py` (`get_device_msg`, `get_map`, `read_all_plan`, `read_gps_ref`,
  `read_low_cfd_config`, `read_tow_params`, `get_all_map_backup`, `get_map_buckup_from_id`...); anything else raises
  `ReadOnlyViolation` before the network is touched. There is no local route (firmware V3.14.4x closed it, #91).
- **Commands:** only `app.control.Controller.send()` sends run commands, through the link, each once. It is
  called by `POST /api/control/send` (which requires `confirm: true`) and by a confirmed or auto-start schedule
  window. Every call goes through, in order:
  1. **Catalogue:** `start_plan {"id": N}`, `pause {}`, `resume {}`, `stop {}`, `cmd_recharge {"cmd": 2}` (the
     official SDK payloads), `smart_vision_control {"state": 1}` (cameras on) and `set_working_state {"state": 1}`
     (keep-awake for the Cameras tile): both people only, cloud only, never off. Anything else is refused.
     **`cmd_vel` (manual drive) is blocked for good.**
  2. **Master switch** `control_enabled`, **off by default**. Switching it on in Settings → Safety needs typing ENABLE and is
     logged as an event.
  3. **Interlocks.** Every command needs the robot online (snapshot under 2 min old) and the cloud connected.
     `start_plan` also needs: the plan exists, no active error, battery at or above the minimum, not already
     running, the yard clear on Frigate, the phone app not driving it, a dry lawn, and someone home. Hard checks can
     never be overridden. Unknown Frigate or phone-app state does not block; unknown presence blocks only the
     scheduler. A wet lawn is a soft check a person can override, and a hard one for the scheduler.
  4. **Audit:** every attempt, sent or refused, goes to the control log with its checks, reply and latency.
- Everything else either reads or changes this panel's own settings.

## Repo layout

- `app/`, `catalog/`, `web/`, `docs/user/`, `CHANGELOG.md` — the panel (the only paths that go into the image).
- `docs/user/` — the in-app Help; `docs/dev/` — design, protocol, map writes, robot states, UI and imagery notes;
  `docs/dev/decisions/` — the ADRs.
- `scripts/` — every check (`check.py`), deploy to a remote Docker host over SSH, the demo instance, the headless Map
  Studio check, and the README screenshots (`screenshots.py`, from the demo).
- `deploy/` — the Unraid template and Compose file, and the Helm chart.
- `tools/` — standalone protocol tools from before the panel: `yarbo_cloud.py` and `yarbo_capture.py` (the cloud
  broker; need `tools/requirements.txt`), and `design/gen.py` (the design-canvas artboards, into the ignored
  `tools/design/project/`). Their cloud session and captures live in `~/.yarbo/`, outside the repo, because they
  contain the yard's GPS position.

## Configuration

Everything site-specific is set in the panel: **Settings → Robot** (the Yarbo cloud sign-in; the serial is always found, never set),
**AI assistant** (optional, recommended next: a token and connect snippets so an MCP client such as Claude can do
the rest by conversation, including finding the most detailed imagery for the yard),
**Cameras** (Frigate), **Home Assistant** (address and token), **Lawn conditions** (the Tempest UDP port),
**Notifications** (this panel's own address, for links) and **This panel** (polling and retention). Changes apply
at once, without a restart, and are kept in the settings table in `DATA_DIR`. Each field shows where its value came
from and offers a way back to the environment's.

The environment supplies **defaults** for the same values, which is handy for a scripted install; nothing in it is
required. Put them in a `.env` file next to `docker-compose.yml` (see `.env.example`; chmod 600), which the
Compose stack reads; the stack file itself holds nothing site-specific.

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./data` | SQLite and imagery cache |
| `PORT` | 8425 | HTTP port |
| `POLL_IDLE_S` / `POLL_ACTIVE_S` | 60 / 2 | Ask for a snapshot when ours is older than this |
| `MAP_REFRESH_S` | 21600 | Map/plans/config refresh interval |
| `RAW_EVERY_S` / `RAW_KEEP_DAYS` | 60 / 90 | Raw snapshot cadence and retention |
| `RUN_IDLE_END_S` | 300 | A run ends after this long without movement |
| `TEMPEST_PORT` | 50222 | Tempest UDP listener; `0` turns it off (needs host networking) |
| `HA_URL` / `HA_TOKEN` | unset | Publish entities to Home Assistant when both are set |
| `DEMO` | unset | `1` runs the demo: a made-up robot and yard, no real robot or integrations (`DEMO_SEED=1` is the same) |
| `DEMO_SPEED` | 1 | With `DEMO=1`: how much faster than real time the demo robot mows |
| `FRIGATE_URL` | unset | Frigate base URL (no auth) |
| `PANEL_AUTH` | on | `off` turns sign-in off (only behind a reverse proxy that signs people in; see `app/auth.py`) |
| `PANEL_URL` | unset | This panel's own URL, used for links and snapshot images in notifications |
| `TZ` | UTC | The scheduler's clock |

## API

Reads: `/health` · `/api/status` · `/api/ha/summary` (for an HA integration; `api` versions it) · `/api/ha/stream` (SSE for its map card) · `/api/stream` (SSE) · `/api/map` · `/api/map/raw` · `/api/plans` · `/api/cfd` ·
`/api/snapshot` · `/api/replies` · `/api/commands` · `/api/runs` · `/api/runs/{id}` · `/api/track/points?since=&hours=` ·
`/api/series?metric=&hours=` · `/api/events` · `/api/stats` · `/api/imagery` · `/api/runs/{id}/points` ·
`/api/runs/{id}/coverage` · `/api/runs/{id}/plan-feedback?raw=` · `/api/problems?days=&run_id=` · `/api/signal?metric=&network=` · `POST /api/studio/backups/save` · `POST /api/studio/backups/{id}/delete` · `POST /api/studio/backups/{id}/rename` · `GET /api/studio/backups/{id}/preview` · `POST /api/studio/backups/{id}/restore` · `/api/conditions` · `/api/weather/series` · `/api/health` ·
`/api/settings` (includes `integrations`: each value in effect and where it came from) · `/api/control/catalogue` · `/api/control/log` · `/api/schedule` · `/api/maintenance` ·
`/api/frigate/cameras` · `/api/frigate/snapshot/{camera}.jpg?height=` · `/api/followcam` · `/api/docs` (OpenAPI)

Imagery: `GET /api/imagery` · `GET /api/imagery/sources` · `GET /api/imagery/sources/{id}/preview.jpg` ·
`POST /api/imagery/probe {ids?}` · `PUT /api/imagery/source {mode: "auto"} | {provider} | {provider: "custom", custom}` ·
`DELETE /api/imagery/sources/{id}` (local only) · `PUT /api/imagery/key {provider, key}` · `POST /api/imagery/refresh` ·
`PUT /api/imagery/context {world}` · `GET /api/imagery/kinds` · `POST /api/imagery/auto` (the same as mode auto) ·
`POST /api/imagery/discover {url}` · `POST /api/imagery/try {entry}` · `POST /api/imagery/sources {entry, use?, replace?}` ·
`GET /api/imagery/outlines` · `POST /api/imagery/sources/{id}/extent` · `POST /api/imagery/sources/{id}/export {country, region, place, landmark, coverage, worldwide?}` ·
tiles: `/tiles/world/{z}/{x}/{y}.jpg`, `/tiles/src/{z}/{x}/{y}.jpg`

Panel settings: `GET /api/settings`, `GET /api/about`, `PUT /api/integrations/{robot|frigate|home_assistant|weather|panel}` (partial object, `null` hands a field back to the environment, applied live), `POST /api/integrations/{group}/test`, `PUT /api/settings/{key}` (imagery_alignment, conditions, control_enabled, map_write_enabled, gates,
yard_cameras, camera_polygons, presence_entity, ha_notify_service, webhook_url) · `PUT /api/schedule/windows` ·
`PUT /api/maintenance/items` · `POST /api/maintenance/{id}/done` · `POST /api/cloud/login` ·
`POST /api/cloud/logout` · `POST /api/notify/test`

Map Studio: `GET /api/studio` · `/api/studio/geojson?ref=` · `/api/studio/diff?a=&b=` ·
`/api/studio/export?ref=&fmt=geojson|kml|json` · `POST /api/studio/backups/refresh` · `POST /api/studio/backups/{id}/fetch`
(reads) · `GET|POST|DELETE /api/studio/draft` · `POST /api/studio/draft/edit {op: add|shape|rename|enable|delete}` ·
`POST /api/studio/draft/undo` · `POST /api/studio/reload` (a fresh `get_map`) · `GET /api/studio/captures[/{id}]` ·
`GET|PUT|DELETE /api/studio/overlay`. `ref` is `current`, `draft`, `version:{id}` or `backup:{id}`. Edits also take
`op: revert` and a `base` stamp (stale windows get 409).

Interlocks without sending: `POST /api/control/preview {cmd, args}`

Can command the robot: `POST /api/control/send {cmd, args, confirm: true, override}` ·
`POST /api/schedule/pending/confirm` (`/dismiss` only clears it) · `POST /api/studio/draft/apply {confirm: true, base}`
(map writes, see Map Studio)

