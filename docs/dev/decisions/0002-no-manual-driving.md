# 0002. Never drive the robot by hand

- **Status:** Accepted
- **Date:** 2026-11-11 (a goal from the start, recorded here)
- **Links:** design §1 Non-goals, §10.1

## Context
The robot accepts `cmd_vel`, a velocity command for driving it by hand. Driving a machine with blades
from a web page, over a cloud link that can lag by a minute on a weak connection, with no view of what is in front
of it, cannot be made safe by interlocks: the command is the danger, whatever state the robot is in.

## Decision
`cmd_vel` is never sent, by any actor, through any route, and is not offered in the UI, the API, the MCP server or
the capture console. Motor commands seen from the app (such as `cmd_roller`) are never sent either. The panel moves
the robot only through the official run commands (`start_plan`, `pause`, `resume`, `stop`, `cmd_recharge`).

## Consequences
- No "drive it out of a stuck spot" from the panel: the phone app, standing next to the robot, stays the way to do
  that.
- A request for joystick control is answered with this ADR; it would need a new ADR that supersedes this one.

## Enforced by
`app/control.py` (`HARD_BLOCKED`, `CATALOGUE`), `app/commands.py` (`check_command`); `tests/test_guards.py`
`test_manual_driving_stays_blocked` and `test_the_robot_is_sent_nothing_but_run_commands_and_captured_writes`.
