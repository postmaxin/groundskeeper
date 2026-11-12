## What and why

<!-- One change per PR. Link the issue it closes. -->

## Checks

- [ ] `python scripts/check.py` prints "all checks passed" (tests, guard tests, catalog, privacy, JS)
- [ ] New behaviour has a test with synthetic data; no real maps, captures, serials, addresses or coordinates added
- [ ] `CHANGELOG.md` (Unreleased) and the matching `docs/user/` page updated, if a user would notice
- [ ] Screenshots regenerated (`scripts/screenshots.py`), if the UI changed visibly
- [ ] `docs/dev/` describes how it works now, and an ADR is added in `docs/dev/decisions/` if this changes a safety
      rule, how the panel reaches the robot, something hard to reverse, or overrides an ADR (or: not needed)

## Safety (tick what applies, or "none")

- [ ] Sends a new command to the robot: it comes from a capture of the official app (link it)
- [ ] Changes an interlock, guard, default or switch: it fails closed and no default got looser
- [ ] Writes to the robot: the result is read back and put back on a mismatch
- [ ] Changes sign-in, tokens or which paths are open
- [ ] Sends data (especially location) to a new place
- [ ] Changes `tests/test_guards.py` (explain why the rule itself should change; link the ADR if it loosens one)
- [ ] None of the above

## What wasn't verified

<!-- e.g. "not tried on a real robot", "not looked at on a phone". Say it here rather than leave it for review. -->

## AI assistance

- [ ] No AI assistant was used
- [ ] An AI assistant wrote some or all of this: <!-- which one, and which parts -->
- [ ] I read the whole diff and ran the checks myself
