"""Robot video token (§13): the cloud's robot service hands out a per-camera Agora token for the signed-in account.

This only READS a token with the session the cloud client already holds. It sends nothing to the robot and opens no
video; the route, the setting and the browser player are separate pieces (§13). The response carries the stream's
encryption key and salt, which with the token give a live, decryptable view of the yard, so:

* `VideoToken` hides token, key, salt, App ID and channel from `repr` and `str`;
* nothing here logs them (or the JWT, or the robot-service address): a log line is an outcome and a camera name;
* a failure's message is a fixed phrase and, at most, the service's short error code, never its free text.

The wire format is in docs/dev/protocol.md, "Robot video (Agora)". The robot-service address is not recorded in this
repository: the caller passes it in (nothing here has a default).
"""
import asyncio
import base64
import binascii
import logging
import re
import zlib
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

PATH = "/yarbo/robot-service/robot/commonUser/getAgoraToken"
CAMERAS = {"front": "middle", "left": "left", "right": "right", "back": "back"}   # shown name -> channel name
OK_CODE = "00000"
AUTH_CODES = {"A0230"}                     # a 200 whose body says the session is no good (protocol.md)
ROBOT_UID = 1                              # the robot publishes as uid 1; a viewer must be a different, non-zero uid


class VideoTokenError(Exception):
    """A token could not be read. `str()` is safe to show and log; `auth` is True when the session was refused."""

    def __init__(self, reason: str, *, auth: bool = False):
        super().__init__(reason)
        self.auth = auth


@dataclass(frozen=True)
class VideoToken:
    camera: str
    uid: int
    channel: str = field(repr=False)
    app_id: str = field(repr=False)
    token: str = field(repr=False)
    key: str = field(repr=False)
    salt: str = field(repr=False)          # as received (base64 text); the viewer decodes it to bytes

    def __str__(self) -> str:
        return repr(self)


def app_id_of(token: str) -> str:
    """The Agora App ID inside a 007 token: 32 hex characters in the zlib-compressed body, after the signature."""
    if not token.startswith("007"):
        raise VideoTokenError("video token has an unexpected format")
    try:
        raw = zlib.decompress(base64.b64decode(token[3:], validate=False))
    except (binascii.Error, zlib.error, ValueError):
        raise VideoTokenError("video token could not be read") from None
    # Body: a length-prefixed signature, then the length-prefixed App ID. Fall back to a scan if the layout differs.
    if len(raw) >= 2:
        start = 2 + int.from_bytes(raw[:2], "little")
        m = re.fullmatch(rb"[0-9a-f]{32}", raw[start + 2:start + 34]) if len(raw) >= start + 34 else None
        if m:
            return m.group().decode()
    m = re.search(rb"[0-9a-f]{32}", raw)
    if not m:
        raise VideoTokenError("video token has no App ID")
    return m.group().decode()


async def fetch(cloud, http, base_url: str, camera: str, uid: int) -> VideoToken:
    """Ask the robot service for `camera`'s token. `cloud` is the CloudTransport (its session and serial), `http` an
    httpx.AsyncClient (or the test fake). On a 401 or the auth body code, refresh the session once and retry once."""
    if camera not in CAMERAS:
        raise VideoTokenError("unknown camera")
    if not isinstance(uid, int) or isinstance(uid, bool) or not 0 < uid < 2**32 or uid == ROBOT_UID:
        raise VideoTokenError("invalid viewer uid")
    if not base_url:
        raise VideoTokenError("no video service address is configured")
    if not cloud.sn:
        raise VideoTokenError("the robot's serial is not set")
    body = {"channel_name": f"{cloud.sn}_{CAMERAS[camera]}_image", "sn": cloud.sn, "uid": uid}
    url = base_url.rstrip("/") + PATH
    for attempt in (1, 2):
        jwt = getattr(cloud._sdk(), "token", None)
        if not jwt:
            raise VideoTokenError("not signed in to the Yarbo cloud", auth=True)
        try:
            resp = await http.post(url, json=body, headers={"Authorization": f"Bearer {jwt}"}, timeout=10)
        except Exception as exc:
            log.warning("video token: request failed (%s)", type(exc).__name__)   # the message can carry the address
            raise VideoTokenError("could not reach the video service") from None
        try:
            doc = resp.json() if resp.status_code == 200 else {}
        except ValueError:
            doc = {}
        code = str(doc.get("code", "")) if isinstance(doc, dict) else ""
        if resp.status_code == 401 or code in AUTH_CODES:
            if attempt == 1:
                log.info("video token: session refused, refreshing once")
                if not await asyncio.to_thread(cloud._refresh):
                    raise VideoTokenError("Yarbo cloud session expired; sign in again", auth=True)
                continue
            log.warning("video token: refused after a refresh")
            raise VideoTokenError("Yarbo cloud refused the session", auth=True)
        if resp.status_code != 200:
            log.warning("video token: HTTP %s", resp.status_code)
            raise VideoTokenError(f"video service answered HTTP {resp.status_code}")
        if code != OK_CODE:
            shown = code if re.fullmatch(r"[A-Za-z0-9]{1,10}", code) else "?"
            log.warning("video token: refused, code %s", shown)
            raise VideoTokenError(f"video service refused the request (code {shown})")
        data = doc.get("data")
        if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data.get(k)
                                                  for k in ("token", "key", "salt")):
            log.warning("video token: reply missing token, key or salt")
            raise VideoTokenError("video service reply was incomplete")
        tok = VideoToken(camera=camera, uid=uid, channel=body["channel_name"], app_id=app_id_of(data["token"]),
                         token=data["token"], key=data["key"], salt=data["salt"])
        log.info("video token: fetched for the %s camera", camera)
        return tok
    raise VideoTokenError("video token request failed")    # unreachable: the loop returns or raises
