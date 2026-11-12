# 0003. The panel owns scheduling and never writes robot schedules

- **Status:** Accepted
- **Date:** 2026-11-11 (recorded; decided when the scheduler was built)
- **Links:** design §8.1, `docs/dev/map-writes.md` (schedules), #68

## Context
The robot can keep schedules of its own (`save_schedule`), but it starts them with none of the panel's checks: no
lawn conditions, presence, yard cameras or phone-app check. A robot-side schedule and a panel window for the same
plan would also start it twice.

## Decision
The panel runs every schedule itself (windows, gates, confirmations, auto-start) and never writes a robot-side
schedule. It reads the robot's own schedules and shows any it finds on the Schedule page, saying that they skip the
run rules.

## Consequences
- **If the panel is down, nothing runs.** Mitigations: an uptime monitor on `/health`, and the phone app as the
  manual fallback.
- Schedules set up in the official app still run without the panel's rules; the panel can only point them out.
- Revisit only if the robot gains a way to make its schedules wait for an outside check.

## Enforced by
`app/scheduler.py`, `app/ops.py`; `app/commands.py` (`check_command`: `save_schedule` is not sendable);
`tests/test_phase2.py` `test_the_panel_never_writes_robot_schedules`.
