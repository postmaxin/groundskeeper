"""Keyboard focus survives a redraw (#36, #37): the Live panel replaces every card on each refresh and the Cameras
tile rebuilds itself on every phase change, which would drop focus to the page. Run in node against a small fake DOM
that loses focus when the focused element is removed, as a browser does (no browser, no robot, no Agora)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


def run_flow() -> dict:
    out = subprocess.run([shutil.which("node"), str(ROOT / "tests" / "focus_flow.cjs"), str(ROOT / "web" / "app.js"),
                          str(ROOT / "web" / "cameras.js")], capture_output=True, encoding="utf-8", timeout=60, check=True).stdout
    return {row[0]: row[1:] for row in json.loads(out)}


@needs_node
def test_opening_closing_and_retrying_move_focus_to_the_control_that_takes_over():
    r = run_flow()
    assert r["open"] == ["BUTTON", "cam-close", "error"]               # Open is gone
    assert r["retry"] == ["BUTTON", "cam-close"]                       # Try again is gone
    assert r["close"] == ["BUTTON", "cam-open", "closed"]              # Close is gone


@needs_node
def test_a_camera_change_and_a_live_refresh_keep_focus_on_the_same_chip():
    r = run_flow()
    assert r["pick-left"] == ["BUTTON", "cam-left", "left"]
    assert r["refresh"] == ["BUTTON", "cam-left", True]                # a new element, the same chip focused


@needs_node
def test_a_keyed_control_keeps_focus_when_the_pending_card_shifts_the_ones_after_it():
    assert run_flow()["keyed-shift"] == ["BUTTON", "ctl-pause"]


@needs_node
def test_a_focused_summary_keeps_focus():
    assert run_flow()["summary"] == ["SUMMARY", "ready-summary"]


@needs_node
def test_focus_is_not_guessed_when_the_control_is_disabled_or_cannot_be_told_apart():
    r = run_flow()
    assert r["disabled"] == ["BODY", None]                             # Stop became disabled: it cannot take focus
    assert r["unkeyed"] == ["BUTTON", "plain-b"]                       # no key, but its kind and text single it out
    assert r["ambiguous"] == ["BODY", None]                            # two buttons now read "Same": do not pick one


@needs_node
def test_focus_elsewhere_is_never_taken():
    r = run_flow()
    assert r["outside"] == ["BUTTON", "outside"]
    assert r["nothing-focused"] == ["BODY", None]


def test_live_control_buttons_and_readiness_summaries_have_stable_keys():
    code = (ROOT / "web" / "ops.js").read_text(encoding="utf-8")
    for k in ("ctl-${cmd}", "ctl-start_plan", "ctl-plan", "pending-start", "pending-dismiss", "ready-summary", "ready-checks", "emerg-${cmd}"):
        assert f'"data-key": "{k}"' in code or f'"data-key": `{k}`' in code, k


def test_every_tile_control_has_a_stable_key():
    code = (ROOT / "web" / "cameras.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'"data-key": [`"]([\w${}.-]+)[`"]', code))
    assert {"cam-open", "cam-close", "cam-retry", "cam-tip", "cam-${k}"} <= keys
