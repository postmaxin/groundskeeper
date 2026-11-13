"""BLE message framing: a topic and a body, chunked into protobuf `Packet`s (ADR 0011, issue #73, upstream #150).

Pure bytes in, bytes out: no Bluetooth, no robot, no I/O. Nothing here sends anything; it only builds and parses the
frames that a future BLE transport would write and read. Everything below comes from a static read of the official
app, not a capture. A capture of the robot's real traffic must confirm these before any of it is relied on:
  * the chunk size: the app uses `mtu - 120` (or `mtu` when `mtu <= 100`) but the decompiled integer may be
    Smi-tagged, so the exact constants are unverified. `mtu` is therefore always a parameter; nothing assumes 500;
  * that `topic` is only the last "/" segment of the cloud topic (the caller passes it in as it wants it sent);
  * which firmware compresses (the app gates on a module version "1.1.43"): `compress` is a parameter, and the
    decoder detects a zlib stream instead of trusting a flag, as `codec.decode` does for the cloud path;
  * how the robot keys a message while reassembling (here: by topic; a new `totalPackets` restarts the topic).

Wire format of a `Packet` (protobuf; each of fields 1-4 is a protobuf wrapper message, whose one value is its own
field 1, so it is length-delimited on the outside):
    1 totalPackets  UInt32Value   tag 0x0a, len, [0x08 varint]
    2 packetIndex   UInt32Value   tag 0x12, len, [0x08 varint]   (1-based)
    3 topic         StringValue   tag 0x1a, len, [0x0a len utf-8]
    4 timestamp     UInt64Value   tag 0x22, len, [0x08 varint]   (ms since the epoch)
    5 data          bytes         tag 0x2a, len, raw bytes
The body is UTF-8 JSON, or protobuf for the five map-write topics; this module treats it as opaque bytes.
"""
import logging
import time
import zlib
from collections import namedtuple

log = logging.getLogger(__name__)

# The app's `minCompressVersion`: the module version that turns compression on. Informational; callers decide.
MIN_COMPRESS_VERSION = "1.1.43"
# Bounds so a bad or hostile peer cannot make the reassembler hold or inflate unbounded memory.
MAX_PACKETS = 4096
MAX_BODY = 16 * 1024 * 1024
MAX_PENDING = 32  # topics with a half-received message; the oldest is dropped past this
PARTIAL_TIMEOUT = 20.0  # seconds a half-received message is kept (the app's received-data watchdog is 20 s)

Packet = namedtuple("Packet", "total index topic timestamp data")
Message = namedtuple("Message", "topic body compressed")


class PacketError(ValueError):
    """The bytes are not a well-formed Packet (truncated, wrong wire type, out of range)."""


