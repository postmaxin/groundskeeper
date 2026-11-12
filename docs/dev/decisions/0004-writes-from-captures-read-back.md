# 0004. Robot writes only from captures, read back and undone on a mismatch

- **Status:** Accepted
- **Date:** 2026-11-11 (recorded; decided when map writing was built)
- **Links:** design §7.4, §14; `docs/dev/map-writes.md`

## Context
Yarbo documents no write commands. The robot accepts payloads it does not fully check, ids tie plans and settings to
areas, and a whole-map restore deletes every plan. A guessed payload can corrupt the map the robot mows by, and the
reply to a write does not prove what was saved.

## Decision
- A write command is used only after it has been captured from the official app, documented in `map-writes.md` and
  replayed against a harmless test object.
- Writes go only through `MapWriter` (and the settings writers built on it), only with the map-writing switch on,
  one at a time. Map, area, plan and network writes need the robot on its charger with no plan; settings writes need
  it idle; backup bookkeeping (save, rename, delete a backup) may run during a plan, since nothing the robot drives
  by changes.
- Every write is read back and compared. Where the panel can put the old state back, a mismatch undoes everything
  that was sent. Where it cannot (a Wi-Fi join or forget, a whole-map restore), the result is only checked
  afterwards.

## Consequences
- Some edits the app can make are not offered: sidewalks and fences have no captured write, and adding or deleting
  an area (captured) changes ids and drops the plans and settings tied to it, so it stays in the app. The draft keeps
  such edits and says why.
- New write support is slow to add, and each needs the owner of a real robot to capture and prove it.

## Enforced by
`app/mapwrite.py`, `app/areasettings.py`, `app/link.py` (`write_command`); `tests/test_guards.py`
`test_only_the_map_writer_gets_the_write_door` and `test_every_robot_command_is_documented`; the read-back tests in
`tests/test_area_writes.py`, `tests/test_pathway_params.py`, `tests/test_deadends.py`.
