# 0009. Run rules depend on the attached head

- **Status:** Proposed
- **Date:** 2026-11-11 (revised before acceptance)
- **Links:** #126, #127, #128, #136

## Context
The robot carries different heads, and every run records the head type (`HeadMsg.head_type`: 0 none, 1 snow blower,
2 leaf blower, 3 mower, 4 Smart Cover, 5 Mower Pro). The start checks and guards were written for mowing: "Lawn dry"
refuses a start in falling snow, and the rain guard docks the robot in sleet, which is when a snow blower is needed.
The area settings editor reads the mower's settings record for any area, including snow areas. The robot's own rain
sensor is on the mower module, so with the snow blower attached the panel's rain condition is the only rain input.

## Decision
- **Only the snow blower (head type 1) relaxes anything.** Every other head, including the leaf blower, the Smart
  Cover, no head and a head the panel has not heard yet, gets the full mower rules (fail closed).
- With the snow blower attached:
  - **Lawn dry** is not checked. It is listed as "not for the snow blower" so the check list still shows it, for every
    actor, the scheduler and assistants included.
  - **The rain guard notifies** instead of pausing or docking, whatever its setting; "ignore" stays ignore. Mower
    heads keep the owner's setting.
- Every other rule applies to every head unchanged: the yard-clear check and the person guard, the everyone-left and
  after-dark guards, presence, the phone-app check, battery, no active error, a write in progress, and the strict
  reading for assistants and API tokens.
- **Fixed in code, not settings.** Which rules a head relaxes is not configurable, so a setting can never make a head
  skip a rule that protects people. Settings → Run rules says which rules are mower-only.
- The head is read from the robot's latest report each time a check or guard runs, so a head change takes effect at
  the next check. The snow blower's relaxations apply only while that report is fresh (the robot-online limit); a
  stale, missing or unknown report gets the full mower rules.
- A head-specific editor or write is not offered for an area labelled only for other heads: the mower area settings
  editor is read-only on an area whose labels leave out Lawn Mower, and when the labels cannot be read. An area with
  no labels (new areas, the demo) stays editable (#128).
- **Snow rules of their own** (snowfall, a temperature floor, a minimum battery for the cold) are not part of this
  decision. They come with their own ADR once their weather inputs are chosen.

## Consequences
- A snow run carries on through rain and sleet with a notification; stopping it is a person's call.
- The after-dark guard still sends a snow plan home when it heads out again after charging at night. Night clearing is
  common, so revisit this once snow runs have been recorded.
- Each relaxed rule needs a guard test for the snow blower and for a head that does not relax it (leaf blower, none,
  unknown).
- Becomes Accepted in the PR that builds it (#126, #127), which names the code and tests below.

## Enforced by
To be added with #126 and #127: `app/control.py` (`check`), `app/ops.py` (guards), `tests/test_guards.py`.