# -- protobuf primitives (only what a Packet needs) ------------------------------------------------------------
def _varint(n: int) -> bytes:
    if n < 0:
        raise ValueError("negative varint")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _read_varint(buf: bytes, pos: int):
    n = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise PacketError("truncated or oversized varint")
        b = buf[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, pos
        shift += 7


def _field(num: int, payload: bytes) -> bytes:
    """A length-delimited field (wire type 2)."""
    return _varint(num << 3 | 2) + _varint(len(payload)) + payload


def _wrap_uint(num: int, value: int) -> bytes:
    """A UInt32Value / UInt64Value field. The wrapper's value is field 1; 0 is the default, so it is left out."""
    return _field(num, b"\x08" + _varint(value) if value else b"")


def _wrap_str(num: int, value: str) -> bytes:
    raw = value.encode()
    return _field(num, _field(1, raw) if raw else b"")


def _fields(buf: bytes):
    """Yield (field number, wire type, value) for a message; a varint's value is an int, others are bytes."""
    pos = 0
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        num, wire = tag >> 3, tag & 7
        if num == 0:
            raise PacketError("field number 0")
        if wire == 0:
            val, pos = _read_varint(buf, pos)
        elif wire == 2:
            size, pos = _read_varint(buf, pos)
            if pos + size > len(buf):
                raise PacketError("length-delimited field runs past the end")
            val, pos = buf[pos:pos + size], pos + size
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if pos + width > len(buf):
                raise PacketError("fixed-width field runs past the end")
            val, pos = buf[pos:pos + width], pos + width
        else:
            raise PacketError(f"unsupported wire type {wire}")
        yield num, wire, val


def _unwrap(val, wire: int, kind: str):
    """The value inside a wrapper message (field 1), or its default when the wrapper is empty."""
    if wire != 2:
        raise PacketError("a wrapper field must be length-delimited")
    inner = None
    for num, w, v in _fields(val):
        if num == 1:
            inner = (w, v)
    if kind == "str":
        if inner is None:
            return ""
        if inner[0] != 2:
            raise PacketError("a StringValue holds length-delimited bytes")
        try:
            return inner[1].decode()
        except UnicodeDecodeError as e:
            raise PacketError("topic is not UTF-8") from e
    if inner is None:
        return 0
    if inner[0] != 0:
        raise PacketError("an integer wrapper holds a varint")
    return inner[1]


# -- Packet ----------------------------------------------------------------------------------------------------
def encode_packet(total: int, index: int, topic: str, timestamp: int, data: bytes) -> bytes:
    """One Packet, serialized: this is one GATT write."""
    return (_wrap_uint(1, total) + _wrap_uint(2, index) + _wrap_str(3, topic) + _wrap_uint(4, timestamp)
            + _field(5, bytes(data)))


def decode_packet(raw: bytes) -> Packet:
    """Parse one Packet. Raises PacketError for anything malformed, truncated or out of range."""
    total = index = timestamp = 0
    topic, data = "", b""
    for num, wire, val in _fields(bytes(raw)):
        if num == 1:
            total = _unwrap(val, wire, "uint")
        elif num == 2:
            index = _unwrap(val, wire, "uint")
        elif num == 3:
            topic = _unwrap(val, wire, "str")
        elif num == 4:
            timestamp = _unwrap(val, wire, "uint")
        elif num == 5:
            if wire != 2:
                raise PacketError("data must be length-delimited")
            data = val
        # unknown fields are skipped, as protobuf does
    if not 1 <= total <= MAX_PACKETS:
        raise PacketError(f"totalPackets {total} out of range")
    if not 1 <= index <= total:
        raise PacketError(f"packetIndex {index} outside 1..{total}")
    return Packet(total, index, topic, timestamp, data)


# -- messages --------------------------------------------------------------------------------------------------
def chunk_size(mtu: int) -> int:
    """Bytes of body per Packet: `mtu - 120`, or `mtu` itself when `mtu <= 100` (the app's rule; see the docstring)."""
    size = mtu if mtu <= 100 else mtu - 120
    if size < 1:
        raise ValueError(f"mtu {mtu} leaves no room for data")
    return size


def encode_message(topic: str, body: bytes, mtu: int, compress: bool = False, now_ms: int = None) -> list:
    """The GATT writes for one message: the body (zlib-deflated if `compress`) split into Packets."""
    payload = zlib.compress(bytes(body)) if compress else bytes(body)
    size = chunk_size(mtu)
    pieces = [payload[i:i + size] for i in range(0, len(payload), size)] or [b""]
    if len(pieces) > MAX_PACKETS:
        raise ValueError(f"{len(pieces)} packets exceeds {MAX_PACKETS}")
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    return [encode_packet(len(pieces), i, topic, ts, p) for i, p in enumerate(pieces, 1)]


def inflate_if_zlib(payload: bytes):
    """(bytes, True) if `payload` is a whole zlib stream, else (payload, False): the way `codec.decode` detects it."""
    d = zlib.decompressobj()
    try:
        out = d.decompress(payload, MAX_BODY + 1)
    except zlib.error:
        return payload, False
    if len(out) > MAX_BODY or not d.eof or d.unused_data:
        # a truncated stream, trailing bytes or a bomb is not a body we can use; hand the bytes back untouched
        return payload, False
    return out, True


class Reassembler:
    """Collects Packets and yields each completed message. Never raises for bad input; it logs and drops.

    Pending messages are keyed by topic. A Packet with a different `totalPackets` than the pending message for its
    topic, or a repeated index carrying different data, means the earlier message was abandoned: it is dropped and a
    new one starts. A repeated index with the same data is ignored. A message that stays incomplete longer than
    `timeout` seconds is dropped the next time a Packet arrives, or when `expire()` is called. At most MAX_PENDING
    topics are held, each up to MAX_BODY bytes.

    Known limit: the robot's real message identity is unconfirmed (see the module docstring), so two messages on one
    topic with the same `totalPackets` whose Packets interleave are not told apart.
    """

    def __init__(self, timeout: float = PARTIAL_TIMEOUT, clock=time.monotonic):
        self.timeout = timeout
        self._clock = clock
        self._pending = {}  # topic -> [total, {index: data}, first-seen time]

    def feed(self, raw: bytes):
        """Take one incoming Packet; return a Message when it completes one, else None."""
        self.expire()
        try:
            pkt = decode_packet(raw)
        except PacketError as e:
            log.warning("BLE Packet unparseable, data may be incomplete or corrupt: %s", e)
            return None
        entry = self._pending.get(pkt.topic)
        if entry is not None and (entry[0] != pkt.total or entry[1].get(pkt.index, pkt.data) != pkt.data):
            log.warning("BLE %s: a new message began before the last finished; dropped %d of %d packets",
                        pkt.topic, len(entry[1]), entry[0])
            entry = None
        if entry is None:
            if len(self._pending) >= MAX_PENDING:
                oldest = min(self._pending, key=lambda t: self._pending[t][2])
                log.warning("BLE %s: too many partial messages, dropped", oldest)
                del self._pending[oldest]
            entry = self._pending[pkt.topic] = [pkt.total, {}, self._clock()]
        entry[1].setdefault(pkt.index, pkt.data)
        if sum(map(len, entry[1].values())) > MAX_BODY:
            log.warning("BLE %s: message larger than %d bytes, dropped", pkt.topic, MAX_BODY)
            del self._pending[pkt.topic]
            return None
        if len(entry[1]) < entry[0]:
            return None
        del self._pending[pkt.topic]
        payload = b"".join(entry[1][i] for i in range(1, entry[0] + 1))
        body, compressed = inflate_if_zlib(payload)
        return Message(pkt.topic, body, compressed)

    def expire(self) -> int:
        """Drop messages that have waited longer than the timeout; return how many."""
        now = self._clock()
        stale = [t for t, e in self._pending.items() if now - e[2] > self.timeout]
        for t in stale:
            e = self._pending.pop(t)
            log.warning("BLE %s: timed out with %d of %d packets, data may be incomplete", t, len(e[1]), e[0])
        return len(stale)

    @property
    def pending(self) -> int:
        return len(self._pending)
