# 0001. Record decisions as ADRs

- **Status:** Accepted
- **Date:** 2026-11-11
- **Links:** #136

## Context
The reasons behind the panel's safety rules and its larger choices were spread over `design.md`, PR descriptions
and the maintainer's own notes. PRs are squash-merged, AI agents and outside contributors cannot see private notes,
and several deliberate choices look like oversights without their reason (no auto-resume, no manual driving, no robot
schedules). `design.md` also mixed plans, history and open questions with descriptions of how the code works, so it
drifted from the code.

## Decision
- `docs/dev/decisions/` holds one short ADR per decision. Accepted ADRs are not rewritten: a new one supersedes them.
- `design.md` and `architecture.md` describe only how the panel works now, and link to the ADR for the why.
- Plans and open questions live in GitHub issues (`research`, `capture needed`), not in the design docs.
- An ADR is required only for the changes listed in this folder's README (safety rules, how the panel reaches the
  robot, hard-to-reverse choices, overriding an ADR). It is a review rule, not a CI gate.

## Consequences
- A reviewer, human or bot, can check a change against a written decision instead of working it out again.
- One more place to look; the README's split (now = design docs, why = ADRs, next = issues) keeps them apart.
- The first ADRs are backfilled from the existing design text and PRs, so their dates are when they were recorded.
- Revisit if ADRs pile up for small changes (the bar is too low) or if safety changes land without one (too high).

## Enforced by
`tests/test_decisions.py` (format, numbering, index); the PR template's decision line; `AGENTS.md` → How to work.
