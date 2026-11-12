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
    flow, page = ROOT / "tests" / "cameras_flow.cjs", ROOT / "web" / "cameras.js"
    out = subprocess.run([shutil.which("node"), str(flow), str(page)],
                         capture_output=True, encoding="utf-8", timeout=60, check=True).stdout
    return json.loads(out)


BEAT_CALLS = ("beat", "interval", "clear-interval")      # the keep-awake timer's calls: asked for by name (beats=True)


def no_gap(seg):
    """A segment without the rejoin-gap waits, for tests about something else that open soon after a Close."""
    return [c for c in seg if c[0] != "rejoin-gap"]


def segment(calls, name, beats=False):
    start = next(i for i, c in enumerate(calls) if c == ["--", name])
    end = next((i for i, c in enumerate(calls[start + 1:], start + 1) if c[0] == "--"), len(calls))
    return [c for c in calls[start + 1:end] if beats or c[0] not in BEAT_CALLS]


def key(n):
    """The synthetic stream secret the stub panel hands out on its n-th token (64 characters, like the robot's)."""
    return "SYNTHETICSECRET" + str(n).rjust(64 - len("SYNTHETICSECRET"), "0")


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
    assert enc[1:] == ["aes-128-gcm2", "groundskeeper-placeholder", "1,2,3"]   # the placeholder; the salt as bytes
    assert ["setClientRole", "host"] in seg
    assert "enableLogUpload" not in names


@needs_node
def test_the_system_check_skips_the_camera_and_microphone_check():
    # Over plain http (not a secure context) the browser hides navigator.mediaDevices, so the SDK's full check fails
    # though watching works. The tile asks without the device check.
    assert ["checkSystemRequirements", False] in segment(run_flow(), "open")


@needs_node
def test_the_robots_video_is_subscribed_to():
    assert ["subscribe", 1, "video"] in run_flow()


@needs_node
def test_changing_camera_leaves_the_channel_and_switches_the_cameras_on_again():
    # The robot stops publishing when the last broadcaster leaves, so the new channel needs camera-on again.
    seg = segment(run_flow(), "pick-left")
    assert "leave" in [c[0] for c in seg]
    posts = [c for c in seg if c[0] == "post"]
    assert [p[1] for p in posts] == ["/api/control/send", "/api/video/token"]
    assert json.loads(posts[0][2]) == {"cmd": "smart_vision_control", "args": {}, "confirm": True}
    assert json.loads(posts[1][2]) == {"camera": "left"}
    names = [c[0] for c in seg]
    # The robot stops a moment after the last viewer leaves: camera-on waits out a gap after the leave, or it is lost.
    assert names.index("leave") < names.index("rejoin-gap") < names.index("post")


@needs_node
def test_a_first_open_does_not_wait_for_the_rejoin_gap():
    calls = run_flow()
    assert "rejoin-gap" not in [c[0] for c in segment(calls, "open")]
    assert "rejoin-gap" not in [c[0] for c in segment(calls, "open-long-after")]


@needs_node
def test_opening_again_soon_after_a_close_waits_out_the_gap():
    # Close left the channel; the robot may still be shutting its video down, so camera-on waits like a change does.
    names = [c[0] for c in segment(run_flow(), "reopen-soon")]
    assert names.count("rejoin-gap") == 1 and names.index("rejoin-gap") < names.index("post")


@needs_node
def test_a_second_quick_camera_change_still_waits_out_the_gap():
    # Left then Right at once: the Right start finds no client (Left's start already left it) but still waits out the
    # gap from that leave. The Left start is superseded before its gap, so one gap, and it is Right's.
    seg = segment(run_flow(), "quick-picks")
    names = [c[0] for c in seg]
    assert names.count("rejoin-gap") == 1 and names.index("leave") < names.index("rejoin-gap")
    posts = [json.loads(c[2]) for c in seg if c[0] == "post" and c[1] == "/api/video/token"]
    assert posts == [{"camera": "right"}]                      # the Left start was superseded before it asked
    assert names.index("rejoin-gap") < names.index("post")


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
    seg = segment(calls, "join-abort-error")                  # a numeric DOMException code is not an Agora code
    assert ["phase", "error", "Joining failed: AbortError"] in seg
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
    seg = no_gap(segment(run_flow(), "control-just-on"))
    assert seg[0][:2] == ["post", "/api/control/send"]


