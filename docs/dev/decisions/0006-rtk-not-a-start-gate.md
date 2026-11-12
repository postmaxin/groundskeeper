# 0006. RTK is not a panel-side start gate

- **Status:** Accepted
- **Date:** 2026-11-11 (recorded; decided after the first recorded starts)
- **Links:** design §10.3, §15; `docs/dev/protocol.md`

## Context
A docked robot often reports no RTK fix and no corrections, and gets its fix only after it leaves the dock. A start
check on RTK would refuse almost every start. The robot already enforces its own RTK precondition.

## Decision
The panel does not check RTK before a start. It sends the command and shows the robot's own refusal word for word.
RTK quality is shown on Health and on the run's map, and gap detection counts only RTK-fixed track (design §12).
While a robot waits for its fix with the blades stopped, the panel says so instead of raising a stuck alert.

## Consequences
- A start can be accepted by the panel and then refused or held by the robot; the reason comes from the robot.
- Revisit if the robot is seen starting to mow without a fix.

## Enforced by
`app/control.py` (`check` has no RTK check), `app/recorder.py` (`waiting_for_fix`), `app/coverage.py`;
`tests/test_phase1b.py` `test_float_rtk_is_unknown_never_a_gap`.
