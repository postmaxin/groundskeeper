# Changelog

What changed in each version of the panel, newest first. The version is in Settings → This panel and under the
robot card. Before 0.9.0 the panel had no version numbers; those changes are listed by date.

## Unreleased

### Added

- **A stats line under the Cameras tile's video** (#53), refreshed every 2 seconds while it plays, for example
  `640×480 · 15 fps · 1.2 Mbps · 2% loss · RTT 180 ms`. It comes from the video library's own counters in your browser
  and nothing is sent anywhere. It says **weak link** when loss is 5% or more, the picture froze for 10% of the time
  or the library rates the link poor, and **slow to draw** when this browser shows far fewer frames than arrive. The
  demo shows a made-up line. Sending samples to the panel to map weak spots is not part of this.
- **"Metrics only" API tokens** (Settings → Access, or `"scope": "metrics"` on `POST /api/auth/tokens`). Such a token
  can read `/api/metrics` and is refused (403) on every other locked path (`/api/`, `/tiles/`, `/imagery/`), so a
  scrape token that leaks gives away nothing about the yard. Existing tokens stay full, and the box is off by
  default. Metrics tokens are kept apart from the full ones, so rolling back to an older image cannot turn one into
  a full token. The metrics endpoint itself comes next.
- **A Cameras tile on Live** shows the robot's own cameras (front, left, right, back), one at a time, while the tile
  is open. It switches the cameras on (Control on, never off), plays the stream with Agora's SDK from this panel
  (the panel asks the SDK not to send usage statistics or upload logs, and switches off its remote configuration; a
  network capture to confirm this is pending, #35; Agora's servers see the panel's address) and leaves the channel
  when closed, on another page or a hidden tab. Shown
  only with Robot video on and the service address set; the demo shows a placeholder.
- **The Cameras tile keeps the robot awake and plays on a real robot** (#45). The robot publishes video only while
  someone sends the keep-awake the Yarbo app sends while open (`set_working_state {"state":1}`), so while the tile is
  open the browser asks `POST /api/video/keepalive` every 4 seconds and the panel sends one fixed beat per accepted
  request (people only; needs Robot video and Control on; at most one every 3 s for everyone together; never state 0;
  not in the control catalogue, so `/api/control/send` cannot send it). Events note when it starts and stops, not
  each beat. The panel's own keep-awake and camera-on are matched to their echoes one for one, so they are not
  taken for the phone app, while the real app's beats still are. This assumes the cloud echoes the panel's own
  commands back, as it did on one robot (#48): until echoes have been seen (and after three credits in a row go unmatched) the
  panel's keep-awake and camera-on count as possibly the phone app's while the cameras are open, so automatic starts
  stay blocked (the safe direction); an event says when that happens. The stream's 64-character secret is longer than
  Agora's Web SDK can wrap, so the tile now writes it into the join message itself, for as long as the client lives (so a reconnect works too)
  (a stopgap, #46: it travels unwrapped inside TLS to Agora, whose servers decrypt the stream for your browser anyway).
  Allow `*.agora.io` and `*.sd-rtn.com` in ad blockers. The Yarbo app switches the cameras off when it goes to the
  background, which stops the panel's video until the tile is reopened.
- **Cameras: known limitations in Help** (Live → Cameras). It has played the back camera during a run on one robot;
  waking a docked, sleeping robot, the front, left and right cameras, a phone and more than one viewer are not yet
  shown to work.
- **Cameras tile in the demo and the screenshots** (#23): the demo's Live page shows the tile as a placeholder (it
  loads nothing from Agora), the Live screenshots in `docs/screenshots/` are regenerated to include it, and Help
  (Live → Cameras) says what the demo shows.
- **Robot video setting** (Settings → Robot → Yarbo cloud, off by default): lets a signed-in person ask for a robot
  camera's join details (`POST /api/video/token`); the viewer's browser then connects to Agora. People only: API tokens
  and AI assistants are refused, and the stream key is never logged. Needs `YARBO_ROBOT_SERVICE_URL` (environment only); the Live tile
  that plays it is the Cameras tile.
- **The controller can switch the robot's cameras on** (`smart_vision_control {"state":1}`, #20): the first step of
  the camera view. It goes through the same interlocks as other run commands (control on, robot online, a route),
  is marked as the panel's own so it is not taken for the phone app, and nothing can send "off" (state 0 is
  refused). Only a signed-in person can send it (not an API token, an assistant, the scheduler or a guard), and
  only through the cloud. The Cameras tile on Live sends it when opened.
- **Installable with pip or uv** (`pyproject.toml`): another Python tool can depend on Groundskeeper's `app` package,
  and `uv sync` sets up a checkout. The Docker image and `requirements*.txt` are unchanged, and a test keeps the two
  dependency lists the same. Running the panel itself still needs a checkout or the container.
- **A Helm chart** (`deploy/helm/groundskeeper/`): deploy the panel on Kubernetes — a single-pod StatefulSet, a Service and a
  PersistentVolumeClaim for `/data`, with a documented `values.yaml`. Everything site-specific stays in the panel's
  Settings, and control is off on a new install as always.
- **Single sign-on with OpenID Connect** (Authentik, Keycloak, Authelia, Google, ...): "Sign in with …" on the sign-in
  page, set with `OIDC_*` in the environment and modelled on Grafana's generic OAuth sign-in. Only the allowed groups
  or email domains get in, and the panel refuses to start with single sign-on on and neither set. `PANEL_PASSWORD=off`
  turns the password off once it works. See Help → Settings → Signing in.

### Fixed

- **Cloud-only: a serial saved in Settings → Robot reaches the cloud connection at once** (#15). With no robot address
  set, the card kept saying "waiting for the robot's serial" until the panel restarted.
- **The Cameras tile says which step failed and why** (#38), for example "Joining failed: OperationError", instead of
  "Something went wrong.". It never shows the token, key, channel or App ID.
- **Keyboard focus stays put when Live refreshes** (#36, #37). Live redraws its cards every few seconds and the Cameras
  tile redraws on each change, which dropped focus to the page. Focus now stays on the same control (a camera chip,
  Close) or, when it goes away, moves to the one that replaces it (Open to Close, Close to Open).
- **The Agora Web SDK's licence is stated as MIT** (#40), as its npm package declares, not Apache-2.0. The MIT text
  now ships beside the SDK in the image (`web/vendor/THIRD_PARTY_NOTICES.md`).

## 0.19.6 — 2026-11-09

### Added

- **`GET /api/ha/summary` for a Home Assistant integration** (#12): one small read with the robot's state, battery,
  positioning, lawn, last run, the next schedule window and whether a start would go out now (with the reasons when
  not), checked as the caller, so a token gets the strict checks, at most every 30 s. It also says when the robot is
  stuck, waiting for its RTK fix or out of touch, and its last run is the last finished one. It carries no position,
  map or camera data, and no serial number: the robot is named by a hash of it. The
  [Home Assistant integration](https://github.com/Groundskeeper-robot/groundskeeper-ha) reads it.

### Changed

- **The robot in 3D looks like a Yarbo with the Lawn Mower Pro:** black shell on rubber tracks, yellow shoulders with
  the two antennas, the red stop button, a beacon in the status colour, and the mower in front with its yellow
  handles and headlights. The mower hangs on the core's hitch and lies on the ground under it, so over a crest or at
  the foot of a bank it tips a little differently from the core. Smooth edges, on a soft shadow, lit from the same
  side as the ground's shading, which now stays put when the map is turned.
- **The robot's own heights draw as smooth slopes** on the 3D ground instead of small steps.
- **The 3D view opens faster after a season of mowing:** the robot's heights are kept between looks and only new
  ones are read, and the surface is worked out once however many requests arrive together.

### Fixed

- **3D:** a single stray position no longer sends the robot model off and back (a real jump is taken once the next
  update agrees); Follow no longer freezes after a touch the browser takes over, or moves under a second finger;
  switching 3D off and on no longer builds a new model each time; and after a mow an open page draws the new ground
  when the run ends instead of a mix of old and new.

## 0.19.5 — 2026-11-07

### Changed

- **API tokens are limited to reading and asking for robot commands.** Settings, safety switches and the map now need
  a person signed in with the password; a program that changes them with a token gets a 403. Commands from a token
  need control on, face the strictest checks (anything unknown blocks a start or resume), cannot override and show as
  `token` in the log. Existing tokens keep working with these limits.

## 0.19.4 — 2026-11-03

### Changed
- **On a phone a connection that isn't fine says why** in the top bar (*Cloud: signed out*, *RTK no corrections*),
  since a tooltip never shows on touch. The row wraps if that's long.
- **Easier to tap on a phone:** the small folds (*Every check*, *History*, *Advanced*), *check again*, the track colour
  select, the replay slider and Map Studio's tolerance chip are all at least 32 px tall.
- **A phone on its side gets the stacked layout**, however wide it is, with the map at 70 % of the height so a swipe
  beside it still scrolls the page. On a tablet or a phone on its side the connections stay in view: one *Links ok*
  chip while all is well, a row of their own when one isn't.
- **The panel uses the whole screen on a phone with a notch**, with the top bar, nav and page kept clear of the notch
  and the home bar.

### Fixed
- **On a phone the map's credit stays folded** when the imagery loads late, instead of covering the legend.

## 0.19.3 — 2026-10-29

### Added
- **RTK correction gaps are named.** The RTK pill reads *no corrections* while the base station's corrections are
  more than 10 s old, and Robot → Health → Positioning lists each gap of 20 s or more with the fix it fell to and the
  HaLow signal, so a DGPS fix with plenty of satellites points at the base station rather than the sky. Each gap is
  an event when it ends.
- **On a phone, Live's map layout starts with Pause, Stop and Return to dock while a run is going**: the emergency
  buttons the camera layout already leads with. Before, stopping a mow there took a scroll of more than a screen.
- **Schedules saved in the Yarbo app are shown** on the Schedule page (and noted in Events): they live on the robot
  and start mows without the panel's run rules, so the page says so (#64). The panel still never writes them.

### Changed
- **The two people rules read as two rules** (Settings → Run rules): *Yard must be clear to start* and the *Person
  guard* (during a run), each saying what the other does, with a note when only one of them is on. A guard's alert
  in the top bar and its notice name the rule that acted (#54).
- **No start or resume while a write to the robot is in progress** (a map, settings or Wi-Fi change): a new hard
  check, for every sender. Pause, stop and return to dock are never held back (#57).
- **No write to the robot right after a start or resume from the panel**, until the robot shows the plan running
  (up to 3 min if it never does). Every write also asks again, just before its first command, that no plan is
  running: Map Studio apply and a backup restore read for many seconds after their checks. A write is refused before
  anything is sent, never half-way. Map Studio, the settings editors and the Network card show the reason (#69).
- **On a phone the top bar shows the connections on a row of their own** (Cloud, Broker, HaLow, RTK), instead of
  scrolling out of sight behind the robot pill, and keeps the same layout on every page (#52).
- **On a phone the connections fold into one *Links ok* chip while all of them are fine**, beside the theme button,
  so the top bar is two rows instead of three (about 101 px instead of 143). Any connection that is not fine brings
  them all back, spelled out on their own row.
- **Bigger tap targets on a phone:** the top bar's chips and tabs, the **?** help link, Settings' section list, Map
  Studio's shape rows and the map's zoom buttons; a tap on an ⓘ shows its explanation (#52).
- **On a phone, Live's Control card says why a button is off** in a line under the buttons ("Start: a run is
  already going."), since a touch screen shows no tooltips.
- **The map tools fit on a phone:** Imagery, Zones and Track stay in the row over the map and the other tools
  (Labels, Cameras, Problem areas, Signal, 3D, Colour, Fit yard) are under **More**, instead of a row that scrolled
  sideways with half the tools off the edge. Wider screens are unchanged.
- **Settings on a phone starts with the section, not the list:** the 13 section links fold into one *Sections*
  row naming the open section; it is open on Settings itself and closes once you pick one. Wider screens keep the
  list on the left.

### Fixed
- **Your resume now overrides the person guard** for that detection, also when the phone app was open: the guard
  had been pausing the robot again within seconds after each resume (#53).
- **A mow paused overnight no longer counts the night:** a run's length and the Runs overview's pace use its
  working time (not paused or charging mid-plan); a run page shows the start-to-end time and the break underneath
  (#62). Stored runs are worked out again once after the update.
- **A run link opened as the first page shows the run** instead of an empty Runs page (#52).
- **On a phone:** fields no longer zoom the page in when tapped on an iPhone, Map Studio's tool bar no longer
  covers the zoom-out button, and long messages and dialogs fit the screen (#52).

## 0.19.2 — 2026-10-28

### Added
- **A Network card** (Robot → Health): HaLow strength with the robot's own weak and 4G-fallback levels, the 4G
  module's values as the robot reports them, and the Wi-Fi it is on with its signal. **Scan** lists nearby networks
  with their signal; **Join** and **Forget** use the phone app's own commands and are confirmed by asking the robot
  afterwards. Join and Forget need map writing on and the robot on its charger with no plan; the password goes to
  the robot only and is never kept or logged. Open networks can't be joined yet (not captured).
- **Signal bars on the top bar:** HaLow, Wi-Fi (when the robot is on it) and GPS (from the fix type) in the
  connections pill.

## 0.19.1 — 2026-10-26

### Added
- **An Unraid Docker template** (`deploy/unraid/groundskeeper.xml`): a container made from it shows in Unraid's
  Docker tab like any other app, with the update check, autostart and Edit, instead of "3rd Party".
- **A heading arrow on the robot dot** (Live map): an arrowhead inside the dot points where the robot is heading,
  and stays true when the map is rotated.

### Changed
- **The 3D robot and Follow use every position the robot sends** (about one a second while the phone app is open)
  instead of only the status every 1.5 s, and take them in the robot's own time order: after a network dropout,
  the late positions in the robot's backlog are skipped instead of sending the 3D robot back on a detour. The 2D
  robot dot still moves with the status.

## 0.19.0 — 2026-10-21

### Changed
- **Local first, the cloud as a fallback.** Commands (start, pause, resume, stop, return to dock) now go to the
  robot's own broker on your network, so they work with no Yarbo account and take effect in about 2 seconds. Reads
  and commands fall back to the Yarbo cloud when the robot can't be reached on your network (a 4G spell where HaLow
  is weak), or after two quick reads or two commands in a row go unanswered there, if you are signed in. A pause,
  stop or return to dock not seen within 10 s is sent once more by the other route (unless a newer command was sent
  meanwhile or control was switched off); a start or resume never is. Settings → Robot shows which route reads
  and commands use and lets you pin one; every change of route is logged in Events.
- **The Yarbo cloud sign-in is optional** for commands and reads. Map, settings and plan writes still go only through
  the cloud until a local write has been proved.
- **The phone app is also seen on your network:** the robot streams its status about once a second while the app
  is open, which the panel now reads as "app open" without the cloud. Without the cloud a missing stream is not yet
  taken as "closed": scheduled, assistant and guard starts wait while the panel cannot tell; a start from the panel
  shows the unknown.
- **Confirming a command is a pop-up** over the page, instead of a panel under the buttons that could open out of
  sight (on a phone, or in the camera layout). It says up front whether the command would go and why not; Send is
  offered only when it would.
- **The two-tap buttons** (camera layout) turn fully amber when armed, say "Tap again to …", and show a bar running
  down the 4 seconds; a line under them says which button is armed.

### Fixed
- The Control and camera-layout buttons were greyed out whenever the Yarbo cloud was not connected, even with the
  robot reachable on your network; they now follow the route to the robot.

## 0.18.0 — 2026-10-20

### Added
- **After-dark guard:** a plan that stopped to charge heads back out by itself when the battery is full, at any
  hour. If that happens between sunset and sunrise at your yard, the panel now sends it home (Return to dock, the
  default; or notify only, or ignore, in Settings → Run rules), not when you sent it out yourself. On a real mow,
  Return to dock ended the plan; the next schedule window starts it again. Like every guard it only sends a command
  while control is on.

### Changed
- **The AI assistant server runs on MCP's 2.x library** (`mcp` 2.3). Nothing changes in what an assistant can do or
  see; the server now reports the panel's version and speaks the newer protocol revisions. Existing tokens and
  connect snippets keep working.

## 0.17.0 — 2026-10-20

### Added
- **Find the robot on your network:** Settings → Robot (and the setup guide) has **Find it on my network**, which
  looks on the panel's own network for a Yarbo announcing itself and offers **Use this**. It only listens; nothing
  is sent to anything it finds. In Docker it needs host networking, like the Tempest.
- **Align** next to the imagery credit on Live's map lines the aerial photo up with the robot's map. Aligning from Settings or the
  setup guide now returns there when you save or cancel.

### Changed
- **The setup guide updates by itself** while something is on its way (the robot connecting, its serial, the cloud
  signing in, the yard imagery, the first Tempest reading), without overwriting anything you have typed.
- **Robot step:** type the address and press **Connect** (checked, then saved) instead of Test and Save; the serial
  is read from the robot and shown, and the port and a typed serial moved under Advanced.
- **Imagery step** in two parts, Pick the photo and Line it up, and says the yard's location comes from the robot's
  map.
- **Safety step** has the switches themselves (control, map writing, yard must be clear, resume after a guard
  pause) instead of only showing them.
- **AI assistant** moved to the last step of the guide and after Notifications in Settings.
- Lawn conditions explains what to check when no Tempest is heard.

### Fixed
- The version under the nav no longer overflows: a full commit id is shortened to 7 characters, and long text is
  cut off with "…".

## 0.16.0 — 2026-10-20

### Added
- **Container images for every release** on GitHub's registry (`ghcr.io/groundskeeper-robot/groundskeeper`), for amd64
  and arm64 (Raspberry Pi), with a ready `docker-compose.yml` and `.env.example`.
- **Demo mode** (`DEMO=1`): a made-up robot mows a made-up yard (on a public park's lawn, with real aerial imagery
  under it) with a week of history, so the panel can be tried without a Yarbo; it never contacts a robot, the Yarbo
  cloud or any integration. `DEMO_SEED=1` now means the same. `scripts/screenshots.py` makes the README screenshots
  from it.
- **Sign-in.** The panel now asks for a password before it shows anything; only `/health` and the sign-in page are
  open. On the first visit you set the password with a one-time setup code from the panel's log
  (`docker logs ...`), so nobody else on the network can claim it first. Settings → **Access** changes the
  password, signs out this or every browser, and makes named **API tokens** for programs (`Authorization: Bearer`).
  Forgot it: `python -m app.auth reset` in the container. `PANEL_AUTH=off` turns sign-in off for a panel behind a
  reverse proxy that signs people in itself. AI assistants keep their own token. **Upgrading:** after this update
  the panel asks for the setup code from its log once.
- `/health` says whether the robot is mid-run (`busy`), so the deploy script's "not while mowing" check works
  without signing in.
- **Dead ends in Map Studio:** draw one (+ Dead end, starting inside an area), reshape, rename or delete it and send
  it to the robot like a pathway; select one for its settings (speed, blades, trimmer), read back and checked. A
  replaced dead end keeps its settings. From the app's own commands, recorded 2026-10-11.

### Changed
- **The container no longer runs as root.** It runs as `PUID:PGID` (1000:1000 by default; the Unraid example uses
  99:100) and hands an existing data folder to that user on its first start. The libraries it downloads at build
  time are checked against their hashes, and the image has its own health check.
- A pathway's transit offset range goes to ±39.4 in (1.0 m), the Yarbo app's own limit (it was held at 27.6 in
  until the limit was known).

### Removed
- **The Terrain map tool.** The 3D map shows the ground instead, with the robot's own heights (the flat height and
  slope image no longer lined up with the photo).

### Fixed
- **Planned vs driven compares the whole plan.** The robot reports each area's plan as separate passes (the edge
  around the outline and the fill stripes), and the panel kept only the first of them, so a run could be compared
  with a fifth of its plan and look mostly off plan. Every pass is kept now, each from where the run took it up
  (a resumed plan leaves out what the robot had already driven, and passes it lists as finished), and the passes kept
  with earlier runs of the same plan fill in what a run is missing. Runs recorded before this are worked out from the robot's whole plan report kept with them.
- **A plan resumed after a long pause stays one run.** A run paused for more than 3 hours closes, and its resume
  was only joined back within 3 hours of the run's last movement, which such a run can never meet, so a plan
  paused in the evening and resumed the next morning became two runs. A resume of the same plan session now joins
  up to 36 hours later (never on a report that the plan is done: a new run's first report can be the last plan's). Runs split before this stay apart; Join with previous joins them.
- **Coverage of a resumed run counts the earlier runs of its plan.** What they mowed is neither a gap nor part of
  this run's share (it used to show as gaps "next to RTK loss"); the run page says how much
  that was.
- **No stuck alert while the robot waits for an RTK fix.** Standing still on a float fix with the blades stopped
  is the robot waiting for its fix: the alert now says "Yarbo is waiting for an RTK fix" (and Live shows it), and the
  stuck alert comes only if it still does not move 3 minutes after the fix is back.
- **One odd firmware reading is not a firmware change.** A new version counts once the robot has reported it for a
  minute; a chassis version that flickered for a few seconds was logged as two firmware changes.
- **Planned vs driven uses the robot's own plan** when it was kept during the run: the robot reports each area's
  path once it starts the area, and the panel now keeps it (only the run's first report was kept before, which has
  no path yet). Runs without it are compared with a fresh preview, as before, and the result says which.
- **A Wi-Fi survey cut short by a restart** that never rejoined its run still gets its report in Events.
- **Run distances are right, and survive panel restarts.** A run's distance, points, RTK share and battery are now
  worked out from its stored track when it ends, is joined or is closed after a restart. Before, the running totals
  started again at every restart, and they counted positions that arrived out of place after a weak-signal gap as
  driving. Every stored run is worked out again on upgrade; on a test mow the result came within about 4 % of the
  wheel odometers.
- **Positions in the robot's own order.** The panel now stores the robot's own time for each position. Distance and
  coverage use that order, a position that is out of place and back is not counted as driving, and "hard going"
  times wheel travel by it, so a burst of messages after a weak-signal gap no longer looks like jumps.
- **Wi-Fi survey across restarts:** its counters are kept with the run, so the verdict covers the whole mow.
- **The dock is no longer a weak-positioning spot** in Problem areas (time on the charger is left out).
- **A plan is not marked finished too early** when the robot briefly lists an area as done while still mowing it.
- **Leaving a charging break no longer ends the run** for the few seconds the robot shows no plan.
- **Late confirmations:** a command marked unconfirmed is still watched for 3 minutes; if the robot carries it out
  late, the log says "confirmed late". Nothing is sent again.
- **Guard notices are not repeated after a restart** for the same detection (the protective pause is unchanged).
- **Changes to run rules and guards are logged** in Events (old → new, and whether the panel or an assistant).
- **Home Assistant:** a value that is not known yet is sent as "unknown", and one refused sensor no longer stops the
  others from updating.
- **Secrets in the robot's replies** are masked when kept, and stored copies are masked when the panel starts.
- **Claude Desktop setup fixed.** Settings → AI assistant (and the setup guide) now give a Claude Desktop entry that
  runs the `mcp-remote` bridge on your computer (needs Node.js). The old entry put an http server straight into
  Claude Desktop's config, which only starts local programs. The page also says not to use Add custom connector: it
  needs https and connects from Anthropic's cloud, which cannot reach a home network.
- **Secrets from other apps' commands are no longer kept.** The panel logs and captures commands other clients send
  the robot (to learn their formats); the Yarbo app's "join this Wi-Fi" command carried the Wi-Fi password, which ended
  up in Events and Captured writes. Passwords, tokens and email addresses are now masked before anything is stored,
  and copies already stored are masked when the panel starts.

### Added
- **Follow the robot in 3D.** With the 3D map on, Follow flies the camera behind the robot and 45° to one side and
  keeps it there, turning with the robot (at most 40° a second, so the turn at the end of a stripe is a swing, not
  a spin). Right-drag (two fingers on a phone) moves the camera round the robot and it keeps that angle; zoom and
  tilt stay as you set them; a drag or Fit yard lets go. Works on Live and in a run's replay.
- **The robot in 3D.** With the 3D map on, the robot is a small 3D model that faces its heading, leans with its own
  pitch and roll, and glides between the panel's updates (it keeps going at its speed for a moment, then catches
  up) instead of jumping. In Runs it drives the replay. The status now carries the robot's compass heading.
- **Replay draws the track as it goes.** Press Play and the run's track grows behind the replay point (the slider
  shows it up to where you drag); before Play the whole run shows. Play is now a green button with a play icon.
- **`GET /api/runs/{id}/plan-feedback`:** the robot's plan reply kept for a run, by shape (or `?raw=true`), for
  comparing a run with the robot's own plan.
- **3D map.** The new 3D button among the map tools tilts the map and raises the ground under it: the robot's
  own RTK heights over the lawns, the 2015 lidar around them, eased together so there is no step where they meet.
  The photo, zones, track, run replay and the robot all lie on it, on Live and in Runs. The menu next to it (1× to 3×)
  draws the ground taller; right-drag (two fingers on a phone) turns and tilts. Map Studio and the camera
  layout's small map stay flat.
- **Notifications: pick the Home Assistant notify service from a list** of the ones your Home Assistant has, the
  companion app's phones first, instead of typing its name. "Type a name…" is still there for anything missing.
- **Map Studio: move a group of corners in small steps.** Shift-drag a box over a selected shape's corners (or
  Shift-click them), then move them north, south, east or west with the arrows or the arrow keys by a step you set
  (0.5 to 100 cm). A burst of moves is one draft edit, so one Undo takes it back.
- **Wi-Fi survey** (Settings → Robot: Off / Next run only / Every run). While a run is driving, the panel asks the
  robot every 30 s which Wi-Fi networks it hears and stores each one's signal where the robot was. Each ask is a ~4 s
  scan on the robot, so **Next run only** surveys one run (at least 10 minutes of driving), reports in Events
  (answers, reply time, pauses in the robot's messages during a scan and otherwise) and switches itself off.
- **Signal → Wi-Fi (survey):** one network's signal across the yard in 4 m cells, with a list of the networks heard
  (a saved one first, else the one heard best).
- **Imagery for every US state and DC:** 91 new free sources, statewide where a state publishes one (18 states and
  DC now have it at 15 cm or sharper) and the largest counties and cities elsewhere (2.5–15 cm). Alaska and Hawaii get imagery for
  the first time (30 cm statewide). Browse the catalog lists each country's sources under a heading per state,
  statewide first; searching a state by name or code (Texas, TX) shows everything that serves it, national and
  worldwide sources included. On a phone the list is tall enough to read.
- **No-vision zones can be sent to the robot** from Map Studio: switched on or off, reshaped and renamed, in place
  (same id), read back and undone on a mismatch. Switching one on, growing it or moving it (more ground where the
  robot ignores its cameras) asks for an extra OK. A zone's obstacle bypass is shown with the area's obstacle
  avoidance labels (Moderate bypass). Adding or deleting one still happens in the app.
- **Pathway mow settings: Blades on and the transit path offset** (on/off, range and step, in inches like an area's)
  can be changed. Any offset change is checked as a whole: the range must stay within 27.6 in each side (the widest
  the app has been seen to send) and the step within 3.9-19.7 in, until the app's own limits are known.

### Changed
- **The panel is now Groundskeeper**, with the new logo: a GK monogram over a strip of lawn, the robot as a dot
  in the grass. It shows in the nav, browser tabs, the favicon and a home-screen icon (added to a phone's home
  screen it opens as Groundskeeper). Home Assistant entity ids (`sensor.yarbo_panel_*`) and the install stay as
  they were; only the link sensor's display name changes. New MCP snippets name the server `groundskeeper`;
  existing assistant configs keep working.
- **Live follow-cam is live video:** Frigate's live stream at 5 frames a second, under a second behind, instead of
  a still every 3 seconds; in the Camera layout and the Follow-cam card. It stops while the tab is hidden or you are
  on another page, and falls back to stills if the stream fails.
- **Problem areas:** a weak-positioning spot names a weak HaLow link (−77 dBm or lower) or late corrections when
  there was one, so a link problem can be told from a poor sky view. The spot's details show its HaLow average.
- **Top bar:** the battery sits at the far right with an icon that fills with the charge, red when low through amber
  to green when full; it stays in view on a phone. The local broker chip sits beside the cloud chip.
- **Map imagery:** Browse the catalog and Fetch again are green buttons, easier to spot.
- **Live → Camera layout:** the small map sits right under the emergency buttons, above the robot's state and
  Control, instead of at the end of the panel.
- **Top bar, regrouped:** problems first, tinted in their colour; the switches (Control, Map writing) as one pill;
  the lawn; then at the right the connections as one pill (Cloud, Broker, RTK, shortened to the word and its dot on
  narrower screens) and the robot's state with its battery at the far right, which keeps its row. On a phone the bar
  is two rows: the title, layout switch and theme on top, then the scrolling chips with the state and battery always
  in view at the end.
- **The name reads "Groundskeeper for Yarbo"** in the nav.
- **Live → Camera layout: the small map always shows the whole yard.** It refits when its card changes size (before,
  it could open zoomed in, or stay wherever it had been left) and cannot be panned or zoomed; Map layout gives the
  full map.
- **Live → Camera layout follows the robot:** when no camera's coverage holds it, the big slot shows the map again,
  and the camera comes back when the robot drives into view (each change waits 10 s). A pinned camera stays up.

## 0.15.1 — 2026-10-10

### Fixed
- The Home Assistant entity lists (presence, and the Lawn-conditions inputs) fill in as soon as Home Assistant is
  connected: saving the connection reads them again, and a failed read retries every 15 s while the page is open.
  Before, a failed read stuck for a minute and the page needed a reload. Its error is in words, like the Test
  button's.

## 0.15.0 — 2026-10-10

### Added
- **Light / dark switch** in the top-right corner of every page. A click picks light or dark for this browser
  and stops following the device; Settings → This panel → Theme → System follows the device again.
- **Settings → Set up**, a guide through the first run: robot, Yarbo cloud, AI assistant, imagery, cameras, Home
  Assistant, weather, safety and notifications, in order. Each step uses the same controls as its own section and is
  ticked when the panel already has what it needs. It opens by itself on a fresh install (no robot address or no
  map yet). The Safety step has "Use the recommended": control off, yard must be clear on, no resume after a guard
  pause. Help → Setting up lists the same steps. MCP has a matching `setup_status` tool.
- **Live → Camera layout** (Map / Camera beside the title; each browser remembers its choice): the follow-cam takes
  the map's place, the map becomes a small card, and the panel starts with a big red **Pause** (**Resume** while
  paused) plus Stop and Return to dock. Each needs a second tap within 4 seconds. Same checks as the Control card.
  On a phone: the picture, the robot's state, then the red button.
- **Lawn conditions: Home Assistant sensors from a list.** Each input lists the HA entities that fit it, matched by
  device class with what each reads now, likely ones first; "Show all sensors" and "Type an id…" cover the rest.
  New kinds can be read: rain in mm/d, in/d or cm, a weather entity (its temperature and humidity, and rainy or
  not), and an on/off moisture sensor for rain or soil.
- **Presence from a list** (Settings → Home Assistant): alarm panels, `zone.home` (people in it), groups of people,
  people and trackers. Persons, trackers, groups and `zone.home` now work as presence, not only alarm panels.

### Changed
- The Home Assistant address starts filled in with HA's usual `http://homeassistant.local:8123`.
- **Test** and the HA status line say what went wrong in words: a name the server cannot find (in Docker, `.local`
  names often do not resolve: use the IP address), no answer in time, or a refused connection. Before, a timeout
  showed nothing at all.

### Fixed
- An on/off rain sensor (or a soil sensor) that had not changed for 15 minutes was read as "no update from the
  sensor"; HA's `last_reported` is used now, and on/off sensors hold their state.

## 0.14.0 — 2026-10-10

### Changed
- **Phones:** the nav is a bar at the foot of the screen (six icons with their names, always in reach), and the
  title bar stays at the top while you scroll. A new page opens at its top. Settings shows every section at once and
  puts each control under its name (two rows used to run off the screen). The runs table fits without scrolling
  sideways. "Show" on a problem spot, gap or off-plan stretch brings the map back into view. Switches in the bar
  show on/off by the toggle alone. Bigger checkboxes.
- **Map imagery:** the catalog is its own section ("The whole catalog": how many sources and countries, how many are
  free at 15 cm or sharper) with a Browse button, and a "Browse the catalog" button at the top of the page.
- Camera coverage on the map is a thin white dotted outline, no longer the same blue dashes as no-vision zones.

## 0.13.0 — 2026-10-10

### Added
- **Settings → AI assistant**, its own section right after Robot and flagged as the optional, recommended next
  step: create a token, copy a ready-made snippet for Claude Code or Claude Desktop (or the address and header for
  any MCP client), and three questions to try first. The token and snippets used to sit under This panel.
- **How sharp is sharp?** in Settings → Map imagery and the help: the cm-per-pixel number on every imagery card
  now carries a verdict (very sharp, sharp, good, coarse) and a scale of what each step shows (7–10 cm: pavers and
  sprinkler heads; 30 cm: soft edges; 60 cm: a blur), with where sharper imagery is usually found.
- **MCP:** the server instructions and the imagery tools now spell out how to find the most detailed map of the
  yard (compare native_m in cm, look at samples, search the region for a county or state service, hand the address
  back), so an assistant does it the same way every time.

## 0.12.0 — 2026-10-10

### Changed
- **Navigation:** six items. Health, Maintenance and Events are tabs under **Robot**; Help is the **?** by each title
  and a link at the foot of the nav. Old addresses still work.
- **Top bar:** Control is a switch (and Map writing, in Map Studio): off in one click, on after typing ENABLE. The
  status chips open where you can act on them.
- **One palette:** Live and Map Studio now draw shapes in the same colours, and a shape never shares a colour with a
  measurement: mowing areas white, no-go zones red hatched, no-vision zones blue dashed, the fence yellow; the
  single-point track is red-orange (weak positioning, not an error); gaps and off-plan stretches pink. Neutral data
  (satellites, voltages, temperatures) is no longer drawn in good/bad colours.
- **Charts:** a value scale with gridlines and a hover or touch readout; the latest value in each chart's header.
- A run that ended back on the dock without the plan's progress being read is shown as information, not a warning.

### Added
- **Light theme**, following the device or Settings → This panel → Theme.

## 0.11.0 — 2026-10-09

### Added
- **Why can't it run?** Whenever the robot cannot start or carry on, the panel says why and what fixes it, in the
  same words on Live, Schedule, a run's page and in notifications: each failed start check, no plans on the robot,
  the wrong head, a skipped schedule window (now also a notification), the robot pausing itself (bumper, e-stop,
  rain, fault: an alert once per pause), and a run that ended before its plan finished.
- **Maintenance starts at Yarbo's advice:** blades every 3 months, tracks greased every two weeks (new item),
  battery reviewed at 500 cycles, a season check at each head change; each item links Yarbo's own page. Intervals
  you set yourself are kept.
- **Weather from Home Assistant:** rain, temperature and humidity sensors in HA can stand in for the Tempest or
  back it up, chosen per input. **Soil moisture** sensors are a fifth lawn rule (Damp / Wet above levels you set).
- **Tempest station pick** when the hub hears more than one, **units** (mm · °C or in · °F) for Live and the rules,
  and the last 24 hours of temperature, dew point, rain and soil moisture under Settings → Lawn conditions.

## 0.10.0 — 2026-10-09

### Added
- **AI assistants (MCP):** connect Claude or another MCP client to the panel (Settings → This panel → AI
  assistants). It can read everything, help set the panel up (it asks before each change; nothing that loosens
  safety is offered), and send commands only
  when you switch on **Assistant commands** in Settings → Safety. It never sees coordinates. Help → AI assistants.
- **18 more imagery sources:** eight US states (NY, PA, OH, IN, NC, VA, CT, MA), six German states, Portugal,
  Ontario, Estonia and Emilia-Romagna. Tiles labelled as generic binary (MassGIS) are recognised as images.
- **Finding imagery for a new place:** a guide in Help, and `python -m app.imagery covers LAT LON` to see which
  sources claim a place.

## 0.9.0 — 2026-10-09

### Added
- **Version numbers.** The panel says which version and build it is (Settings → This panel, under the robot card,
  `/api/about`, `/health`).
- **Help** in the panel: a page per view, How do I…, Safety, Integrations, and this changelog. The **?** next to
  each page's title opens its help.
- **Not capable of yet:** what the panel cannot do today, and what would unlock each item.

## 2026-10-09
- **Planned vs driven** on a run's page: the robot's own route preview against the track. Off-plan stretches are
  pink and listed as across the stripes or away from the plan.
- Robot-measured terrain is cut to the mowing areas.
- Tempest: a repeated rain-start broadcast counts as one rain start.

## 2026-10-08
- **Terrain:** USGS lidar height and slope with contours, and a surface measured from the robot's own RTK heights
  (recorded from this day on).
- **Area mow settings** laid out like the Yarbo app, with the app's names; matched settings are editable, each save
  read back. Route preview on the map.
- Pathways drawn as roads you can tell apart. Open pages reload themselves after an update.
- **UI once-over:** Runs takes in Insights; one track colour control and one legend; one full-width Events log
  (with the control log); raw data moved to Settings; Live is "now"; phone header and nav.

## 2026-10-07
- **Robot backups:** save on request, delete, restore (Map Studio → History). Area writes keep local copies.
- Map Studio: reshape and rename an area in place; **Record an app edit** to learn commands the panel has not seen.

## 2026-10-06
- **Problem areas:** where the robot really has trouble (stuck, errors, steep ground, weak positioning, hard
  going), on the map and per run.
- **Insights:** how the mowing goes over time (now the Runs overview).
- Every robot state code is read; alerts on the robot's own stuck flag and errors.
- Signal heatmap, edge-pass track colouring, partial map writes.
- **Pathway mow settings** editor in Map Studio.

## 2026-10-05
- **Plan-aware runs:** one run per plan session, with a real paused state; join a run with the previous one.
- Guard resume (opt-in), Ignore on every guard, "Yard must be clear", commands confirmed by the robot's state.
- A live track that keeps up, a robot you can spot, and a stuck alert that does not rely on the robot's own flag.

## 2026-10-04
- **Settings redesign:** one section at a time; integrations set in the panel and applied live.
- **Map Studio:** tidy tools and drawing aids; side panel tabs; rename every shape in one list.
- **Map imagery:** add a source (discover, try at the yard, save, export for sharing).

## 2026-10-03
- **Map imagery:** a catalog of national and regional aerial sources with coverage outlines, a picker with yard
  samples and automatic mode, align by points, and a worldwide layer when zoomed out.
- Map history: a map put back as it was becomes current again.

## 2026-10-01
- **Map writes** for no-go zones and pathways, from the captured app protocol.
- **Map Studio:** inspect, measure, history with the robot's backups, diffs, drafts, editing on the map.
- **Phase 2:** guarded control, scheduler, Frigate follow-cam, alerts, maintenance.
- **Phase 1b:** run analytics, gap detection, lawn conditions, Health, imagery alignment.
- **First version:** a read-only recorder and map for a Yarbo robot.