@needs_node
def test_renewing_the_token_rejoins_with_camera_on_again():
    # The rejoin leaves the channel, which stops the robot's video, so camera-on goes with it (Control on).
    calls = run_flow()
    for name in ("renew-will-expire", "renew-did-expire"):                # both events rejoin with a new token
        seg = segment(calls, name)
        assert [c[1] for c in seg if c[0] == "post"] == ["/api/control/send", "/api/video/token"], name
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
    assert phases == [["playing", ""], ["playing", ""], ["playing", ""],
                      ["waiting", "The robot stopped sending video."]]
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
    own = [c[1:] for c in segment(calls, "own-leave") if c[0] == "phase"]
    assert own == [["waiting", "Waiting for the robot's video…"]]
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


# -- the keep-awake (the robot streams only while someone sends it) ------------------------------------------------
@needs_node
def test_the_first_beat_goes_before_camera_on_and_then_one_every_four_seconds():
    seg = no_gap(segment(run_flow(), "beats-open", beats=True))
    assert seg[0] == ["interval", 4000] and seg[1] == ["beat", '{"start":true}']    # at once: a press opens the window
    assert seg[2][:2] == ["post", "/api/control/send"]                           # ... before camera-on
    assert [c[0] for c in seg].count("beat") == 3                                # 1 at the start, then one per tick
    assert ["intervals", 1] in seg
    assert [c for c in seg if c[0] == "beat"][1:] == [["beat", "{}"]] * 2        # then nothing in the request body


@needs_node
def test_a_token_renewal_does_not_open_a_new_window():
    seg = segment(run_flow(), "beat-renew", beats=True)
    assert [c for c in seg if c[0] == "beat"][0] == ["beat", "{}"]


@needs_node
def test_a_start_beat_that_fails_asks_for_the_window_again():
    seg = segment(run_flow(), "beat-start-fails", beats=True)
    assert [c for c in seg if c[0] == "beat"] == [["beat", '{"start":true}'], ["beat", '{"start":true}']]


@needs_node
def test_a_beat_in_flight_is_not_asked_again_on_top():
    seg = segment(run_flow(), "beat-in-flight", beats=True)
    held = [c[0] for c in seg][:[c[0] for c in seg].index("clear-interval")]   # until the tile is closed
    assert held.count("beat") == 1


@needs_node
def test_when_the_panel_ends_the_window_the_tile_closes_says_so_and_beats_no_more():
    calls = run_flow()
    assert next(c for c in calls if c[0] == "after-ended") == ["after-ended", False, "closed",
                                                               "Stopped. Open to watch again.", 0]
    assert next(c for c in calls if c[0] == "beats-after-ended") == ["beats-after-ended", 0]


@needs_node
def test_changing_camera_restarts_the_beats_without_a_second_timer():
    seg = segment(run_flow(), "beats-pick", beats=True)
    assert [c[0] for c in seg if c[0] in BEAT_CALLS] == ["clear-interval", "clear-interval", "interval", "beat"]   # the second clear is the stats timer (#53), playing before the pick
    assert ["beat", '{"start":true}'] in seg                                    # picking a camera is a press too
    assert ["intervals", 1] in seg


@needs_node
@pytest.mark.parametrize("how", ["close", "hidden", "pagehide", "leave-live", "crypt-error", "connection-lost",
                                 "settings-off", "join-error", "token-error"])
def test_every_way_of_stopping_stops_the_beats(how):
    stopped = next(c for c in run_flow() if c[:2] == ["stopped", how])
    assert stopped[2:] == [0, 0]                    # no beat after it, and no timer left


@needs_node
def test_try_again_beats_again_and_close_stops_it():
    seg = no_gap(segment(run_flow(), "try-again", beats=True))
    assert [c[0] for c in seg][:2] == ["interval", "beat"]
    assert [c for c in seg if c[0] == "intervals"] == [["intervals", 1], ["intervals", 0]]


@needs_node
def test_a_beat_in_flight_when_the_tile_closes_shows_nothing():
    assert next(c for c in segment(run_flow(), "beat-after-close") if c[0] == "hint") == ["hint", ""]


@needs_node
def test_a_failed_beat_does_not_break_playback_and_says_nothing():
    seg = segment(run_flow(), "beat-fails")
    assert ["phase", "playing", ""] in seg and ["hint", ""] in seg


@needs_node
def test_control_off_from_the_server_shows_the_existing_hint_even_after_the_video_starts_waiting():
    seg = segment(run_flow(), "beat-control-off")
    hint = next(c for c in seg if c[0] == "hint")
    assert "Control is off" in hint[1]
    assert ["phase", "waiting", "Waiting for the robot's video…"] in seg
    assert ["hint-after-control-on", ""] in seg                        # Control back on: that hint goes ...
    assert ["hint-unrelated", "something else"] in seg                 # ... and only that one


