# Decisions

Architecture decision records (ADRs): **why** Groundskeeper is the way it is. Each record is one decision, its
context and what follows from it. How things work now is described in [`../design.md`](../design.md) and
[`../architecture.md`](../architecture.md); this folder keeps the reasons, so a later change can see what it would
undo.

## When a change needs one

Write an ADR in the same PR (or before it, for a decision that comes first) when the change:

- adds, removes or loosens a **safety rule**: an interlock, a guard, a default, who may send what, or a rule in
  `tests/test_guards.py`;
- changes **how the panel reaches the robot**: a route, a transport, or a class of command it may send;
- is **hard to reverse**: a storage format people's databases depend on, a dependency or licence, removing a
  feature;
- **overrides** an earlier ADR. Agree that in the issue first.

Most PRs need none: a fix, a new read, a UI change, a setting with a safe default. When unsure, ask in the issue.

## Rules

- **Never edit an accepted ADR's decision.** To change a decision, write a new ADR and mark the old one
  `Superseded by NNNN`. Fixing a typo or a broken link is fine.
- **Proposed** means agreed in principle and not built yet; it becomes **Accepted** in the PR that builds it.
- Name the code or test that enforces the decision, so a reader can check it still holds without trusting the prose.
- Keep it short: a page at most. No real robot data, dates of runs or readings (the privacy rules in `AGENTS.md`).
- Number in order, four digits, and add a line to the index below. `tests/test_decisions.py` checks the format and
  the index.

## Template

```markdown
# NNNN. Title in a few words

- **Status:** Proposed | Accepted | Superseded by NNNN | Rejected
- **Date:** YYYY-MM-DD
- **Links:** #issue, #PR

## Context
What forces the decision: the robot's behaviour, a constraint, what went wrong.

## Decision
What we do, in a few sentences.

## Consequences
What follows, good and bad, and what would make us revisit it.

## Enforced by
The code and tests that hold the decision in place.
```

## Index

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-record-decisions.md) | Record decisions as ADRs | Accepted |
| [0002](0002-no-manual-driving.md) | Never drive the robot by hand (`cmd_vel` blocked) | Accepted |
| [0003](0003-panel-owns-scheduling.md) | The panel owns scheduling and never writes robot schedules | Accepted |
| [0004](0004-writes-from-captures-read-back.md) | Robot writes only from captures, read back and undone on a mismatch | Accepted |
| [0005](0005-guard-resume-opt-in.md) | Guards act by themselves; resuming after a guard is opt-in | Accepted |
| [0006](0006-rtk-not-a-start-gate.md) | RTK is not a panel-side start gate | Accepted |
| [0007](0007-yarbo-cloud-only.md) | Yarbo cloud only: no local route | Accepted |
| [0008](0008-sdk-noncommercial-licence.md) | Use Yarbo SDK 0.3.0 under PolyForm Noncommercial | Accepted |
| [0009](0009-head-aware-run-rules.md) | Run rules depend on the attached head | Proposed |
| [0010](0010-robot-video-through-yarbos-agora.md) | Robot video through Yarbo's Agora channel, as a host that publishes nothing | Accepted |
