"""Cloud sends: a publish the client refuses is an error, and the panel's own send is marked before it goes (so an echo
handled on the broker's thread before publish() returns is not taken for someone else's command). Stand-ins only."""
import time
from types import SimpleNamespace

import pytest

from app import demo_robot as dr
from app.cloud import CloudTransport
from app.config import Config
from app.link import RobotLink
from app.recorder import Recorder
from app.store import Store


class FakeMqtt:
    def __init__(self, info):
        self.info = info

    def publish(self, topic, payload):
        return self.info


def _transport(tmp_path, info) -> CloudTransport:
    t = CloudTransport(str(tmp_path), "YB0000DEMO0001", lambda *a: None)
    t._mqtt, t.connected = FakeMqtt(info), True
    return t


def _recorder(tmp_path) -> Recorder:
    async def no_imagery(*a, **k):
        return None
    cfg = Config(data_dir=str(tmp_path), port=8425, poll_idle_s=60,
                 poll_active_s=2, map_refresh_s=21600, raw_every_s=60, raw_keep_days=90, run_idle_end_s=300,
                 tempest_port=0, ha_url=None, ha_token=None, demo=True, frigate_url=None, panel_url=None)
    r = Recorder(cfg, Store(str(tmp_path)), SimpleNamespace(yard_meta=None, ensure_yard=no_imagery), None)
    r.gps_ref = dr.REF
    return r


class FakeCloud:
    """Connected; publish() runs `during` (an echo handled early) and then raises `error` if given."""
    state, connected = "connected", True

    def __init__(self, during=None, error=None):
        self.during, self.error, self.sent = during, error, []

    def publish(self, cmd, payload):
        if self.during:
            self.during(cmd)
        if self.error:
            raise self.error
        self.sent.append(cmd)


def _link(tmp_path, cloud, mark_sent=None) -> RobotLink:
    store = Store(str(tmp_path))
    return RobotLink(SimpleNamespace(transport=None), cloud, store, mark_sent=mark_sent)


def test_a_publish_the_client_refuses_is_an_error_and_not_counted(tmp_path):
    t = _transport(tmp_path, SimpleNamespace(rc=4))
    with pytest.raises(ConnectionError, match="MQTT error 4"):
        t.publish("pause", {})
    assert t.published == 0


def test_a_publish_the_client_took_is_counted(tmp_path):
    t = _transport(tmp_path, SimpleNamespace(rc=0))
    t.publish("pause", {})
    assert t.published == 1


def test_a_publish_result_without_rc_is_counted(tmp_path):
    t = _transport(tmp_path, object())
    t.publish("pause", {})
    assert t.published == 1


def test_an_echo_handled_during_the_send_sees_the_panels_mark(tmp_path):
    rec = _recorder(tmp_path)
    seen = []
    cloud = FakeCloud(during=lambda cmd: seen.append(time.time() - rec._sent.get(cmd, 0) < 5))
    _link(tmp_path, cloud, mark_sent=rec.mark_sent).command("pause", {})
    assert seen == [True] and cloud.sent == ["pause"]


def test_a_send_that_fails_takes_its_mark_back(tmp_path):
    rec = _recorder(tmp_path)
    lk = _link(tmp_path, FakeCloud(error=ConnectionError("no")), mark_sent=rec.mark_sent)
    with pytest.raises(ConnectionError, match="no"):
        lk.command("pause", {})
    assert "pause" not in rec._sent


def test_a_send_that_fails_puts_the_earlier_mark_back(tmp_path):
    rec = _recorder(tmp_path)
    rec._sent["pause"] = 123.0
    lk = _link(tmp_path, FakeCloud(error=ConnectionError("no")), mark_sent=rec.mark_sent)
    with pytest.raises(ConnectionError):
        lk.command("pause", {})
    assert rec._sent["pause"] == 123.0


@pytest.mark.parametrize("mark", [None, lambda cmd: None])
def test_a_mark_that_returns_nothing_still_works(tmp_path, mark):
    cloud = FakeCloud()
    _link(tmp_path, cloud, mark_sent=mark).command("pause", {})
    assert cloud.sent == ["pause"]
    failing = FakeCloud(error=ConnectionError("no"))
    with pytest.raises(ConnectionError):
        _link(tmp_path, failing, mark_sent=mark).command("pause", {})


def _own_snapshot(rec):
    st = rec._own()
    return ({k: list(v) for k, v in st["beats"].items() if v}, list(st["hist"]), st["trusted"], st["last"], st["n"])


@pytest.mark.parametrize("trusted_before", [True, False])
def test_a_counted_send_that_fails_leaves_the_credits_and_trust_as_they_were(tmp_path, trusted_before):
    rec = _recorder(tmp_path)
    st = rec._own()
    st["last"] = time.time() - 3600           # a long quiet spell: the failed send's mark would start a new session
    if trusted_before:
        st["hist"].extend([True] * st["hist"].maxlen)
        st["trusted"] = True
    before = _own_snapshot(rec)
    lk = _link(tmp_path, FakeCloud(error=ConnectionError("no")), mark_sent=rec.mark_sent)
    with pytest.raises(ConnectionError):
        lk.command("set_working_state", {"state": 1})
    assert _own_snapshot(rec) == before
    assert "set_working_state" not in rec._sent


def test_a_counted_send_that_fails_keeps_the_credit_an_earlier_send_queued(tmp_path):
    rec = _recorder(tmp_path)
    rec.mark_sent("set_working_state")
    before = _own_snapshot(rec)
    lk = _link(tmp_path, FakeCloud(error=ConnectionError("no")), mark_sent=rec.mark_sent)
    with pytest.raises(ConnectionError):
        lk.command("set_working_state", {"state": 1})
    assert _own_snapshot(rec) == before
