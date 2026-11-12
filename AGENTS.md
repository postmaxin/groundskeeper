# AGENTS.md: working on Groundskeeper

Instructions for AI coding agents, and a useful summary for people. Groundskeeper is a self-hosted recorder, map and
guarded control panel for Yarbo robot mowers: a FastAPI server, plain-JS front end and SQLite, in one container. It
controls a machine with blades and stores the location of someone's home. The **rules** below are not style
preferences; a change that breaks one is not merged. `CONTRIBUTING.md` has the human version of the workflow.

Folders have their own short `AGENTS.md` with local rules: `app/AGENTS.md`, `web/AGENTS.md`, `tests/AGENTS.md`,
`catalog/AGENTS.md`. Read the one for each folder you change.

## Definition of done

A change is ready for review when all of these are true:

1. `python scripts/check.py` prints **all checks passed**. It runs the tests, the guard tests, the imagery catalog
   checks, the privacy check and the front-end syntax check: the same as CI.
2. New behaviour has a test, written with synthetic data.
3. `CHANGELOG.md` has a line under **Unreleased** for anything a user would notice, and the matching `docs/user/`
   page says how it works now. A feature that `docs/user/not-yet.md` lists loses its line there.
4. If the UI changed visibly, `scripts/screenshots.py` was run and you looked at the result.
5. The PR template is filled in, including the safety checklist and whether an AI assistant wrote the change.

## Commands

```bash
pip install -r requirements.txt -r requirements-dev.txt       # Python 3.12
python scripts/check.py                                       # every check; --quick skips the full test suite
python -m pytest -q tests/test_auth.py                        # one test file
DEMO=1 PANEL_AUTH=off DATA_DIR=./demo-data python -m app      # run with a synthetic robot on :8425
```

The UI needs `web/vendor/` (MapLibre, three.js; git-ignored): see `CONTRIBUTING.md` → Set up. Tests don't need it.
The Agora Web SDK is vendored there too, for the robot cameras.

## Rules for AI agents

These come on top of the rules below, which apply to everyone.

- **Work from an issue.** If the issue is unclear or the change would break a rule, stop and ask in the issue. Don't
  guess what a maintainer wants.
- **One change per PR.** No drive-by refactors, renames, reformatting or "tidying" of code you didn't need to touch.
  A small diff is reviewed faster and is more likely to be merged.
- **Never invent robot behaviour.** Commands, payloads, field meanings, units and state codes come from a capture of
  the official app or from `docs/dev/`. If what you need isn't there, the answer is a "capture needed" issue, not a
  plausible guess.
- **Never weaken a check to make it pass.** Don't edit `tests/test_guards.py`, loosen an assertion, add a `skip`,
  or widen an allowlist unless the issue asks for exactly that. A failing guard test means the change is wrong.
- **Never touch a real robot.** Develop and test against the demo (`DEMO=1`) and the fakes in `tests/`. Don't run
  anything against a panel that is connected to a robot, and never send it a command.
- **Real data stays out.** Don't paste a real map, capture, serial, address, coordinates or log into code, tests,
  docs, commits or the PR, even when the person you work for gives it to you. Make a synthetic stand-in.
- **Leave the version alone.** A feature or fix PR adds its line under **Unreleased** in `CHANGELOG.md` and nothing
  more; the release commit bumps `app/version.py`. Releases bump the last number (0.19.0 → 0.19.1); the middle one
  only when a maintainer says so.
- **Review bots are advice.** CodeRabbit reviews each PR that is ready for review (`.coderabbit.yaml`). Check each of
  its comments against these rules and the code before acting; fix what is right, reply where it is wrong. A bot
  never overrides a rule here or a maintainer's decision.
