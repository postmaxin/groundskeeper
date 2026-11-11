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
                         capture_output=True, encoding="utf-8", timeout=60, check=True).stdout
    return json.loads(out)


def segment(calls, name):
    start = next(i for i, c in enumerate(calls) if c == ["--", name])
    end = next((i for i, c in enumerate(calls[start + 1:], start + 1) if c[0] == "--"), len(calls))
    return calls[start + 1:end]


# Every Agora SDK parameter the tile sets before the client exists, with the value (all written with force: the SDK
# ignores a plain setParameter for a key a cached or remote configuration has already set).
QUIET = {"ENABLE_EVENT_REPORT": False, "UPLOAD_LOG": False, "ENABLE_CONFIG_DISTRIBUTE": False, "ENABLE_PRELOAD": False,
         "ENABLE_CREATE_CLIENT_PRECONNECT": False, "CREATE_CLIENT_PRECONNECT_MODE": "report", "INSTALL_ID": True}


@needs_node
def test_opening_switches_on_then_joins_with_the_sdk_quietened_and_encryption_set():
    seg = segment(run_flow(), "open")
    names = [c[0] for c in seg]
    assert seg[0][:2] == ["post", "/api/control/send"]
    assert json.loads(seg[0][2]) == {"cmd": "smart_vision_control", "args": {}, "confirm": True}
    assert seg[1][:2] == ["post", "/api/video/token"] and json.loads(seg[1][2]) == {"camera": "front"}
    order = ["setLogLevel", "createClient", "setClientRole", "setEncryptionConfig", "join"]
    assert [n for n in names if n in order] == order
    params = {c[1]: c[2:] for c in seg if c[0] == "setParameter"}
    assert params == {k: [v, True] for k, v in QUIET.items()}                # every one, and forced
    last_param = max(i for i, n in enumerate(names) if n == "setParameter")
    assert last_param < names.index("setLogLevel") < names.index("createClient")   # all before the client exists
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


@needs_node
def test_a_slow_leave_and_a_fast_token_never_make_two_live_clients():
    calls = run_flow()
    before = segment(calls, "slow-leave")
    assert "leave" in [c[0] for c in before]
    assert "createClient" not in [c[0] for c in before]                 # still waiting for the first leave
    assert [c[0] for c in segment(calls, "slow-leave-released")].count("createClient") == 1


@needs_node
def test_a_refused_token_never_loads_the_sdk():
    calls = run_flow()
    seg = segment(calls, "refused-token")
    assert "sdkRead" not in [c[0] for c in seg]
    assert ["phase", "error", "Getting a token failed: The panel refused."] in seg


@needs_node
def test_a_failed_join_says_which_step_failed_and_why_without_secrets():
    calls = run_flow()
    seg = segment(calls, "join-operation-error")                        # a DOMException with an empty message
    assert ["phase", "error", "Joining failed: OperationError"] in seg
    seg = segment(calls, "join-sdk-code")                               # an SDK error shows its code, never its message
    assert ["phase", "error", "Joining failed: INVALID_OPERATION"] in seg
    assert "007tok" not in json.dumps([c for c in seg if c[0] == "phase"])
    long = next(c for c in segment(calls, "join-long") if c[0] == "phase")
    assert long[2].startswith("Joining failed: xxx") and len(long[2]) <= 100
    seg = segment(calls, "token-502")                                   # the server's fixed phrase, as before
    assert ["phase", "error", "Getting a token failed: The robot video service did not answer."] in seg


@needs_node
def test_a_robot_present_but_not_publishing_stays_waiting():
    seg = segment(run_flow(), "present-not-publishing")
    names = [c[0] for c in seg]
    assert "subscribe" not in names and "leave" not in names and "join" in names
    assert any(c[:2] == ["phase", "waiting"] for c in seg)


@needs_node
def test_a_failed_subscribe_leaves_and_shows_a_sanitized_error():
    seg = segment(run_flow(), "publish-fails")
    assert "leave" in [c[0] for c in seg]
    assert ["phase", "error", "Playing failed: SUBSCRIBE_FAILED"] in seg
    assert "007tok" not in json.dumps(seg)


@needs_node
def test_control_switched_on_after_the_last_render_still_sends_the_command():
    seg = segment(run_flow(), "control-just-on")
    assert seg[0][:2] == ["post", "/api/control/send"]


@needs_node
def test_renewing_the_token_never_sends_camera_on_even_with_control_on():
    calls = run_flow()
    for name in ("renew-will-expire", "renew-did-expire"):                # both events rejoin with a new token
        seg = segment(calls, name)
        assert [c[1] for c in seg if c[0] == "post"] == ["/api/video/token"], name
        assert [c[0] for c in seg].count("createClient") == 1, name


@needs_node
def test_only_the_robots_video_is_played():
    seg = segment(run_flow(), "other-publisher")
    names = [c[0] for c in seg]
    assert "subscribe" not in names and "play-other" not in names and "play" not in names
    assert ["phase", "waiting", "Waiting for the robot's video…"] in seg