# -- the scoped secret swap (a stopgap, AgoraIO/agora-rtc-web#15) ---------------------------------------------------
@needs_node
def test_the_real_secret_goes_into_the_join_frame_and_into_a_rejoin_while_the_client_lives_and_is_removed_on_leave():
    seg = segment(run_flow(), "swap")
    n = next(i for i, c in enumerate(seg) if c[0] == "join")
    ws = [c for c in seg[n:] if c[0] == "ws-send"]
    during, after = ws[:4], ws[4:]
    # anything else untouched
    assert during[:3] == [["ws-send", "ping"], ["ws-send", "<binary>"], ["ws-send", '{"x":1}']]
    message = json.loads(during[3][1])["_message"]
    assert re.fullmatch(r"SYNTHETICSECRET0+\d+", message["aes_secret"]) and len(message["aes_secret"]) == 64
    assert "aes_encrypt" not in message
    assert message["aes_mode"] == "aes-128-gcm2" and message["aes_salt"] == "AQID"
    assert '"ts":1731234567890123456' in during[3][1]                                               # nothing re-rounded
    xhr = [c for c in seg[n:] if c[0] == "xhr-send"]
    assert json.loads(xhr[0][1]) == {"aesMode": "aes-128-gcm2", "aesSecret": message["aes_secret"], "uid": 5}
    # Still installed after join (the SDK sends join_v3 again when its signalling reconnects): that frame, as the SDK
    # makes it (wrapped, with its flag), gets the real secret; a frame with no secret field (rejoin_v3) is left alone.
    restored = [c for c in seg if c[0] == "restored"]
    assert restored == [["restored", False, False], ["restored", True, True]]       # in place, then gone on leave
    again = json.loads(after[0][1])["_message"]
    assert again["aes_secret"] == message["aes_secret"] and "aes_encrypt" not in again
    assert after[1] == ["ws-send", '{"_id":9,"_type":"rejoin_v3","_message":{"cid":1,"uid":2}}']
    assert json.loads(xhr[1][1]) == {"aesSecret": message["aes_secret"]}


@needs_node
def test_only_agoras_own_connections_are_touched():
    seg = segment(run_flow(), "swap-scope")
    sends = [c for c in seg if c[0] in ("ws-send", "xhr-send")]
    assert "SYNTHETICSECRET" in sends[3][1] and "SYNTHETICSECRET" in sends[4][1]   # the SDK's join and access point
    others = sends[5:]
    assert len(others) == 9 and not any("SYNTHETICSECRET" in c[1] for c in others)
    assert all("WRAPPED-BY-THE-SDK" in c[1] for c in others if c[0] == "ws-send")    # as written, flag and all


@needs_node
def test_the_swap_handles_camel_case_names_and_leaves_no_wrapper_behind():
    seg = segment(run_flow(), "swap-camel")
    text = next(c[1] for c in seg if c[0] == "ws-send" and "aesSecret" in c[1])
    assert json.loads(text)["_message"]["aesSecret"].startswith("SYNTHETICSECRET") and "aesEncrypt" not in text
    assert ["restored", True, True] in seg


@needs_node
@pytest.mark.parametrize("name", ["swap-changed", "swap-none"])
def test_a_join_message_without_a_secret_field_fails_visibly_and_restores(name):
    seg = segment(run_flow(), name)
    assert ["phase", "error", "Joining failed: video library changed"] in seg
    assert ["restored", True, True] in seg


@needs_node
def test_a_failed_join_restores_and_shows_the_joins_own_error():
    seg = segment(run_flow(), "swap-join-fails")
    assert ["phase", "error", "Joining failed: NetworkError"] in seg and ["restored", True, True] in seg


@needs_node
def test_a_join_that_never_settles_times_out_and_restores():
    seg = segment(run_flow(), "swap-hangs")
    assert ["restored", True, True] in seg and ["phase", "error", "Joining failed: The join timed out."] in seg


@needs_node
def test_overlapping_starts_never_share_a_wrapper_or_a_secret():
    calls = run_flow()
    first, held = segment(calls, "swap-overlap"), segment(calls, "swap-overlap-held")
    assert [c[0] for c in first].count("join") == 1                              # A joins and is held there
    assert [c[0] for c in held][:2] == ["intervals", "join"]                    # B's join waited until A let go
    used = [re.search(r"SYNTHETICSECRET0+\d+", c[1]).group(0) for c in first + held
            if c[0] == "ws-send" and "aes_secret" in c[1]]
    assert used == [key(1), key(2), key(2)]                        # each join carried its own start's secret
    assert ["restored", False, False] in held and ["restored", True, True] in held       # one wrapper, gone on leave
    again = [json.loads(c[1])["_message"]["aes_secret"] for c in held if c[0] == "ws-send" and "_type\":\"join" in c[1]]
    assert again == [key(2), key(2)]                               # B's reconnect carries B's secret


