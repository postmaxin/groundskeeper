# 0005. Guards act by themselves; resuming after a guard is opt-in

- **Status:** Accepted
- **Date:** 2026-11-11 (recorded; decided when the guards were built)
- **Links:** design §10.3, §11

## Context
During a run the guards pause or dock the robot when a person or pet is seen, rain starts, everyone leaves or a
plan heads back out after dark. Stopping is always the safe direction. Resuming is not: it spins the blades again,
and the causes are detected unreliably (a camera misses a child behind a tree, a detection flaps). The yard cameras
are also only a second opinion on top of the robot's own detection.

## Decision
- Guards are level-triggered and keep sending their pause or dock until the robot confirms it. The interlocks for
  pause, stop and dock are only the robot-online and route checks. Guards act only while control is on; with control
  off they notify.
- A person stays in charge: a person's resume or start while a guard holds snoozes that guard for the rest of that
  detection.
- Resuming after a guard pause is **off by default** (`guard_resume`). When the owner turns it on, the resume checks
  (no error, yard clear, phone app quiet, lawn and presence when switched on) are all hard and unknown counts as a
  fail; it only undoes a pause the guard itself sent and saw confirmed.
- The after-dark guard never pauses (a "pause" setting acts as dock), so the robot is not left on the lawn all
  night.
- An AI assistant can never turn `guard_resume` on.

## Consequences
- After a guard pause someone has to press Resume; a run can sit paused until they do.
- A false detection costs a pause, never an unexpected restart.

## Enforced by
`app/ops.py` (`_guard_tick`, `_guard_resume`), `app/control.py` (`GATE_DEFAULTS`, `check` for actor `guard`),
`app/mcp_server.py` (`NEVER_GATES`); `tests/test_guards.py` `test_a_new_install_has_every_switch_off`; the guard
tests in `tests/test_runs.py` (for example
`test_a_human_resume_during_a_detection_snoozes_the_guard_for_that_detection`).
