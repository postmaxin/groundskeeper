"""The robot video token (§13): shapes, the App ID from the 007 token, refresh-and-retry, and that no secret leaks
into a log record, a repr or an error. No network, no robot, no cloud: the HTTP client and the session are fakes."""
import asyncio
import base64
import json
import logging
import zlib

import pytest

from app import video
from tests.fakehttp import FakeHTTP, FakeResponse

SN = "YB0000DEMO0001"
BASE = "https://robot-service.invalid"
APP_ID = "0123456789abcdef0123456789abcdef"
KEY = "deadbeefcafe0123456789abcdef0123"
SALT = base64.b64encode(b"synthetic-salt-0123456789abcdefg").decode()
JWT = "jwt.synthetic.session"


def make_token(app_id: str = APP_ID, marker: str = "tokenbody") -> str:
    """A synthetic AccessToken2 shape: '007' + base64(zlib(signature, app id, rest)), each part length-prefixed."""
    def packed(b: bytes) -> bytes:
        return len(b).to_bytes(2, "little") + b
    raw = packed(bytes(range(32))) + packed(app_id.encode()) + packed(marker.encode())
    return "007" + base64.b64encode(zlib.compress(raw)).decode()


TOKEN = make_token()
SECRETS = (TOKEN, KEY, SALT, APP_ID, JWT, BASE, f"{SN}_middle_image")


def ok_reply(**over):
    data = {"token": TOKEN, "key": KEY, "salt": SALT}
    data.update(over)
    return FakeResponse(200, json.dumps({"code": "00000", "data": data, "message": "ok", "sign": "x"}).encode(),
                        "application/json")


def reply(status=200, body=None):
    return FakeResponse(status, json.dumps(body if body is not None else {}).encode(), "application/json")


class FakeSDK:
    def __init__(self, token=JWT):
        self.token = token


class FakeCloud:
    """The slice of CloudTransport that app.video uses: serial, session JWT, and a refresh that swaps the JWT."""
    def __init__(self, refresh_ok="ok"):
        self.sn = SN
        self.sdk = FakeSDK()
        self.refreshes = 0
        self.refresh_ok = refresh_ok

    def session_jwt(self):
        return self.sdk.token

    def refresh_session(self, stale_jwt=None):
        self.refreshes += 1
        self.sdk.token = "jwt.refreshed.session"
        return self.refresh_ok


def fetch(cloud, http, camera="front", uid=888882, base=BASE):
    return asyncio.run(video.fetch(cloud, http, base, camera, uid))


def test_a_token_is_read_with_the_documented_request():
    http = FakeHTTP(answer_post=lambda u, j, h: ok_reply())
    t = fetch(FakeCloud(), http, "front")
    (call,) = http.posts
    assert call["url"] == BASE + "/yarbo/robot-service/robot/commonUser/getAgoraToken"
    assert call["json"] == {"channel_name": f"{SN}_middle_image", "sn": SN, "uid": 888882}
    assert call["headers"]["Authorization"] == f"Bearer {JWT}"
    assert (t.camera, t.uid, t.channel) == ("front", 888882, f"{SN}_middle_image")
    assert (t.token, t.key, t.salt) == (TOKEN, KEY, SALT)
    assert t.app_id == APP_ID


def test_each_camera_has_its_own_channel():
    http = FakeHTTP(answer_post=lambda u, j, h: ok_reply())
    for cam, ch in (("left", "left"), ("right", "right"), ("back", "back")):
        assert fetch(FakeCloud(), http, cam).channel == f"{SN}_{ch}_image"


@pytest.mark.parametrize("camera,uid,base", [("up", 5, BASE), ("front", 0, BASE), ("front", 1, BASE),
                                              ("front", -4, BASE), ("front", True, BASE), ("front", 5, "")])
def test_bad_input_is_refused_before_any_request(camera, uid, base):
    http = FakeHTTP(answer_post=lambda u, j, h: ok_reply())
    with pytest.raises(video.VideoTokenError):
        fetch(FakeCloud(), http, camera, uid, base)
    assert http.posts == []


def test_the_app_id_is_found_even_when_the_layout_differs():
    odd = "007" + base64.b64encode(zlib.compress(b"\xff\xff" + APP_ID.encode() + b"tail")).decode()
    assert video.app_id_of(odd) == APP_ID
    for bad in ("006abc", "007!!!", "007" + base64.b64encode(zlib.compress(b"no id here")).decode()):
        with pytest.raises(video.VideoTokenError):
            video.app_id_of(bad)


def test_a_refusal_carries_only_its_code(caplog):
    caplog.set_level(logging.DEBUG)
    http = FakeHTTP(answer_post=lambda u, j, h: reply(200, {"code": "B1234", "message": f"bad {KEY} {JWT}"}))
    with pytest.raises(video.VideoTokenError) as e:
        fetch(FakeCloud(), http)
    assert "B1234" in str(e.value) and KEY not in str(e.value) and JWT not in str(e.value)
    assert len(http.posts) == 1                              # a plain refusal is not retried


def test_an_incomplete_reply_is_an_error():
    http = FakeHTTP(answer_post=lambda u, j, h: ok_reply(salt=""))
    with pytest.raises(video.VideoTokenError):
        fetch(FakeCloud(), http)


@pytest.mark.parametrize("first", [reply(401), reply(200, {"code": "A0230", "message": "x"})])
def test_a_refused_session_is_refreshed_and_retried_once(first):
    answers = [first, ok_reply()]
    http = FakeHTTP(answer_post=lambda u, j, h: answers.pop(0))
    cloud = FakeCloud()
    t = fetch(cloud, http)
    assert cloud.refreshes == 1 and t.app_id == APP_ID
    assert [c["headers"]["Authorization"] for c in http.posts] == [f"Bearer {JWT}", "Bearer jwt.refreshed.session"]


