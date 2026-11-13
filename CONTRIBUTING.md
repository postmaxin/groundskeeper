# Contributing to Groundskeeper

Thanks for helping. Groundskeeper is a small project with one hard constraint: it drives a machine with blades and
holds the location of someone's home. Read [AGENTS.md](AGENTS.md) before you change code. It is short, and a PR
that breaks one of its rules can't be merged, whether a person or an AI assistant wrote it.

## Ways to help

- **Captures from the official app.** Most of what the panel can't do yet is waiting on someone to record the Yarbo
  app making that change. See [Recording an app edit](#recording-an-app-edit) and the issues labelled
  `capture needed`.
- **Imagery sources.** A sharper free aerial map for your county or country helps everyone there. See
  [catalog/README.md](catalog/README.md).
- **Bug reports**, especially from robots, heads or firmware versions other than the ones the panel was built on.
- **Code and docs.** Look for issues labelled `good first issue` or `help wanted`. If none are open, ask in
  [Discussions](https://github.com/Groundskeeper-robot/groundskeeper/discussions) what would help.

## Set up

You need Python 3.12 and Git. Node.js is optional and only used to syntax-check the front end.

```bash
git clone https://github.com/Groundskeeper-robot/groundskeeper && cd groundskeeper
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt          # Windows: .venv\Scripts\pip
mkdir -p web/vendor
curl -sSfL -o web/vendor/maplibre-gl.js  https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js
curl -sSfL -o web/vendor/maplibre-gl.css https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css
curl -sSfL -o web/vendor/three.module.min.js https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.min.js
curl -sSfL -o web/vendor/AgoraRTC_N-production.js \
  https://cdn.jsdelivr.net/npm/agora-rtc-sdk-ng@4.24.9/AgoraRTC_N-production.js
```

### Run it without a robot

Demo mode runs a made-up robot mowing a made-up yard, through the same recorder, map and run code as a real one:

```bash
DEMO=1 PANEL_AUTH=off DATA_DIR=./demo-data .venv/bin/python -m app     # http://127.0.0.1:8425
```

On Windows the commands on this page work as they are in WSL. With Windows Python (Git Bash or PowerShell) the
virtual environment's programs are in `.venv/Scripts`, not `.venv/bin`. In PowerShell, this runs the demo and then
clears the variables, so a later run in the same window doesn't start with sign-in off:

```powershell
$env:DEMO = "1"; $env:PANEL_AUTH = "off"; $env:DATA_DIR = "./demo-data"
try { .venv\Scripts\python -m app } finally { Remove-Item Env:DEMO, Env:PANEL_AUTH, Env:DATA_DIR }
```

`DEMO_SPEED=10` makes it mow faster. Use a separate `DATA_DIR` for the demo: it refuses to start on a folder that
holds a real robot's data.

### Run it against your own robot

```bash
DATA_DIR=./data .venv/bin/python -m app
```

Then open Settings → Robot and sign in to the Yarbo cloud. `./data` holds your yard's location and your cloud
session. It is git-ignored; keep it that way.

## Checks

Run this before you open a PR. CI runs the same command:

```bash
.venv/bin/python scripts/check.py            # tests, guard tests, imagery catalog, privacy check, JS syntax
.venv/bin/python scripts/check.py --quick    # the same without the full test suite, while you work
```

`tests/test_guards.py` turns the rules in [AGENTS.md](AGENTS.md) into tests: who may send to the robot, defaults
that stay off, paths open without sign-in, no HTML strings or new hard-coded colours in the UI. If one fails, the
message names the rule. Don't change the guard test to get past it; if you think a rule should change, open an issue.

Put your own private values in `.private-denylist`, one per line: your robot's serial, your street, your home
network. The file is git-ignored, and `check_public.py` will then refuse them too.

If you changed something visible, regenerate the screenshots from the demo. This needs `pip install playwright`
and Microsoft Edge or Chrome:

```bash
.venv/bin/python scripts/screenshots.py
```

## Pull requests

1. Fork the repo, branch from `main`, and keep each PR to one change.
2. Add tests. Fixtures are always synthetic: never paste a real map, capture, serial, address or coordinates.
3. Add a line to `CHANGELOG.md` under **Unreleased** for anything a user would notice. If the change is visible,
   update the help page in `docs/user/` it belongs to, and remove its line from `docs/user/not-yet.md` if it adds
   something that page lists.
4. Run `scripts/check.py` and fill in the PR template, including the safety checklist.
5. CI must pass. A maintainer reviews every PR. Anything that sends a command to the robot, changes an interlock,
   touches sign-in or sends location data off the machine gets a second, closer look.
6. [CodeRabbit](https://coderabbit.ai) reviews each PR once it is ready for review, and again on each push
   (`.coderabbit.yaml`; drafts wait). It checks the PR against `AGENTS.md`. Its comments are suggestions: fix what is
   right, reply where it is wrong, and leave the decision to the maintainer. Never weaken a check because it asked.

Dependabot opens PRs for Python packages (weekly), and GitHub Actions and the Docker base image (monthly), as set in
`.github/dependabot.yml`. A maintainer merges them once CI passes. Some moves are left out on purpose and done by hand
as their own change: a new `mcp` major, and a new Python minor for the image.

## Working with an AI assistant

AI-assisted PRs are welcome, and the repo is set up for them. [AGENTS.md](AGENTS.md) is written for coding agents
(Claude Code reads it through `CLAUDE.md`; Codex, Cursor, Copilot and others read `AGENTS.md` directly), and each of
`app/`, `web/`, `tests/` and `catalog/` has its own short `AGENTS.md`. Point your assistant at the issue and let it
read those first.

What we ask:

- **Say so in the PR**, and which parts the assistant wrote. Nobody is judged for it; it tells the reviewer where to
  look harder.
- **You own the change.** Read the whole diff, run `scripts/check.py`, and try it in the demo. "The AI wrote it" is
  not an answer to a review question.
- **Keep it to the issue.** Assistants like to tidy up along the way; revert anything the issue didn't ask for.
- **No invented robot behaviour.** If the assistant "knows" a command, payload or field meaning that isn't in
  `docs/dev/` or a capture, it made it up. Remove it and ask for a capture instead.
- **No real data in the prompt that ends up in the repo.** Assistants copy what you show them. Give them the demo,
  not your yard.
- **Don't let an assistant drive your robot.** Develop against `DEMO=1`. The panel's own AI assistant support
  (Settings → AI assistant) is a separate feature for using the panel (off by default, behind the same interlocks), not for developing it.

## Recording an app edit

The panel learns new robot commands only from the official app, never by guessing. To record one:

1. Run the panel against your robot, signed in to the Yarbo cloud (Settings → Robot).
2. Open Map Studio → More → **Record an app edit** and start a recording.
3. Make one change in the Yarbo app, such as one setting on one area, and write down exactly what you changed.
4. Stop the recording and open **Captured writes**. Passwords, tokens and email addresses are masked before
   anything is stored.
5. Open an issue with the "Protocol capture" template. Paste the command and say what you changed in the app.
   **Remove anything that locates your yard first:** coordinates, point lists and your robot's serial.

## Releases (maintainers)

Versions follow [semver](https://semver.org). `CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com).
A release normally bumps the last number (0.19.0 → 0.19.1), whatever it holds: fixes and new features alike. The
middle number goes up only when a maintainer decides a release is a big step (a new way of reaching the robot, say,
or a change people must act on).

1. Bump `__version__` in `app/version.py` and `appVersion` in `deploy/helm/groundskeeper/Chart.yaml`.
2. In `CHANGELOG.md`, rename `## Unreleased` to `## X.Y.Z — YYYY-MM-DD` and start a new empty `## Unreleased`
   above it.
3. Commit, then tag and push the tag:
   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```
4. The release workflow checks that the tag, `app/version.py` and the changelog agree. It then publishes
   `ghcr.io/groundskeeper-robot/groundskeeper:X.Y.Z`, `:X.Y` and `:latest` for amd64 and arm64, and creates a GitHub
   release whose notes are that version's changelog section.

### Pre-release images

`.github/workflows/prerelease.yml` builds `ghcr.io/groundskeeper-robot/groundskeeper-dev`: `:edge` on every push to
`main`, `:nightly` (and `:nightly-YYYYMMDD`) on a schedule when `main` changed, `:sha-<commit>` for each. Each build
runs `scripts/check.py` first and passes `PANEL_CHANNEL`, so the panel shows the channel beside its version. Nothing
there writes to the release package or `:latest`. Run the workflow by hand for a nightly now. Old versions are not
pruned automatically (free storage for a public package; a count-based clean-up can split a multi-arch build).

Once, after the first run, make the package **public** in its settings on GitHub.

## Security

Please don't open a public issue for a security problem. See [SECURITY.md](SECURITY.md).

## Conduct

Be kind and assume good faith. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
