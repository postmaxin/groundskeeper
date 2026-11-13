"""The optional BLE route behind RobotLink (ADR 0012): off unless the setting AND a connected adapter are there, only
run commands and reads, never a map write, cmd_vel or the cloud-video commands, one route per send. A fake adapter
stands in for the device and a scripted robot answers through it: no Bluetooth, no robot, synthetic data."""
import json
import threading

import pytest

from app import ble, ble_packet, control, link, mapwrite
from app.commands import ReadOnlyViolation
from app.link import NoRoute, RobotLink
from app.recorder import COUNTED_OWN

SN = "YB0000DEMO0001"


class FakeBleAdapter(ble.BleAdapter):
    """Loops every written Packet back through a scripted robot. `replies` maps a topic to the body it answers with."""

    def __init__(self, mtu=200, connected=True, replies=None):
        self._mtu, self._up, self.replies = mtu, connected, replies or {}
        self.writes, self.heard, self._cb = [], ble_packet.Reassembler(), None
        self.fail_after = None

    @property
    def mtu(self):
        return self._mtu

    @property
    def is_connected(self):
        return self._up

    async def connect(self, name_prefix):
        self._up = True

    async def disconnect(self):
        self._up = False

    def set_notify_callback(self, callback):
        self._cb = callback

    async def write(self, data):
        if self.fail_after is not None and len(self.writes) >= self.fail_after:
            raise OSError("link lost")
        self.writes.append(bytes(data))
        msg = self.heard.feed(data)
        if msg and msg.topic in self.replies:                        # the robot answers, in as many Packets as needed
            for pkt in ble_packet.encode_message(msg.topic, self.replies[msg.topic], self._mtu):
                self._cb(pkt)

    def sent_topics(self):
        r, out = ble_packet.Reassembler(), []
        for w in self.writes:
            m = r.feed(w)
            if m:
                out.append((m.topic, json.loads(m.body)))
        return out


class FakeCloud:
    def __init__(self, state="connected"):
        self.state, self.connected, self.sent = state, state == "connected", []

    def publish(self, cmd, payload):
        self.sent.append(cmd)


class Rec:
    app_stream_at = 0.0


@pytest.fixture
def rig():
    made = []

    def make(cloud="signed out", enabled=True, connected=True, replies=None, adapter=True):
        got, marked = [], []
        ad = FakeBleAdapter(connected=connected, replies=replies)
        tr = ble.BleTransport(ad, lambda t, d, ts: got.append((t, d)), lambda: SN) if adapter else None
        made.append(tr)
        if tr and connected:
            tr.connect("Yarbo-test")                  # binds the link to SN, as Ops will
        cl = FakeCloud(cloud)
        lk = RobotLink(Rec(), cl, None, mark_sent=marked.append, ble=tr, ble_enabled=lambda: enabled)
        return lk, cl, ad, got, marked

    yield make
    for t in made:
        if t:
            t.close()


def test_a_run_command_goes_over_ble_when_enabled_and_connected(rig):
    lk, cl, ad, got, marked = rig()
    assert lk.command("pause", {}) == "ble"
    assert ad.sent_topics() == [("pause", {})] and cl.sent == [] and marked == ["pause"]
    assert lk.last_command["route"] == "ble" and lk.state == "connected" and lk.status()["ble"] is True


def test_a_read_goes_over_ble_and_its_reply_reassembles_into_the_normal_message_path(rig):
    reply = json.dumps({"topic": "get_map", "data": {"x": "y" * 1500}}).encode()      # many Packets at this MTU
    lk, cl, ad, got, _ = rig(replies={"get_map": reply})
    assert lk.read("get_map") == "ble"
    assert len(ad.writes) == 1 and cl.sent == []
    assert got == [(f"snowbot/{SN}/device/get_map", {"topic": "get_map", "data": {"x": "y" * 1500}})]


def test_a_long_command_is_chunked_with_the_codec_at_the_adapters_mtu(rig):
    lk, cl, ad, *_ = rig()
    ad._mtu = 130                                          # 10 bytes of body per Packet
    lk.command("start_plan", {"id": 12345, "note": "z" * 50})
    assert len(ad.writes) > 1 and ad.sent_topics() == [("start_plan", {"id": 12345, "note": "z" * 50})]


