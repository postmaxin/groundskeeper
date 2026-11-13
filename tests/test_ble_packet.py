"""The BLE Packet codec (app/ble_packet.py): framing only, no I/O.

The chunk constants (mtu - 120, or mtu when mtu <= 100) are from a static read of the app and await confirmation by
an HCI capture on a real robot; the tests pass `mtu` explicitly and never depend on a default.
"""
import json
import logging
import random
import zlib

import pytest

from app import ble_packet as bp
from app.ble_packet import PacketError, Reassembler, decode_packet, encode_message, encode_packet

JSON_BODY = json.dumps({"topic": "get_map", "items": list(range(200))}, separators=(",", ":")).encode()
# Opaque bytes standing in for a protobuf map-write body (including every byte value).
PROTO_BODY = bytes(range(256)) * 3


def feed_all(packets, rx=None):
    rx = rx or Reassembler()
    out = [m for m in (rx.feed(p) for p in packets) if m]
    return out


# -- wire format -----------------------------------------------------------------------------------------------
def test_a_packet_matches_hand_computed_wrapper_bytes():
    # total=2, index=1, topic "ab", timestamp 300 (varint ac 02), data "hi"; each of fields 1-4 is a wrapper message
    golden = bytes.fromhex("0a020802" "12020801" "1a040a026162" "220308ac02" "2a026869")
    assert encode_packet(2, 1, "ab", 300, b"hi") == golden
    assert decode_packet(golden) == bp.Packet(2, 1, "ab", 300, b"hi")


def test_a_large_timestamp_and_a_non_ascii_topic_round_trip():
    ts = 1_760_000_000_123
    pkt = decode_packet(encode_packet(3, 3, "état", ts, b"\x00\xff"))
    assert pkt == bp.Packet(3, 3, "état", ts, b"\x00\xff")


def test_unknown_fields_are_skipped():
    raw = encode_packet(1, 1, "t", 5, b"x") + bytes([0x32, 0x01, 0x07])  # field 6, length-delimited
    assert decode_packet(raw).data == b"x"


def test_an_empty_wrapper_reads_as_its_default():
    # topic wrapper present but empty, as protobuf writes a default string value
    raw = bytes.fromhex("0a020801" "12020801" "1a00" "2a00")
    assert decode_packet(raw) == bp.Packet(1, 1, "", 0, b"")


# -- chunking --------------------------------------------------------------------------------------------------
def test_chunk_size_rule_and_its_edges():
    assert bp.chunk_size(100) == 100  # mtu <= 100: the whole mtu
    assert bp.chunk_size(23) == 23
    assert bp.chunk_size(121) == 1  # mtu > 100: mtu - 120
    assert bp.chunk_size(185) == 65
    assert bp.chunk_size(512) == 392


@pytest.mark.parametrize("mtu", [101, 110, 120, 0, -5])
def test_an_mtu_with_no_room_is_refused(mtu):
    # 101..120 would give a zero or negative chunk by the app's rule
    with pytest.raises(ValueError):
        bp.chunk_size(mtu)
    with pytest.raises(ValueError):
        encode_message("t", b"x", mtu)


def test_a_body_is_split_into_chunk_size_pieces_with_one_based_indexes():
    pkts = [decode_packet(p) for p in encode_message("t", b"x" * 250, mtu=220)]  # chunk = 100
    assert [(p.index, p.total, len(p.data)) for p in pkts] == [(1, 3, 100), (2, 3, 100), (3, 3, 50)]
    assert len({p.timestamp for p in pkts}) == 1


def test_the_small_mtu_branch_uses_the_whole_mtu():
    pkts = [decode_packet(p) for p in encode_message("t", b"y" * 45, mtu=20)]
    assert [len(p.data) for p in pkts] == [20, 20, 5]


def test_an_empty_body_is_one_empty_packet():
    (raw,) = encode_message("ping", b"", mtu=185)
    assert decode_packet(raw) == bp.Packet(1, 1, "ping", decode_packet(raw).timestamp, b"")
    assert feed_all([raw]) == [bp.Message("ping", b"", False)]


# -- round trips -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("mtu", [20, 100, 185, 512])
@pytest.mark.parametrize("body", [JSON_BODY, PROTO_BODY, b"{}", b"x" * bp.chunk_size(185)],
                         ids=["json", "protobuf", "tiny", "one-chunk"])
