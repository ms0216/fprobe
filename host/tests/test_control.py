"""ホストライブラリ: CONTROL プロトコルと Device（仕様どおりに振る舞う偽デバイスを相手に）。"""

import struct

import pytest

from fprobe import ControlError, Device, ProtocolError
from fprobe import generated as g
from fprobe.caps import parse
from fprobe.control import decode_response, encode_exchange, encode_read, encode_write
from fprobe.device import DeviceMismatch


def caps_blob(entries: list[tuple[int, bytes]]) -> bytes:
    body = b"".join(struct.pack("<HH", k, len(v)) + v + bytes(-len(v) % 4) for k, v in entries)
    body += struct.pack("<HH", g.CAPS_END, 0)
    return struct.pack("<II", g.CAPS_MAGIC, 8 + len(body)) + body


CAPS = caps_blob([(g.CAPS_PROTOCOL_VERSION, struct.pack("<I", 1)), (g.CAPS_BOARD, b"Fake"),
                  (99, b"future"), (g.CAPS_TIMEBASE_HZ, struct.pack("<I", 100_000_000)),
                  (g.CAPS_CHANNELS, struct.pack("<I", 0b1001))])


class FakeDevice:
    """仕様 §3 どおりに応答する偽デバイス（system・timebase・caps の窓）。"""

    def __init__(self, max_packet=512, magic=g.SYSTEM_MAGIC_VALUE):
        self.max_packet = max_packet
        self.words = {g.SYSTEM_MAGIC: magic, g.SYSTEM_VERSION: g.PROTOCOL_VERSION, g.SYSTEM_SCRATCH: 0}
        self.time = 0x1_2345_6789
        self.time_hi = 0
        self.requests: list[bytes] = []
        self._response = b""
        self.tag_offset = 0
        self.flushes = 0

    def _access(self, address, write=None):
        if g.CAPS_BASE <= address < g.CAPS_BASE + g.CAPS_SIZE:
            offset = address - g.CAPS_BASE
            return int.from_bytes(CAPS[offset:offset + 4].ljust(4, b"\0"), "little")
        if address == g.TIMEBASE_TIME_LO:
            self.time_hi = self.time >> 32
            return self.time & 0xFFFFFFFF
        if address == g.TIMEBASE_TIME_HI:
            return self.time_hi
        if address not in self.words:
            return None
        if write is not None and address == g.SYSTEM_SCRATCH:
            self.words[address] = write
        return self.words[address]

    def send(self, message):
        assert len(message) <= self.max_packet
        self.requests.append(message)
        op, tag, count, address = struct.unpack_from("<BBHI", message)
        tag = (tag + self.tag_offset) & 0xFF
        data = []
        status, done = g.STATUS_OK, 0
        for i in range(count):
            at = address if op == g.OP_EXCHANGE else address + 4 * i          # EXCHANGE は番地を進めない
            value = self._access(at, struct.unpack_from("<I", message, 8 + 4 * i)[0]
                                 if op in (g.OP_WRITE, g.OP_EXCHANGE) else None)
            if value is None:
                status = g.STATUS_BUS_ERROR
                break
            done += 1
            if op in (g.OP_READ, g.OP_EXCHANGE):
                data.append(value)
        self._response = struct.pack(f"<{len(data)}IBBH", *data, tag, status, done)

    def receive(self, max_length):
        assert len(self._response) <= max_length
        return self._response

    def flush(self):
        self.flushes += 1
        self._response = b""


def test_encoding_matches_spec():
    assert encode_read(7, 0x1000, 3) == bytes([1, 7, 3, 0, 0x00, 0x10, 0, 0])
    assert encode_write(9, 8, [0x11223344]) == bytes([2, 9, 1, 0, 8, 0, 0, 0, 0x44, 0x33, 0x22, 0x11])
    assert decode_response(bytes([1, 2, 3, 4, 5, 0, 1, 0])) == (5, 0, 1, [0x04030201])
    with pytest.raises(ProtocolError):
        decode_response(bytes([5, 0, 1]))
    assert encode_exchange(3, 0x20C, [5, 6]) == bytes([3, 3, 2, 0, 0x0C, 2, 0, 0, 5, 0, 0, 0, 6, 0, 0, 0])