def test_nothing_goes_by_ble_with_the_setting_off_which_is_the_default(rig):
    lk, cl, ad, *_ = rig(enabled=False)
    for send in (lambda: lk.command("pause", {}), lambda: lk.read("get_map")):
        with pytest.raises(NoRoute):
            send()
    assert ad.writes == [] and lk.read_route() is None and lk.status()["ble"] is False
    plain = RobotLink(Rec(), FakeCloud("signed out"), None)             # no transport at all: the same as before
    assert plain.ble_up() is False and "ble" not in plain.status()
    with pytest.raises(NoRoute):
        plain.command("pause", {})


def test_the_setting_alone_is_not_enough(rig):
    lk, *_ = rig(adapter=False)
    with pytest.raises(NoRoute):
        lk.command("pause", {})
    lk2, cl2, ad2, *_ = rig(connected=False)
    with pytest.raises(NoRoute):
        lk2.read("get_map")
    assert ad2.writes == [] and lk2.ble_up() is False


def test_the_setting_must_be_exactly_true(rig):
    lk, *_ = rig(enabled="yes")
    assert lk.ble_up() is False


@pytest.mark.parametrize("cmd", sorted(control.HARD_BLOCKED | {"cmd_vel", "cmd_roller"}))
def test_blocked_commands_are_refused_the_same_with_ble_on(rig, cmd):
    lk, cl, ad, *_ = rig()
    for door in (lk.command, lk.write_command):
        with pytest.raises(ReadOnlyViolation):
            door(cmd, {})
    with pytest.raises(ReadOnlyViolation):
        lk.read(cmd)
    assert ad.writes == [] and cl.sent == []


def test_the_transport_itself_refuses_what_the_link_would_never_hand_it(rig):
    lk, cl, ad, *_ = rig()
    for cmd in ("cmd_vel", "save_nogozone", "smart_vision_control", "preview_snowbot_area_path", "made_up"):
        with pytest.raises(ConnectionError):
            lk.ble.publish(cmd, {})
    assert ad.writes == []


def test_a_map_write_never_goes_by_ble_even_with_it_on_and_the_cloud_down(rig):
    lk, cl, ad, *_ = rig(cloud="signed out")
    assert lk.write_route() is None
    for cmd in sorted(mapwrite.WRITE_COMMANDS):
        with pytest.raises(NoRoute):
            lk.write_command(cmd, {"id": 1})
    assert ad.writes == [] and cl.sent == [] and lk.why_no_writes()


def test_a_map_write_stays_on_the_cloud_when_both_are_up(rig):
    lk, cl, ad, *_ = rig(cloud="connected")
    assert lk.write_command("save_nogozone", {"id": 1}) == "cloud"
    assert cl.sent == ["save_nogozone"] and ad.writes == []
    assert lk.write_command("save_nogozone", {"id": 1}, route="cloud") == "cloud"
    with pytest.raises(NoRoute):
        lk.write_command("save_nogozone", {"id": 1}, route="ble")


def test_the_cloud_video_commands_and_the_unencoded_preview_stay_off_ble(rig):
    lk, cl, ad, *_ = rig()
    for cmd in ("smart_vision_control", "set_working_state"):
        with pytest.raises(NoRoute):
            lk.command(cmd, {"state": 1})
    with pytest.raises(NoRoute):
        lk.read("preview_snowbot_area_path")
    assert ad.writes == []
    # what the own-echo accounting counts, and what only goes as {"state": 1}, can never be sent by BLE
    assert COUNTED_OWN <= ble.NEVER_ON_BLE and link.ON_ONLY <= ble.NEVER_ON_BLE


def test_with_both_routes_up_the_cloud_carries_it_and_ble_gets_nothing(rig):
    lk, cl, ad, _, marked = rig(cloud="connected")
    assert lk.command("pause", {}) == "cloud" and lk.read("get_map") == "cloud"
    assert cl.sent == ["pause", "get_map"] and ad.writes == [] and marked == ["pause", "get_map"]