def test_encode_split_reassemble_round_trip(body, mtu, compress):
    raw = encode_message("get_map", body, mtu, compress)
    (msg,) = feed_all(raw)
    assert (msg.topic, msg.body, msg.compressed) == ("get_map", body, compress)
    if not compress:
        assert len(raw) == max(1, -(-len(body) // bp.chunk_size(mtu)))


@pytest.mark.parametrize("n", [64, 65, 66, 65 * 5, 65 * 5 + 3])
def test_bodies_smaller_equal_and_several_times_the_chunk_size(n):
    body = bytes(random.Random(n).randrange(256) for _ in range(n))  # incompressible
    raw = encode_message("t", body, mtu=185)  # chunk 65
    assert len(raw) == -(-n // 65)
    assert [m.body for m in feed_all(raw)] == [body]
    assert [m.body for m in feed_all(encode_message("t", body, 185, compress=True))] == [body]


def test_equal_to_chunk_size_is_exactly_one_packet_uncompressed():
    assert len(encode_message("t", b"a" * 65, mtu=185)) == 1
    assert len(encode_message("t", b"a" * 66, mtu=185)) == 2


def test_a_json_body_survives_as_json_and_a_protobuf_body_as_opaque_bytes():
    (jmsg,) = feed_all(encode_message("get_map", JSON_BODY, 185, compress=True))
    assert json.loads(jmsg.body)["topic"] == "get_map"
    (pmsg,) = feed_all(encode_message("save_pathway", PROTO_BODY, 185))
    assert pmsg.body == PROTO_BODY and pmsg.compressed is False


def test_compression_is_detected_not_flagged():
    # the decoder is told nothing: a deflated body is inflated, a plain body is left alone
    assert feed_all(encode_message("t", JSON_BODY, 185, compress=True))[0].compressed is True
    assert feed_all(encode_message("t", JSON_BODY, 185, compress=False))[0].compressed is False


# -- reassembly ------------------------------------------------------------------------------------------------
def test_out_of_order_and_duplicate_chunks_still_reassemble_once():
    raw = encode_message("t", PROTO_BODY, mtu=185)  # 12 chunks
    order = list(range(len(raw)))
    random.Random(7).shuffle(order)
    stream = [raw[i] for i in order]
    stream.insert(3, stream[1])  # duplicate of an already-seen chunk
    stream.insert(5, stream[0])
    msgs = feed_all(stream)
    assert [m.body for m in msgs] == [PROTO_BODY]


def test_a_duplicate_of_the_last_chunk_after_completion_starts_nothing_complete():
    raw = encode_message("t", b"z" * 130, mtu=185)  # 3 chunks of 65, 65... = 2 chunks
    rx = Reassembler()
    assert len(feed_all(raw, rx)) == 1
    assert rx.feed(raw[-1]) is None  # a straggler: a new partial, never a bogus message
    assert rx.pending == 1


def test_two_topics_interleave():
    a = encode_message("a", b"A" * 200, mtu=185)
    b = encode_message("b", b"B" * 200, mtu=185)
    mixed = [p for pair in zip(a, b) for p in pair] + list(a[len(b):]) + list(b[len(a):])
    msgs = feed_all(mixed)
    assert {m.topic: m.body for m in msgs} == {"a": b"A" * 200, "b": b"B" * 200}


def test_a_new_message_on_the_same_topic_replaces_an_abandoned_one():
    old = encode_message("t", b"o" * 200, mtu=185)  # 4 chunks
    new = encode_message("t", b"n" * 70, mtu=185)  # 2 chunks
    rx = Reassembler()
    assert rx.feed(old[0]) is None
    assert feed_all(new, rx) == [bp.Message("t", b"n" * 70, False)]


def test_a_partial_message_times_out_and_is_logged(caplog):
    now = [100.0]
    rx = Reassembler(timeout=20, clock=lambda: now[0])
    raw = encode_message("t", b"x" * 200, mtu=185)
    rx.feed(raw[0])
    now[0] += 21
    with caplog.at_level(logging.WARNING):
        assert rx.expire() == 1
    assert rx.pending == 0
    assert "data may be incomplete" in caplog.text
    # the rest of the old message cannot complete it
    assert feed_all(raw[1:], rx) == []


def test_a_message_within_the_timeout_is_kept():
    now = [0.0]
    rx = Reassembler(timeout=20, clock=lambda: now[0])
    raw = encode_message("t", b"x" * 200, mtu=185)
    rx.feed(raw[0])
    now[0] += 19
    assert [m.body for m in feed_all(raw[1:], rx)] == [b"x" * 200]


# -- malformed input -------------------------------------------------------------------------------------------
GOOD = encode_packet(2, 1, "ab", 300, b"hi")


@pytest.mark.parametrize("raw", [
    GOOD[:-1],  # data cut short
    GOOD[:5],  # cut inside a wrapper
    b"\x0a",  # tag with no length
    b"\x0a\x80",  # unterminated length varint
    b"\xff" * 12,  # oversized varint
    b"\x00\x01",  # field number 0
    b"\x0b\x00",  # a deprecated group wire type
    b"\x08\x02",  # field 1 as a bare varint, not a wrapper
    bytes.fromhex("0a020802" "12020801" "1a040a02ffff" "2a00"),  # topic is not UTF-8
    bytes.fromhex("0a020800" "12020801" "2a00"),  # totalPackets 0
    bytes.fromhex("0a020802" "12020803" "2a00"),  # packetIndex 3 of 2
    bytes.fromhex("0a020802" "12020800" "2a00"),  # packetIndex 0 (indexes are 1-based)
    encode_packet(bp.MAX_PACKETS + 1, 1, "t", 0, b""),
    b"",  # nothing at all
])
def test_a_malformed_packet_is_rejected(raw):
    with pytest.raises(PacketError):
        decode_packet(raw)


def test_the_reassembler_ignores_garbage_and_carries_on(caplog):
    rx = Reassembler()
    raw = encode_message("t", b"q" * 100, mtu=185)
    with caplog.at_level(logging.WARNING):
        assert rx.feed(b"\xff\xff\xff") is None
        assert rx.feed(GOOD[:-1]) is None
    assert "data may be incomplete or corrupt" in caplog.text
    assert [m.body for m in feed_all(raw, rx)] == [b"q" * 100]


def test_a_corrupt_zlib_stream_is_handed_back_untouched():
    bad = zlib.compress(b"hello world" * 20)[:-4]  # truncated stream
    (msg,) = feed_all([encode_packet(1, 1, "t", 1, bad)])
    assert msg.body == bad and msg.compressed is False


def test_an_inflate_bomb_is_not_inflated(monkeypatch):
    monkeypatch.setattr(bp, "MAX_BODY", 1000)
    bomb = zlib.compress(b"\x00" * 100_000)
    (msg,) = feed_all([encode_packet(1, 1, "t", 1, bomb)])
    assert msg.body == bomb and msg.compressed is False


def test_trailing_bytes_after_a_zlib_stream_keep_the_payload_opaque():
    payload = zlib.compress(b"hello") + b"tail"
    (msg,) = feed_all([encode_packet(1, 1, "t", 1, payload)])
    assert msg.body == payload and msg.compressed is False


def test_pending_state_is_bounded(monkeypatch):
    monkeypatch.setattr(bp, "MAX_PENDING", 3)
    monkeypatch.setattr(bp, "MAX_BODY", 100)
    now = [0.0]
    rx = Reassembler(clock=lambda: now[0])
    for i in range(5):
        now[0] += 1
        rx.feed(encode_packet(2, 1, f"t{i}", 1, b"x"))
    assert rx.pending == 3
    # the oldest topics were evicted; a message that grows past the byte limit is dropped
    rx.feed(encode_packet(3, 1, "big", 1, b"x" * 60))
    assert rx.feed(encode_packet(3, 2, "big", 1, b"x" * 60)) is None
    assert "big" not in rx._pending


def test_a_restart_or_a_one_packet_message_does_not_evict_an_unrelated_partial(monkeypatch):
    monkeypatch.setattr(bp, "MAX_PENDING", 2)
    rx = Reassembler()
    rx.feed(encode_packet(2, 1, "a", 1, b"1"))
    rx.feed(encode_packet(2, 1, "b", 1, b"2"))
    assert rx.feed(encode_packet(1, 1, "c", 1, b"whole")) == bp.Message("c", b"whole", False)  # needs no slot
    rx.feed(encode_packet(3, 1, "b", 1, b"2"))  # b restarts with a new total: reuses its own slot
    assert rx.pending == 2
    assert rx.feed(encode_packet(2, 2, "a", 1, b"3")) == bp.Message("a", b"13", False)  # a survived