def test_exchange_writes_and_reads_the_same_word_and_is_split():
    fake = FakeDevice(max_packet=64)
    dev = Device(fake)
    fake.requests.clear()
    assert dev.exchange(g.SYSTEM_SCRATCH, list(range(30))) == list(range(30))     # 書いた直後に読む
    assert [struct.unpack_from("<BxH", r) for r in fake.requests] == [(g.OP_EXCHANGE, 14)] * 2 + [(g.OP_EXCHANGE, 2)]
    assert dev.read32(g.SYSTEM_SCRATCH) == 29
    with pytest.raises(ControlError) as e:
        dev.exchange(g.SYSTEM_SCRATCH + 4, [1])
    assert e.value.status == g.STATUS_BUS_ERROR and e.value.address == g.SYSTEM_SCRATCH + 4


def test_device_checks_identity():
    Device(FakeDevice())
    with pytest.raises(DeviceMismatch):
        Device(FakeDevice(magic=0))


def test_older_compatible_protocols_are_accepted_and_newer_ones_are_not():
    old = FakeDevice()
    old.words[g.SYSTEM_VERSION] = 1                         # EXCHANGE の無い版も READ / WRITE はそのまま使える
    assert Device(old).protocol_version == 1
    assert Device(FakeDevice()).protocol_version == g.PROTOCOL_VERSION
    new = FakeDevice()
    new.words[g.SYSTEM_VERSION] = g.PROTOCOL_VERSION + 1
    with pytest.raises(DeviceMismatch, match="プロトコル版"):
        Device(new)


def test_scratch_round_trip_and_bus_error():
    dev = Device(FakeDevice())
    dev.write32(g.SYSTEM_SCRATCH, 0xCAFEBABE)
    assert dev.read32(g.SYSTEM_SCRATCH) == 0xCAFEBABE
    with pytest.raises(ControlError) as e:
        dev.read(g.SYSTEM_SCRATCH, 3)                       # 0x0C は未定義
    assert e.value.status == g.STATUS_BUS_ERROR and e.value.address == g.SYSTEM_SCRATCH + 4


def test_long_transfers_are_split(monkeypatch):
    monkeypatch.setattr("fprobe.control.MAX_READ_WORDS", 1000)
    fake = FakeDevice(max_packet=64)
    dev = Device(fake)
    fake.requests.clear()
    with pytest.raises(ControlError):
        dev.write(g.SYSTEM_SCRATCH, [1] * 20)               # 14 ワード / 要求に分かれ、2 ワード目でエラー
    assert all(len(r) <= 64 for r in fake.requests)
    fake.requests.clear()
    dev.read(g.CAPS_BASE, 1024)                             # caps の窓いっぱい
    assert [struct.unpack_from("<H", r, 2)[0] for r in fake.requests] == [1000, 24]


def test_tag_mismatch_is_detected():
    fake = FakeDevice()
    dev = Device(fake)
    fake.tag_offset = 1
    with pytest.raises(ProtocolError, match="tag"):
        dev.read32(g.SYSTEM_SCRATCH)
    assert fake.flushes == 1                                # 古い応答を残さない
    fake.tag_offset = 0
    assert dev.read32(g.SYSTEM_SCRATCH) == 0


def test_capabilities_and_time():
    dev = Device(FakeDevice())
    caps = dev.capabilities()
    assert caps.protocol_version == 1 and caps.board == "Fake" and caps.timebase_hz == 100_000_000
    assert caps.channels == {g.CH_CONTROL, g.CH_UART0}
    assert caps.unknown == {99: b"future"}                  # 未知の種類は読み飛ばして残す
    assert dev.time() == 0x1_2345_6789


@pytest.mark.parametrize("blob", [b"XXXX" + CAPS[4:], CAPS[:-4] + b"\1\0\0\0", CAPS[:12]])
def test_malformed_capabilities_are_rejected(blob):
    with pytest.raises(ValueError):
        parse(blob)


def test_entry_overrunning_total_is_rejected():
    bad = bytearray(CAPS)
    struct.pack_into("<H", bad, 8 + 2, 200)                 # 先頭項目の長さを総バイト数より大きく
    with pytest.raises(ValueError, match="はみ出す"):
        parse(bytes(bad))


def test_vcd_writer(tmp_path):
    from fprobe.la import Capture, write_vcd
    cap = Capture(samples=[0b00, 0b01, 0b11, 0b10], trigger=2, trigger_time=0, sample_period_s=10e-9, channels=2)
    path = tmp_path / "x.vcd"
    write_vcd(cap, path, {0: "clk"})
    text = path.read_text()
    assert "$var wire 1 ! clk $end" in text and "#20" in text and "1#" in text   # トリガは 20 ns
    assert cap.channel(1) == [0, 0, 1, 1]
