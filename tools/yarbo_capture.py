"""Listen-only protocol capture on Yarbo's cloud broker. Never publishes.

    YARBO_SN=<serial> tools/.venv/Scripts/python tools/yarbo_capture.py LABEL [MINUTES]

YARBO_SN (the robot's serial, as in its MQTT topics) is required. The robot's own broker on the home network is
closed by current firmware, so only the cloud is captured.

Subscribes to snowbot/{SN}/# on Yarbo's NEW cloud broker (saved session from yarbo_cloud.py), decodes zlib/JSON,
and appends every message to
~/.yarbo/captures/<date>_<LABEL>.jsonl (outside the workspace: payloads include the home GPS position).
Binary payloads that are not JSON are also saved as separate files (in case images ride MQTT).
Prints app/* commands and data_feedback replies in full; the DeviceMSG/heart_beat chatter is only counted.
"""
import json
import os
import secrets
import sys
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from yarbo_robot_sdk import YarboClient

SN = os.environ.get("YARBO_SN", "").strip()
if not SN:
    sys.exit("set YARBO_SN (the robot's serial) first")
SESSION = Path.home() / ".yarbo" / "session.json"
OUTDIR = Path.home() / ".yarbo" / "captures"
NOISY = ("/device/DeviceMSG", "/device/heart_beat")

label = sys.argv[1] if len(sys.argv) > 1 else "session"
minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 20
OUTDIR.mkdir(parents=True, exist_ok=True)
out_path = OUTDIR / f"{datetime.now():%Y%m%d-%H%M%S}_{label}.jsonl"
out = open(out_path, "a", encoding="utf-8")
lock = threading.Lock()
counts = {}


def decode(raw):
    try:
        return "zlib-json", json.loads(zlib.decompress(raw))
    except Exception:
        pass
    try:
        return "json", json.loads(raw)
    except Exception:
        pass
    try:
        txt = raw.decode("utf-8")
        if txt.isprintable():
            return "text", txt
    except Exception:
        pass
    return "binary", None


def on_message(broker):
    def handler(client, userdata, msg):
        ts = datetime.now().isoformat(timespec="milliseconds")
        kind, data = decode(msg.payload)
        rec = {"ts": ts, "broker": broker, "topic": msg.topic, "kind": kind, "size": len(msg.payload)}
        if kind == "binary":
            f = OUTDIR / f"{out_path.stem}_{broker}_{int(time.time()*1000)}.bin"
            f.write_bytes(msg.payload)
            rec["file"] = f.name
            rec["magic"] = msg.payload[:8].hex()
        else:
            rec["data"] = data
        with lock:
            out.write(json.dumps(rec) + "\n")
            out.flush()
            key = (broker, msg.topic)
            counts[key] = counts.get(key, 0) + 1
            if not msg.topic.endswith(NOISY):
                preview = json.dumps(data)[:1500] if kind != "binary" else f"<binary {len(msg.payload)} B magic {rec['magic']}>"
                print(f"{ts[11:]} [{broker:5}] {msg.topic}  {preview}", flush=True)
            elif counts[key] == 1:
                print(f"{ts[11:]} [{broker:5}] {msg.topic}  (first; further ones counted only)", flush=True)
    return handler


def on_connect(broker):
    def handler(client, userdata, flags, rc, props=None):
        print(f"[{broker}] CONNACK {rc}", flush=True)
        client.subscribe(f"snowbot/{SN}/#")
    return handler


def on_subscribe(broker):
    def handler(client, userdata, mid, rcs, props=None):
        print(f"[{broker}] SUBACK {[str(r) for r in rcs]}", flush=True)
    return handler


def make(broker, client_id):
    c = mqtt.Client(client_id=client_id, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    c.on_connect, c.on_message, c.on_subscribe = on_connect(broker), on_message(broker), on_subscribe(broker)
    c.reconnect_delay_set(min_delay=5, max_delay=60)
    return c


clients = []

sess = json.loads(SESSION.read_text())
yc = YarboClient()
yc.restore_session(sess["username"], sess["token"], sess["refresh_token"])
yc._auth.refresh()
sess.update(token=yc.token, refresh_token=yc.refresh_token)
SESSION.write_text(json.dumps(sess))
user = sess["username"].lower()
cld = make("cloud", f"HA_{user}_{secrets.randbelow(10000):04d}")
cld.username_pw_set(user, yc.token)
cld.tls_set()
cld.connect(yc._config.get("new_mqtt_host"), int(yc._config.get("new_mqtt_port")), 60)
clients.append(cld)

for c in clients:
    c.loop_start()
print(f"capturing '{label}' for {minutes} min -> {out_path}", flush=True)
try:
    time.sleep(minutes * 60)
finally:
    for c in clients:
        c.loop_stop()
        c.disconnect()
    out.close()
    print("\n== message counts")
    for (b, t), n in sorted(counts.items()):
        print(f"  {n:6}  [{b}] {t}")
