"""Which head the 3D robot draws (#134): the Snow Blower when it is the head, else the mower; live the attached
head, in a replay the head the run was driven with. The page script is run in node against stubs (no browser, no
three.js, no map)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


def run_flow() -> dict:
    out = subprocess.run([shutil.which("node"), str(ROOT / "tests" / "robot3d_head_flow.cjs"),
                          str(ROOT / "web" / "robot3d.js")],
                         capture_output=True, encoding="utf-8", timeout=60, check=True).stdout
    return json.loads(out)


@needs_node
def test_live_it_draws_the_snow_blower_only_when_that_is_the_attached_head():
    live = run_flow()["live"]
    assert live["snow"] == live["snowAsText"] == live["snowAgain"] == live["snowOnceMore"] == "snow"
    assert live["mowerPro"] == live["mower"] == live["leafBlower"] == live["none"] == "mower"


@needs_node
def test_a_status_with_no_head_puts_the_mower_back():
    r = run_flow()
    assert r["live"]["notHeard"] == "mower" and r["live"]["missing"] == "mower"
    assert r["noStatus"] == "snow"                       # no status at all changes nothing


@needs_node
def test_a_head_heard_before_3d_is_switched_on_is_drawn_when_the_model_is_built():
    r = run_flow()
    assert r["beforeBuild"] == 1 and r["afterBuild"] == "snow"


@needs_node
def test_a_replay_draws_the_head_the_run_was_driven_with():
    r = run_flow()
    assert r["replay"]["snowRunMowerNow"] == "snow" and r["replay"]["mowerRunSnowNow"] == "mower"
    assert r["replay"]["runWithNoHead"] == "mower" and r["replay"]["runWithNoInfo"] == "mower"
    assert r["backToLive"] == "snow"                     # the attached head again once the replay is closed


@needs_node
def test_the_map_is_redrawn_only_when_the_head_changes():
    r = run_flow()
    assert r["repaintsSameHead"] == 0 and r["repaintsOnChange"] == 1