- **Say what you didn't verify.** In the PR, list what you tested and what you couldn't (for example: "not tried on
  a robot", "UI not looked at on a phone").

## Map of the code

| Where | What |
|---|---|
| `app/main.py` | Every HTTP route, the sign-in middleware, startup (`lifespan`), `SETTINGS_DEFAULTS` |
| `app/auth.py` | Password, sessions, API tokens, signed links, the gate (`OPEN_PATHS`, `needs_auth`) |
| `app/recorder.py` | Listens to the robot through the Yarbo cloud, keeps state, records telemetry, detects runs, stall alert |
| `app/commands.py` | What may be sent: the read requests (`READ_ONLY_COMMANDS`, `COMPUTE_ONLY`) and the `cmd_vel` block |
| `app/link.py` | `RobotLink`: the one doorway to the robot, through the Yarbo cloud, for reads, commands and writes |
| `app/cloud.py`, `app/control.py` | Cloud transport; the controller, the only sender of run commands (`CATALOGUE`, `check`, `GATE_DEFAULTS`) |
| `app/ops.py`, `app/scheduler.py` | Scheduler, guards, notifications, maintenance: things that act over time |
| `app/runs.py`, `app/coverage.py`, `app/problems.py`, `app/insights.py`, `app/planned.py` | Run analysis |
| `app/mapstudio.py`, `app/mapwrite.py`, `app/areasettings.py`, `app/tidy.py`, `app/backups.py` | Map Studio: drafts, writes with read-back, settings |
| `app/why.py` | Every "can't run because…" reason |
| `app/runtime.py` | Integration settings that override the environment, applied live |
| `app/mcp_server.py` | MCP server for AI assistants (`ROUTES` allowlist, scrubbed output) |
| `app/imagery/`, `catalog/` | Aerial imagery: protocols, catalog data (one JSON file per source) |
| `app/demo.py`, `app/demo_robot.py` | Demo mode: synthetic history and a simulated robot |
| `web/` | Plain JS, no build step: `app.js` (shell, Live, Runs), `studio.js`, `settings.js`, `ops.js`, `imagery.js`, `help.js`, `robot3d.js` |
| `web/cameras.js` | Live's Cameras tile: the robot's own cameras |
| `docs/user/` | In-app Help pages (served by the panel) |
| `docs/dev/` | Design (`design.md`, cited from code as §N), protocol, map writes, robot states, UI rules |
| `tests/` | pytest; fakes for the robot, HTTP and stores; all synthetic. `tests/test_guards.py` checks the rules below |

## How to work

1. Read the module's docstring and the `docs/dev/design.md` section it cites before changing it. Robot behaviour
   that looks odd is usually documented there, or in `docs/dev/protocol.md` / `docs/dev/robot-states.md`. Check
   [Looks wrong, is deliberate](#looks-wrong-is-deliberate) before "fixing" something.
2. Write the test first, using synthetic data. `tests/` already has fakes for the recorder, store, HTTP and the
   robot's replies; reuse them.
3. Keep the change small and in the style of the code around it.
4. Finish the [definition of done](#definition-of-done).

## Recipes

Step lists for the common changes. Each ends with the definition of done.

**Read a new value from the robot.** Find it in a capture or `docs/dev/protocol.md`. If it needs a new request, add
the command to `READ_ONLY_COMMANDS` in `app/commands.py` (named `get_`/`read_`, or listed in `COMPUTE_ONLY`
with a comment saying why nothing on the robot changes) and describe its reply in `docs/dev/protocol.md`. Parse it in
`app/recorder.py` or `app/telemetry.py`, tolerating missing fields. Test with a fake reply.

**Send a new command to the robot** (a map write, a setting, a run command). Only from a capture of the official app;
link it in the PR. Document the payload and the capture in `docs/dev/map-writes.md` (writes) or
`docs/dev/protocol.md` (run commands) first. Writes go through `app/mapwrite.py` (`WRITE_COMMANDS`) or
`app/areasettings.py`, are serialised, read back, compared and put back on a mismatch. Run commands go in
`control.CATALOGUE` and through `control.check`, with any new interlock failing closed. Remove the line from
`docs/user/not-yet.md`. Expect a maintainer to test it on a real robot before merging.

**Add an API route.** Add it to `app/main.py`. Paths under `/api/` are locked behind sign-in automatically; never add
to `OPEN_PATHS`. A route that changes the password or tokens needs a person's session (`_person`). A new route that
writes is closed to API tokens unless it is added to `TOKEN_WRITES` (`app/auth.py`), so leave it closed. Test with
FastAPI's `TestClient`. Don't expose it to AI assistants (`app/mcp_server.py` `ROUTES`) unless the issue asks; any
MCP change gets its own review.

**Add a setting.** A panel setting gets a default in `SETTINGS_DEFAULTS` (`app/main.py`), off or the safest value.
An integration setting (an address, a token) goes through `app/runtime.py`. In the UI, add a row with `srow(name,
help, control)` inside the right `sec*` function of `web/settings.js`; don't make a new card. Document it in
`docs/user/settings.md`.

**Explain why the robot can't run.** Add a reason to `app/why.py` (`reason(code, title, fix, …)`) and use it where the
decision is made. Titles say what is wrong; `fix` says what the user can do.

**Add an imagery source.** Follow `catalog/README.md` and `catalog/AGENTS.md`: one JSON file, its coverage outline, then
`python -m app.imagery schema --write` and `table --write`.

**Change the UI.** Follow `web/AGENTS.md` and `docs/dev/ui-design.md`: tokens for colour, `h()` for DOM, short
labels with tooltips, works at phone width. Regenerate the screenshots.

## Rules

### Safety
- **No new robot command without a capture from the official app or SDK.** Never guess a command or a payload.
  Formats and where they came from go in `docs/dev/protocol.md` or `docs/dev/map-writes.md` first.
- `cmd_vel` (manual driving) stays blocked everywhere. A new read goes in `READ_ONLY_COMMANDS`
  (`app/commands.py`), and a request that only computes goes in `COMPUTE_ONLY` too.
- Only `app.control.Controller.send()` (run commands) and `app.mapwrite.MapWriter` (map and settings writes) send
  commands, and only through `app.link.RobotLink` (`command()` for run commands, `write_command()` for captured
  writes), which alone reaches `CloudTransport.publish`; reads go through its `read()`, read requests only. Every
  send is marked as the panel's own, so its echo is never taken for a person's. A command goes once; nothing repeats
  it. Interlocks live in `control.check`, are enforced on the server (never only in the UI), and new ones
  fail closed: an unknown state blocks.
- Never loosen a default. A new install has control, map writing, guard auto-resume, assistant commands and the
  MCP server all off.
- Every map or settings write is read back, compared, and put back on a mismatch (`app/mapwrite.py`,
  `app/areasettings.py`). Writes to the robot are serialised.
- Robot replies are matched by topic name only. Anything per-id must be serialised and must check the reply's
  `id`. Never trust `StateMSG.stuck` alone (`app/recorder.py` has the panel's own stall detection).
- Demo mode (`DEMO=1`) must never reach a real robot, the cloud, Home Assistant, Frigate or Tempest.

### Access
- Sign-in (`app/auth.py`) guards every path that returns data. New routes under `/api/`, `/tiles/` or `/imagery/`
  are locked automatically. Never add a path to `OPEN_PATHS` that returns data.
- Password and token changes need a person's session (`_person` in `app/main.py`), never an API token.
- API tokens read, and may ask for robot commands as actor `token` (the strict checks, no override). Every other write
  is refused by default (`TOKEN_WRITES` in `app/auth.py`).
- MCP tools stay inside the `ROUTES` allowlist, only ever tighten rules, and `scrub` their results.

### Privacy
- Never commit real robot data: captures, maps, serials, IP addresses, coordinates, or screenshots of a real yard.
  Fixtures and screenshots are synthetic; screenshots come from the demo.
- Never probe or fetch an imagery source whose coverage doesn't contain the yard. That would send the yard's
  location to it. Imagery URLs carry coordinates, so keep them out of logs.
- Examples use `192.0.2.x` addresses, `user@example.com` and the serial `YB0000DEMO0001`.

### Code and UI
- Python 3.12, standard library first. New lines stay at 120 characters or fewer; don't rewrap existing lines.
  Comments say why, as in the code around them.
- New dependencies need a reason in the PR. Nothing loads from a CDN at runtime; the Dockerfile vendors with
  checksums.
- Front end: plain JS, no framework or build step. Build DOM with `createElement`/`textContent` (the `h()`
  helper); never `innerHTML`.
- Colours only through the tokens in `web/style.css` (JS `tok()`, `MAP`, `SERIES`); never a new hard-coded hex. Map
  colours stay the same in both themes.
- Reasons the robot can't run go through `app/why.py`.
- UI copy: short labels; explanations go in tooltips or the Help pages, not paragraphs on screen.

`tests/test_guards.py` checks the rules a test can check: who may publish to the robot, `cmd_vel`, read-only
requests, defaults, documented commands, open paths, HTML injection, CDN loads, new hard-coded colours, and that
paths named in the `AGENTS.md` files exist.

## Looks wrong, is deliberate

Don't "fix" these; each is how the robot behaves or a choice made on purpose.

- **x is west, y is north** in the robot's local frame. Plot the robot from `CombinedOdom`, not RTK.
- **`get_map_buckup_from_id`** is the robot's own spelling.
- **Replies are matched by topic name, not request id**, because the robot's replies carry none. Two requests of
  the same command in flight get the same reply; that's why per-id reads are serialised.
- **`StateMSG.stuck` is ignored for alerts.** The robot reported 0 while it was stuck; the panel detects stalls itself.
- **Run commands get no reply.** `control` confirms them by watching the robot's state change (`CONFIRM`).
- **The robot reuses freed ids**, so history is never keyed on an id alone. Ids are global across kinds.
- **A record with id 0 in a reply means "never configured, these are defaults"**, not feature 0.
- **The robot's own map fails strict validation** (tiny self-crossings). Drafts are judged against it, not in
  absolute terms.
- **Robot backups hold a second copy of every pathway and sidewalk**; they are de-duplicated before comparing.
- **Telemetry is stamped on arrival.** After a network gap samples bunch up; speed, slip and distance skip bursts.
- **An ArcGIS `exportImage` with no coverage returns a black image with HTTP 200**, hence the blank detection.
- **`httpx` drops a URL's own query when `params=` is given**; use `merge_query` in `app/imagery/common.py`.
- **One route: the Yarbo cloud.** Firmware V3.14.4x closed the robot's own broker on the home network (#91), so
  reads, run commands and map writes all go through the cloud, as the official app's do (`docs/dev/protocol.md` →
  Access control). `app.link.RobotLink` (`read`, `command`, `write_command`) alone reaches `CloudTransport.publish`;
  there is no other route and no fallback.

## Glossary

| Term | Meaning |
|---|---|
| Area | A mowing area outline on the robot's map, with its own mow settings |
| Pathway / dead end | A drivable route between areas / a branch that ends; both have their own settings |
| No-go zone / no-vision zone | Where the robot must not drive / where it ignores its obstacle cameras |
| Plan | A list of areas the robot mows in one go (`start_plan`); a **plan session** is one start of it, `planId:startTime` |
| Run | The panel's record of one plan session, joined across pauses, charging breaks and panel restarts |
| Head | The tool on the robot (mower, snow blower, …); runs record the head type |
| RTK fixed / float / single | Centimetre / decimetre / metre-level positioning; problem areas follow weak positioning |
| HaLow | The long-range Wi-Fi link between the robot and its base station (the "data center") |
| Interlock | A check that must pass before a command is sent (`control.check`) |
| Guard | A rule that acts during a run (person seen → pause, rain → dock) |
| Draft | Map Studio's server-side copy of the map being edited, shared by everyone using the panel |
| Capture | A recording of the official app's commands, made with Map Studio → Record an app edit |
