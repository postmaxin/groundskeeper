"""Replay pace: a sample with no speed keeps the chosen rate instead of being hurried as if the robot stood still.
Run in node (no browser, no robot)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


def rates() -> dict:
    out = subprocess.run([shutil.which("node"), str(ROOT / "tests" / "replay_rate_flow.cjs")],
                         capture_output=True, encoding="utf-8", timeout=60, check=True).stdout
    return json.loads(out)


@needs_node
def test_a_sample_with_no_speed_keeps_the_chosen_replay_rate():
    assert rates()["missing"] == [1, 1]


@needs_node
def test_a_standing_robot_or_a_track_gap_is_hurried():
    r = rates()
    assert r["still"] == 600 and r["gap"] == 600 and r["fast"] == 6000
    assert r["moving"] == 1
