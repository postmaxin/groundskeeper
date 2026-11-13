"""The panel's version. Bump it with each release that changes behaviour, move CHANGELOG.md's "Unreleased" entry
under the new number in the same commit, and tag the commit `v<version>`. A release bumps the last number
(0.19.0 → 0.19.1); the middle one only when a maintainer decides a release is a big step (CONTRIBUTING.md → Releases). The build (git commit) comes from the
image: scripts/deploy.sh passes it as PANEL_BUILD."""
import os
import re

__version__ = "0.20.0"
_build = (os.environ.get("PANEL_BUILD") or "").strip().lower()
BUILD = (_build[:7] if re.fullmatch(r"[0-9a-f]{8,40}", _build) else _build[:20]) or None   # a full commit id is cut to 7
# A pre-release image says which: "edge" (built on every merge to main) or "nightly" (once a night), from
# .github/workflows/prerelease.yml. None for a release or a local build, and for anything else, so a typo shows nothing.
_channel = (os.environ.get("PANEL_CHANNEL") or "").strip().lower()
CHANNEL = _channel if _channel in ("edge", "nightly") else None


def version_info() -> dict:
    return {"version": __version__, "build": BUILD, "channel": CHANNEL}