@needs_node
def test_the_robot_stopping_its_video_goes_back_to_waiting_and_a_republish_plays_again():
    calls = run_flow()
    phases = [c[1:] for c in segment(calls, "unpublished") if c[0] == "phase"]
    assert phases == [["playing", ""], ["playing", ""], ["playing", ""], ["waiting", "The robot stopped sending video."]]
    again = segment(calls, "republished")
    assert ["subscribe", 1, "video"] in again and ["phase", "playing", ""] in again


@needs_node
def test_a_decrypt_error_leaves_and_says_so():
    seg = segment(run_flow(), "crypt-error")
    assert "leave" in [c[0] for c in seg]
    assert ["phase", "error", "Could not decrypt the stream."] in seg


@needs_node
def test_closing_the_page_leaves_the_channel_and_closes_the_tile():
    seg = segment(run_flow(), "pagehide")
    assert "leave" in [c[0] for c in seg] and ["open", False, "closed"] in seg


@needs_node
def test_video_switched_off_in_settings_stops_the_tile_and_says_why():
    seg = segment(run_flow(), "settings-off")
    assert "leave" in [c[0] for c in seg]
    assert ["card", True] in seg and ["open", False, "closed"] in seg
    assert ["phase", "closed", "Robot video was switched off."] in seg


@needs_node
def test_closing_during_the_join_goes_no_further():
    calls = run_flow()
    role = segment(calls, "close-during-role")                          # closed while setClientRole is pending
    assert not {"setEncryptionConfig", "join"} & {c[0] for c in role}
    assert ["phase", "closed", ""] in role
    join = segment(calls, "close-during-join")                          # closed while join is pending
    assert "subscribe" not in [c[0] for c in join]
    assert ["phase", "closed", ""] in join                              # not "waiting" over "closed"


@needs_node
def test_closing_while_waiting_on_a_leave_a_token_or_the_script_goes_no_further():
    calls = run_flow()
    leave = segment(calls, "close-during-leave")
    assert "post" not in [c[0] for c in leave] and "createClient" not in [c[0] for c in leave]
    reopened = segment(calls, "reopen-during-leave")
    assert [c[0] for c in reopened].count("post") == 0                  # no camera-on from a start that was closed
    token = segment(calls, "close-during-token")
    assert not {"createClient", "sdkRead"} & {c[0] for c in token} and ["phase", "closed", ""] in token
    script = segment(calls, "close-during-sdk-load")
    assert "createClient" not in [c[0] for c in script] and ["phase", "closed", ""] in script


@needs_node
def test_a_lost_connection_leaves_and_says_so_but_our_own_leave_is_not_an_error():
    calls = run_flow()
    assert [c[1:] for c in segment(calls, "own-leave") if c[0] == "phase"] == [["waiting", "Waiting for the robot's video…"]]
    lost = segment(calls, "connection-lost")
    assert "leave" in [c[0] for c in lost] and ["phase", "error", "The connection was lost."] in lost


@needs_node
def test_a_robot_already_publishing_at_join_is_subscribed_to_and_played_once():
    seg = segment(run_flow(), "already-publishing")
    names = [c[0] for c in seg]
    assert names.count("subscribe") == 1 and names.count("play") == 1
    assert [c for c in seg if c[0] == "phase"][-1] == ["phase", "playing", ""]   # the early event is not undone


@needs_node
def test_the_sdks_own_storage_is_cleared_after_it_loads_and_on_leaving():
    seg = segment(run_flow(), "sdk-load")
    # Only the panel's own item is ever left: the SDK's device id and caches (websdk_ng_*) are gone each time.
    assert [c[1] for c in seg if c[0] == "storage"] == ["yarbo.robotCamera"] * 4       # also while a leave is pending


@needs_node
def test_a_script_load_that_hangs_gives_up_and_the_next_try_loads_again():
    calls = run_flow()
    hung = segment(calls, "sdk-hangs")
    assert ["phase", "error", "Loading the video library failed: The video library took too long to load."] in hung
    assert ["sdk-cleared", True] in hung
    retry = segment(calls, "sdk-retry")
    assert "createClient" in [c[0] for c in retry] and ["phase", "waiting", "Waiting for the robot's video…"] in retry


def test_the_source_rejects_a_client_kept_before_it_has_joined():
    assert not re.search(r"cams\.\w+\s*=\s*join", CODE)                    # the join details are never kept on cams
    assert "cams.client = client" in CODE                               # the client alone is, so a close can leave it


def test_the_source_never_names_state_zero_log_upload_or_the_console():
    assert "enableLogUpload" not in CODE
    assert not re.search(r"console\.", CODE)
    assert "setLogLevel(4)" in CODE
    assert not re.search(r"state\s*:\s*0\b", CODE)
    assert not re.search(r"(localStorage|sessionStorage)[^;]*(token|key|salt)", CODE, re.I)


def test_the_sdk_comes_from_this_panel_only():
    assert 'CAM_SDK_URL = "/vendor/AgoraRTC_N-production.js"' in CODE
    assert not re.search(r"https?://", CODE)