def test_a_send_that_fails_on_ble_is_not_repeated_on_the_cloud_and_takes_its_mark_back():
    undone, ad = [], FakeBleAdapter()
    tr = ble.BleTransport(ad, lambda *a: None, lambda: SN)
    tr.connect("Yarbo-test")
    try:
        cl = FakeCloud("signed out")
        lk = RobotLink(Rec(), cl, None, mark_sent=lambda c: (lambda: undone.append(c)), ble=tr,
                       ble_enabled=lambda: True)
        ad.fail_after = 0
        with pytest.raises(ConnectionError):
            lk.command("pause", {})
        cl.state, cl.connected = "connected", True            # the cloud returns: nothing repeats it by itself
        assert cl.sent == [] and undone == ["pause"] and lk.last_command is None
    finally:
        tr.close()


def test_one_command_is_one_send_with_one_mark_whatever_the_route(rig):
    lk, cl, ad, _, marked = rig(cloud="signed out")
    lk.command("pause", {})
    lk.read("get_device_msg")
    assert marked == ["pause", "get_device_msg"] and len(ad.sent_topics()) == 2 and cl.sent == []


def test_an_incoming_message_without_a_serial_or_with_junk_is_dropped(rig):
    lk, cl, ad, got, _ = rig()
    lk.ble._sn = lambda: None
    for pkt in ble_packet.encode_message("get_map", b"{}", 200):
        lk.ble._notified(pkt)
    lk.ble._sn = lambda: SN
    lk.ble._notified(b"\xff\xff not a packet")
    assert got == []


def test_the_setting_is_off_by_default_and_switched_on_by_a_person(tmp_path, monkeypatch):
    import asyncio

    from fastapi import HTTPException
    from app import main
    from tests.test_settings_api import fake_request
    assert main.SETTINGS_DEFAULTS["ble_enabled"] is False
    monkeypatch.setattr(main.auth_mod, "disabled", lambda: True)    # the sign-in gate is tested in test_auth
    req, r, _ = fake_request(tmp_path)
    with pytest.raises(HTTPException):
        asyncio.run(main.put_setting(req, "ble_enabled", "yes"))
    assert asyncio.run(main.put_setting(req, "ble_enabled", True)) == {"ble_enabled": True}
    assert r.store.setting("ble_enabled") is True


def test_the_transport_runs_its_adapter_off_the_callers_thread(rig):
    lk, cl, ad, *_ = rig()
    seen = []
    orig = ad.write

    async def write(data):
        seen.append(threading.current_thread().name)
        await orig(data)

    ad.write = write
    lk.command("pause", {})
    assert seen == ["yarbo-ble"]


def test_a_link_made_for_one_robot_is_no_route_once_another_is_selected(rig):
    lk, cl, ad, got, _ = rig(replies={"get_map": b"{}"})
    serial = [SN]
    lk.ble._sn = lambda: serial[0]
    assert lk.command("pause", {}) == "ble"
    serial[0] = "YB0000DEMO0002"                              # the selection changed; the link did not
    assert lk.ble_up() is False
    with pytest.raises(NoRoute):
        lk.command("pause", {})
    n = len(ad.writes)
    for pkt in ble_packet.encode_message("get_map", b"{}", 200):
        lk.ble._notified(pkt)
    assert len(ad.writes) == n and got == []


def test_a_timed_out_write_may_have_gone_so_nothing_else_is_sent_until_reconnect(rig, monkeypatch):
    lk, cl, ad, got, marked = rig()
    monkeypatch.setattr(ble, "SEND_TIMEOUT_S", 0.2)
    started = []

    async def hang(data):
        started.append(1)
        await __import__("asyncio").sleep(30)

    real, ad.write = ad.write, hang
    with pytest.raises(ConnectionError, match="may or may not"):
        lk.command("pause", {})
    assert started == [1] and lk.ble_up() is False and marked == ["pause"]       # the mark stays: it may arrive
    ad.write = real
    with pytest.raises(NoRoute):
        lk.command("pause", {})                                # nothing overlaps the one that may still be out
    lk.ble.connect("Yarbo-test")                               # a fresh link clears it
    assert lk.command("pause", {}) == "ble"


def test_a_compressed_body_over_the_limit_is_dropped_not_inflated_again(rig, monkeypatch):
    import zlib
    lk, cl, ad, got, _ = rig()
    monkeypatch.setattr(ble_packet, "MAX_BODY", 1000)
    bomb = zlib.compress(b"[" + b"0," * 5000 + b"0]")      # inflates past the limit, so it is handed back as is
    for pkt in ble_packet.encode_message("get_map", bomb, 200):
        lk.ble._notified(pkt)
    assert got == []
