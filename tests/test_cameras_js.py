"""The Cameras tile (web/cameras.js, §13): what it sends to the robot, the order it sets Agora's SDK up in, and when
it leaves the channel. The page script is run in node against stubs (no browser, no Agora, no robot), plus source
checks for the rules a flow test can't see."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "web" / "cameras.js").read_text(encoding="utf-8")
CODE = re.sub(r"/\*.*?\*/|//[^\n]*", "", SRC, flags=re.S)             # without comments
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


def run_flow() -> list:
    out = subprocess.run([shutil.which("node"), str(ROOT / "tests" / "cameras_flow.cjs"), str(ROOT / "web" / "cameras.js")],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    return json.loads(out)


def segment(calls, name):
    start = next(i for i, c in enumerate(calls) if c == ["--", name])
    end = next((i for i, c in enumerate(calls[start + 1:], start + 1) if c[0] == "--"), len(calls))
    return calls[start + 1:end]


@needs_node
def test_opening_switches_on_then_joins_with_statistics_off_and_encryption_set():
    seg = segment(run_flow(), "open")
    names = [c[0] for c in seg]
    assert seg[0][:2] == ["post", "/api/control/send"]
    assert json.loads(seg[0][2]) == {"cmd": "smart_vision_control", "args": {}, "confirm": True}
    assert seg[1][:2] == ["post", "/api/video/token"] and json.loads(seg[1][2]) == {"camera": "front"}
    order = ["setParameter", "setLogLevel", "createClient", "setClientRole", "setEncryptionConfig", "join"]
    assert [n for n in names if n in order] == order
    assert ["setParameter", "ENABLE_EVENT_REPORT", False] in seg
    enc = next(c for c in seg if c[0] == "setEncryptionConfig")
    assert enc[1:] == ["aes-128-gcm2", "k", "1,2,3"]                    # the salt is base64-decoded to bytes
    assert ["setClientRole", "audience"] in seg
    assert "enableLogUpload" not in names


@needs_node
def test_the_robots_video_is_subscribed_to():
    assert ["subscribe", 1, "video"] in run_flow()


@needs_node
def test_changing_camera_leaves_the_channel_and_does_not_send_a_command_again():
    seg = segment(run_flow(), "pick-left")
    assert "leave" in [c[0] for c in seg]
    posts = [c for c in seg if c[0] == "post"]
    assert [p[1] for p in posts] == ["/api/video/token"] and json.loads(posts[0][2]) == {"camera": "left"}


@needs_node
def test_a_hidden_tab_leaves_the_channel_and_closes_the_tile():
    calls = run_flow()
    assert "leave" in [c[0] for c in segment(calls, "hide")]
    assert ["open", False, "closed"] in segment(calls, "closed-state")


@needs_node
def test_leaving_live_leaves_the_channel_and_closes_the_tile():
    seg = segment(run_flow(), "leave-live")
    assert "leave" in [c[0] for c in seg]
    assert ["open", False, "closed"] in seg


@needs_node
def test_with_control_off_it_sends_no_command_and_says_why():
    calls = run_flow()
    seg = segment(calls, "open-control-off")
    assert [c[1] for c in seg if c[0] == "post"] == ["/api/video/token"]
    assert any(c[0] == "hint" and "Control is off" in c[1] for c in calls)


@needs_node
def test_nothing_ever_asks_for_state_zero_or_enables_log_upload():
    calls = run_flow()
    posts = [json.loads(c[2]) for c in calls if c[0] == "post" and c[1] == "/api/control/send"]
    assert posts and all(p == {"cmd": "smart_vision_control", "args": {}, "confirm": True} for p in posts)
    assert "enableLogUpload" not in [c[0] for c in calls]


def test_the_source_never_names_state_zero_log_upload_or_the_console():
    assert "enableLogUpload" not in CODE
    assert not re.search(r"console\.", CODE)
    assert not re.search(r"state\s*:\s*0\b", CODE)
    assert not re.search(r"(localStorage|sessionStorage)[^;]*(token|key|salt)", CODE, re.I)


def test_the_sdk_comes_from_this_panel_only():
    assert 'CAM_SDK_URL = "/vendor/AgoraRTC_N-production.js"' in CODE
    assert not re.search(r"https?://", CODE)