@needs_node
def test_the_real_secret_is_never_kept_on_cams_or_in_storage():
    assert next(c for c in run_flow() if c[0] == "leak") == ["leak", False, False, False]


def test_the_source_gives_the_sdk_only_the_placeholder_and_uses_the_secret_in_one_place():
    assert 'CAM_PLACEHOLDER_SECRET = "groundskeeper-placeholder"' in CODE
    assert re.search(r'setEncryptionConfig\("aes-128-gcm2", CAM_PLACEHOLDER_SECRET,', CODE)
    assert CODE.count("join.key") == 1 and "camerasJoin(client, [join.app_id" in CODE
    assert not re.search(r"join\.key[^;]*(cams|localStorage|sessionStorage|console|location|document|textContent)",
                         CODE)
    assert not re.search(r"\bsecret\b[^;]*(cams\.|localStorage|sessionStorage|console|location\.)", CODE)


def test_the_swap_is_one_wrapper_scoped_to_agora_and_removed_on_leave_and_when_the_join_fails():
    body = CODE[CODE.index("function camerasSwapInstall"):]
    body = body[:body.index("let camJoinTail")]
    assert ".prototype" not in CODE                                            # no prototype is ever patched
    assert body.count("globalThis.WebSocket = ") == 2 and body.count("globalThis.XMLHttpRequest = ") == 2
    assert "if (!camerasAgoraUrl(url)) return;" in body and "this.#agora = camerasAgoraUrl(url);" in body
    assert 'CAM_AGORA_HOSTS = [".agora.io", ".sd-rtn.com"]' in CODE
    assert re.search(r'u\.protocol === "wss:" \|\| u\.protocol === "https:"', CODE)
    assert body.startswith("function camerasSwapInstall(secret) {\n  if (camSwapRemove) camSwapRemove();")   # never two
    assert re.search(r"function camerasLeave\(\) \{[^}]*camSwapRemove\(\)", CODE, re.S)
    assert "Promise.race([client.join(" in CODE


def test_the_beat_has_one_route_and_is_cleared_when_leaving():
    assert CODE.count("/api/video/keepalive") == 1
    assert re.search(r"function camerasLeave\(\) \{[^}]*clearInterval\(cams\.beatTimer\)", CODE, re.S)
    assert "CAM_BEAT_MS = 4000" in CODE


@needs_node
def test_the_swap_helpers_clean_up_after_themselves_without_the_caller():
    calls = run_flow()
    start = next(i for i, c in enumerate(calls) if c == ["--", "direct-join-fails"])
    seg = [c for c in calls[start:] if c[0] in ("direct", "restored")]
    assert seg == [["direct", "boom"], ["restored", True, True],                  # a failed join: its own removal
                   ["direct", "video library changed"], ["restored", True, True],  # no secret field: its own removal
                   ["direct", "joined"], ["restored", False, False],               # success: kept for the client's life
                   ["restored", True, True]]                                       # camerasLeave() removes it


@needs_node
def test_a_wrapper_that_cannot_be_restored_goes_quiet_once_removed():
    calls = run_flow()
    seg = segment(calls, "direct-wrapped-over")
    frames = [c for c in seg if c[0] == "ws-send"]
    assert len(frames) == 1 and "WRAPPED-BY-THE-SDK" in frames[0][1] and "OVERSECRET" not in frames[0][1]


@needs_node
def test_over_plain_http_the_sdks_wrap_gets_a_stand_in_only_while_the_client_lives():
    calls = run_flow()
    seg = segment(calls, "http-join")
    assert ["wrapped", 128] in seg                                       # the join got past the wrap
    assert ["subtle-while-joined", "object"] in seg
    refused = [c for c in seg if c[0] == "crypto-call"]
    assert refused == [["crypto-call", n, "This needs the panel over https."]
                       for n in ("digest", "aes-import", "rsa-encrypt-other-key")]   # nothing but the wrap
    assert ["subtle-after-leave", "undefined", False] in segment(calls, "http-left")


@needs_node
def test_over_https_the_browsers_own_web_crypto_is_used_and_never_replaced():
    seg = segment(run_flow(), "https-join")
    names = [c[0] for c in seg]
    assert "browser-importKey" in names and "browser-encrypt" in names
    assert seg.count(["subtle-is-browsers", True]) == 2                  # while joined, and after leaving