def test_a_second_refusal_after_the_refresh_is_final():
    http = FakeHTTP(answer_post=lambda u, j, h: reply(401))
    cloud = FakeCloud()
    with pytest.raises(video.VideoTokenError) as e:
        fetch(cloud, http)
    assert e.value.auth and cloud.refreshes == 1 and len(http.posts) == 2


def test_an_expired_session_stops_without_a_retry():
    http = FakeHTTP(answer_post=lambda u, j, h: reply(401))
    cloud = FakeCloud(refresh_ok="expired")
    with pytest.raises(video.VideoTokenError) as e:
        fetch(cloud, http)
    assert e.value.auth and len(http.posts) == 1


def test_a_network_failure_during_the_refresh_is_not_reported_as_a_refused_session():
    http = FakeHTTP(answer_post=lambda u, j, h: reply(401))
    cloud = FakeCloud(refresh_ok="unreachable")
    with pytest.raises(video.VideoTokenError) as e:
        fetch(cloud, http)
    assert not e.value.auth and "reach" in str(e.value) and len(http.posts) == 1


def test_signed_out_makes_no_request():
    http = FakeHTTP(answer_post=lambda u, j, h: ok_reply())
    cloud = FakeCloud()
    cloud.sdk.token = None
    with pytest.raises(video.VideoTokenError):
        fetch(cloud, http)
    assert http.posts == []


def test_a_network_failure_does_not_leak_the_address(caplog):
    caplog.set_level(logging.DEBUG)

    def boom(u, j, h):
        raise ConnectionError(f"cannot reach {u} with {h}")
    with pytest.raises(video.VideoTokenError) as e:
        fetch(FakeCloud(), FakeHTTP(answer_post=boom))
    text = str(e.value) + caplog.text
    assert BASE not in text and JWT not in text


def test_no_secret_appears_in_logs_repr_or_str(caplog):
    caplog.set_level(logging.DEBUG)
    answers = [reply(401), ok_reply()]                       # take the refresh path too
    t = fetch(FakeCloud(), FakeHTTP(answer_post=lambda u, j, h: answers.pop(0)))
    shown = repr(t) + str(t) + f"{t}" + f"{t!r}"
    assert str(t.uid) not in shown
    logs = "\n".join(r.getMessage() for r in caplog.records) + caplog.text
    assert "fetched for the front camera" in logs
    for secret in SECRETS + ("jwt.refreshed.session", SN):
        assert secret not in shown, secret
        assert secret not in logs, secret


def test_an_uninitialised_cloud_client_is_signed_out_and_is_not_built(tmp_path, monkeypatch):
    from app.cloud import CloudTransport
    cloud = CloudTransport(str(tmp_path), SN, lambda *a: None)
    monkeypatch.setattr(cloud, "_sdk", lambda: pytest.fail("must not build the SDK client (a network call)"))
    http = FakeHTTP(answer_post=lambda u, j, h: ok_reply())
    with pytest.raises(video.VideoTokenError) as e:
        fetch(cloud, http)
    assert e.value.auth and http.posts == []


def test_a_refresh_is_skipped_when_another_caller_already_refreshed(tmp_path):
    from app.cloud import CloudTransport

    class Auth:
        calls = 0

        def refresh(self):
            Auth.calls += 1

    class Client:
        token = "jwt.newer.session"
        _auth = Auth()
    cloud = CloudTransport(str(tmp_path), SN, lambda *a: None)
    cloud._client = Client()
    assert cloud.refresh_session("jwt.older.session") == "ok" and Auth.calls == 0   # someone got there first


def test_refresh_session_tells_a_network_failure_from_an_expired_session(tmp_path):
    from yarbo_robot_sdk.exceptions import TokenExpiredError
    from app.cloud import CloudTransport

    def make(exc):
        class Auth:
            def refresh(self):
                raise exc

        class Client:
            token = "jwt.session"
            _auth = Auth()
        cloud = CloudTransport(str(tmp_path), SN, lambda *a: None)
        cloud._client = Client()
        return cloud
    assert make(ConnectionError("down")).refresh_session() == "unreachable"
    assert make(TokenExpiredError("old")).refresh_session() == "expired"
    assert make(ConnectionError("down"))._refresh() is True        # the worker keeps its session and retries later
    assert make(TokenExpiredError("old"))._refresh() is False


def test_logout_waits_for_a_refresh_in_flight_so_the_session_file_stays_gone(tmp_path):
    import threading
    from app.cloud import CloudTransport
    inside, release = threading.Event(), threading.Event()

    class Auth:
        def refresh(self):
            inside.set()
            release.wait(5)

    class Client:
        token, refresh_token, _auth = "jwt.session", "r", Auth()
    cloud = CloudTransport(str(tmp_path), SN, lambda *a: None)
    cloud._client, cloud.username = Client(), "a@example.com"
    cloud._save()
    refresher = threading.Thread(target=cloud.refresh_session)
    refresher.start()
    assert inside.wait(5)                                   # the refresh holds the lock, about to save
    logger_out = threading.Thread(target=cloud.logout)
    logger_out.start()
    logger_out.join(0.3)                                    # without the lock it would already have unlinked
    release.set()
    refresher.join(5)
    logger_out.join(5)
    assert not refresher.is_alive() and not logger_out.is_alive()
    assert not cloud.path.exists() and cloud._client is None
