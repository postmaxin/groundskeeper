"""Yarbo MQTT payloads: zlib-compressed JSON, except heart_beat, which is plain JSON."""
import base64
import json
import re
import zlib

# Fields whose values are secrets: another client's commands are logged and captured (to learn their formats), and
# e.g. the app's change_to_wifi carries the Wi-Fi password (2026-10-10). The key stays, the value is masked.
# Not plain "pass": by_pass_level and the *_bypass switches are settings, not secrets.
_SECRET = r"passw|passphrase|pwd|secret|token|psk|mail"
SECRET_KEY = re.compile(_SECRET, re.I)
MASK = "•••"
# "key": "value" for a secret key; the value may be cut short (a stored sample ends mid-string).
_SECRET_TEXT = re.compile(r'("[^"]*(?:' + _SECRET + r')[^"]*"\s*:\s*)"(?:[^"\\]|\\.)*(?:"|$)', re.I)


def redact(obj):
    """A copy with every secret-looking field's value masked (any depth)."""
    if isinstance(obj, dict):
        return {k: (MASK if isinstance(k, str) and SECRET_KEY.search(k) and v not in (None, "") else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def redact_text(text: str) -> str:
    """The same for JSON text that may be cut short (stored samples and event details)."""
    return _SECRET_TEXT.sub(lambda m: f'{m.group(1)}"{MASK}"', text) if isinstance(text, str) else text


def decode(raw: bytes):
    """Return the decoded JSON value, or None if the payload is neither zlib JSON nor JSON."""
    try:
        return json.loads(zlib.decompress(raw))
    except (zlib.error, ValueError):
        pass
    try:
        return json.loads(raw)
    except ValueError:
        return None


def encode(obj) -> bytes:
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode())


def decode_any(data):
    """A reply's `data` field in whatever form it came: a dict/list, JSON text, base64(zlib(JSON)) or base64(JSON)."""
    if isinstance(data, (dict, list)) or data is None:
        return data
    if isinstance(data, str):
        try:
            return json.loads(data)
        except ValueError:
            pass
        try:
            raw = base64.b64decode(data)
        except ValueError:
            return None
        for f in (lambda b: json.loads(zlib.decompress(b)), lambda b: json.loads(b)):
            try:
                return f(raw)
            except (zlib.error, ValueError):
                pass
    return None


# get_map_buckup_from_id returns the map with singular keys; map them to get_map's names.
BACKUP_KEYS = {"area": "areas", "nogozone": "nogozones", "novisionzone": "novisionzones", "pathway": "pathways",
               "sidewalk": "sidewalks", "deadend": "deadends", "chargingPoints": "allchargingData",
               "chargingPoint": "chargingData"}


def locate_map(obj, depth: int = 0):
    """The map dict inside a decoded reply (it may be wrapped, or itself be encoded one level down)."""
    if isinstance(obj, str) and depth < 4:
        return locate_map(decode_any(obj), depth + 1)
    if isinstance(obj, dict):
        if "areas" in obj or "nogozones" in obj:
            return obj
        if isinstance(obj.get("area"), list):  # a backup: the same map with singular keys
            return {BACKUP_KEYS.get(k, k): v for k, v in obj.items()}
        if depth < 4:
            for v in obj.values():
                found = locate_map(v, depth + 1)
                if found is not None:
                    return found
    return None


def decode_map(data):
    """get_map's `data` arrives as base64(zlib(JSON)); accept an already-decoded dict too."""
    if isinstance(data, dict):
        return data
    return json.loads(zlib.decompress(base64.b64decode(data)))