def test_the_tile_joins_as_host_but_never_publishes_anything():
    # Host so the robot sees a viewer; nothing from this browser ever goes into the channel.
    assert 'setClientRole("host")' in CODE and 'setClientRole("audience")' not in CODE
    for call in ("publish(", "createMicrophoneAudioTrack", "createCameraVideoTrack", "createScreenVideoTrack",
                 "createCustomAudioTrack", "createCustomVideoTrack", "createBufferSourceAudioTrack", "getUserMedia"):
        assert call not in CODE, call


# --- Video stats under the tile (#53, step 1): the SDK's own local counters, read here, sent nowhere. ---

def stats_lines(calls):
    return {c[1]: (c[2], c[3]) for c in calls if c[0] == "stats"}


@needs_node
def test_the_stats_line_formats_the_sdks_counters():
    lines = stats_lines(run_flow())
    assert lines["healthy"] == ("640×480 · 15 fps · 1.2 Mbps · 2% loss · RTT 180 ms", False)
    assert lines["delay"][0].endswith("RTT 180 ms · delay 320 ms")
    assert lines["kbps"][0] == "640×480 · 15 fps · 640 kbps · <1% loss · RTT 180 ms"
    assert lines["no-rtt"][0] == "640×480 · 15 fps · 1.2 Mbps · 2% loss"


@needs_node
def test_a_weak_link_is_said_when_loss_freezes_or_the_quality_is_poor():
    lines = stats_lines(run_flow())
    assert lines["loss"] == ("640×480 · 15 fps · 1.2 Mbps · 8% loss · RTT 180 ms · weak link", True)
    assert lines["freeze"] == ("640×480 · 15 fps · 1.2 Mbps · 2% loss · RTT 180 ms · froze 6 s · weak link", True)
    assert lines["quality"] == ("640×480 · 15 fps · 1.2 Mbps · 2% loss · RTT 180 ms · weak link", True)
    assert lines["downlink-ignored"] == ("640×480 · 15 fps · 1.2 Mbps · 2% loss · RTT 180 ms", False)    # only the robot's uplink counts
    assert lines["quality-unknown"][1] is False and "weak" not in lines["quality-unknown"][0]    # 0 means unknown, not poor


@needs_node
def test_a_slow_browser_is_told_apart_from_a_weak_link():
    text, weak = stats_lines(run_flow())["slow-browser"]
    assert text.endswith("slow to draw") and "weak link" not in text and weak is False
    stalled = stats_lines(run_flow())["render-stalled"]
    assert stalled == ("640×480 · 0 fps · 1.2 Mbps · 2% loss · RTT 180 ms · slow to draw", False)   # a stalled renderer is not masked
    assert stats_lines(run_flow())["render-missing"][0].startswith("640×480 · 12 fps")                  # no render counter: use the receive rate


@needs_node
def test_no_numbers_means_no_line_and_a_throwing_sdk_is_quiet():
    calls = run_flow()
    assert next(c for c in calls if c[0] == "stats-before") == ["stats-before", ""]
    lines = stats_lines(calls)
    for name in ("nothing-yet", "no-user", "bad-numbers", "throws"):
        assert lines[name] == ("", False), name


@needs_node
def test_the_stats_line_never_carries_a_secret_channel_or_uid():
    calls = run_flow()
    assert next(c for c in calls if c[0] == "stats-line-secret-free")[1] is True
    assert not re.search(r"console\.", CODE)


@needs_node
def test_the_stats_timer_stops_on_every_way_of_stopping():
    stopped = [c for c in run_flow() if c[0] == "stats-stopped"]
    assert [c[1] for c in stopped] == ["close", "hidden", "pagehide", "leave-live", "connection-lost", "robot-stops-video"]
    for _, name, shown, after, reads, timers in stopped:
        assert shown.startswith("640×480") and after == "" and reads == 0, name
        assert timers == (1 if name == "robot-stops-video" else 0), name      # that one stays open: the keep-awake beat goes on


@needs_node
def test_changing_camera_clears_the_line_and_never_doubles_the_timer():
    calls = run_flow()
    assert next(c for c in calls if c[0] == "stats-after-pick") == ["stats-after-pick", "", 1]   # only the keep-awake beat is left
    assert next(c for c in calls if c[0] == "stats-timers") == ["stats-timers", 2]               # beat + one stats timer


def test_the_stats_source_reads_only_local_sdk_counters_every_two_seconds():
    assert "CAM_STATS_MS = 2000" in CODE
    for call in ("getRemoteVideoStats", "getRTCStats", "getRemoteNetworkQuality"):
        assert CODE.count(f".{call}(") == 1
    assert re.search(r"function camerasLeave\(\) \{[^}]*clearInterval\(cams\.statsTimer\)", CODE, re.S)
