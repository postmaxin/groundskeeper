# app/: the server

Local rules for the Python side. The top-level `AGENTS.md` comes first.

- **Who may talk to the robot.** `cloud.publish()` is reached only through `link.RobotLink`. Its `read()` takes only
  `READ_ONLY_COMMANDS` (`commands.py`); `command()` and `write_command()` are called only by
  `control.Controller.send()` and `mapwrite.MapWriter` (`ops.py` wires the writer). `tests/test_guards.py` checks
  this.
- **Interlocks** are in `control.check` and run on the server for every sender (UI, scheduler, guards, MCP). A new
  one fails closed: unknown or missing state blocks. The reason shown to the user goes through `why.py`.
- **Writes** to the robot run under `MapWriter.lock`, are read back, compared and put back on a mismatch. A per-id
  read must check the reply's `id`: replies are matched by topic name only (`Recorder._pending`).
- **Routes** live in `main.py`. Sign-in is a middleware (`auth.needs_auth`); a new `/api/` route is locked without
  you doing anything. Password and token routes check `_person`.
- **Settings** have a safe default in `SETTINGS_DEFAULTS`; integration settings (addresses, tokens) go through
  `runtime.py` and apply without a restart.
- **Store** (`store.py`) is SQLite through the store's own methods. Add a method there rather than SQL in a route.
  Schema changes must upgrade an existing database in place: people update by pulling a new image.
- **Time and the robot.** Robot timestamps can be missing or late; telemetry rows carry arrival time and, where known,
  the robot's own pose time (`odom_ts`). Don't assume samples arrive in order.
- **Location is private.** Don't log coordinates, imagery URLs (they carry coordinates), the serial or tokens. The
  MCP server `scrub`s what it returns; keep it that way.
- **Docstrings cite the design** (`§7.4` = `docs/dev/design.md` section 7.4). Keep the citation when you change the
  code, and update the section if the behaviour changes.
- `imagery/` has its own conventions: see `docs/dev/imagery-catalog.md` and `catalog/AGENTS.md`.
