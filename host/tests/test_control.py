"""ホストライブラリ: CONTROL プロトコルと Device（仕様どおりに振る舞う偽デバイスを相手に）。"""

import struct

import pytest

from fprobe import ControlError, Device, ProtocolError
from fprobe import generated as g
from fprobe.caps import parse
from fprobe.control import ControlTimeout, decode_response, encode_exchange, encode_read, encode_write
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
        self._responses: list[bytes] = []                  # 届いている応答（古い順。受け取ると消える）
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
        self._responses.append(struct.pack(f"<{len(data)}IBBH", *data, tag, status, done))

    def receive(self, max_length):
        if not self._responses:
            raise TimeoutError("応答が届かない")
        response = self._responses.pop(0)
        assert len(response) <= max_length
        return response

    def flush(self):
        self.flushes += 1
        self._responses.clear()


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


def test_a_reply_with_another_tag_is_never_taken_as_this_reply():
    fake = FakeDevice()
    dev = Device(fake)
    fake.tag_offset = 1
    with pytest.raises(ControlTimeout) as info:            # 違う tag の応答は読み捨て、この要求の応答は届かない
        dev.read32(g.SYSTEM_SCRATCH)
    assert info.value.executed is None and info.value.op == "READ"   # 実行されたかは分からない（事実）
    assert isinstance(info.value, TimeoutError)
    assert dev.control.stale_responses == 1
    assert fake.flushes == 1                                # 古い応答を残さない
    fake.tag_offset = 0
    assert dev.read32(g.SYSTEM_SCRATCH) == 0


def test_a_late_reply_of_an_earlier_request_is_dropped_and_the_wait_goes_on():
    fake = FakeDevice()
    dev = Device(fake)
    dev.write32(g.SYSTEM_SCRATCH, 7)
    stale = struct.pack("<IBBH", 0xDEAD, (dev.control._tag - 3) & 0xFF, g.STATUS_OK, 1)   # 前の要求の遅れた応答
    original = fake.send

    def send(message):
        fake._responses.append(stale)                      # この要求の応答より先に、遅れた応答が届く
        original(message)
    fake.send = send
    assert dev.read32(g.SYSTEM_SCRATCH) == 7
    assert dev.control.stale_responses == 1


def test_a_write_whose_reply_does_not_come_is_reported_as_unknown_outcome():
    fake = FakeDevice()
    dev = Device(fake)
    original = fake.send

    def lose_reply(message):
        original(message)
        fake._responses.clear()                            # 応答が落ちた（要求は実行された。PC からは区別できない）
    fake.send = lose_reply
    with pytest.raises(ControlTimeout, match="実行されたかは分からない") as info:
        dev.write32(g.SYSTEM_SCRATCH, 5)
    assert (info.value.op, info.value.address, info.value.count, info.value.executed) == ("WRITE", g.SYSTEM_SCRATCH, 1, None)
    assert len([r for r in fake.requests if r[0] == g.OP_WRITE]) == 1     # 送り直さない（2 回実行しうる）


def _lose_replies(fake, n):
    """最初の n 個の要求の応答を落とす（要求は実行された。PC からは区別できない）。落とした応答を返す口も返す。"""
    original, lost, left = fake.send, [], [n]

    def send(message):
        original(message)
        if left[0]:
            left[0] -= 1
            lost.append(fake._responses.pop())
    fake.send = send
    return lost


def test_a_read_is_not_resent_unless_the_caller_asks():
    fake = FakeDevice()
    dev = Device(fake)
    _lose_replies(fake, 1)
    with pytest.raises(ControlTimeout) as info:
        dev.read(g.SYSTEM_SCRATCH, 1)
    assert info.value.attempts == 1
    assert len([r for r in fake.requests if r[0] == g.OP_READ and r[4:8] == struct.pack("<I", g.SYSTEM_SCRATCH)]) == 1
    assert dev.control.counters() == {"read_retries": 0, "stale_responses": 0}


def test_a_read_is_resent_with_a_new_tag_when_the_caller_asks_and_the_late_reply_is_dropped():
    fake = FakeDevice()
    dev = Device(fake)
    dev.write32(g.SYSTEM_SCRATCH, 9)
    lost = _lose_replies(fake, 1)
    original = fake.send

    def send(message):
        if lost and len(fake.requests) and fake.requests[-1][0] == g.OP_READ:
            fake._responses.append(lost.pop())             # 前の READ の応答が、送り直した READ の応答より先に遅れて届く
        original(message)
    fake.send = send
    assert dev.read(g.SYSTEM_SCRATCH, 1, timeout_retries=2) == [9]
    reads = [r for r in fake.requests if r[0] == g.OP_READ][-2:]
    assert reads[0][1] != reads[1][1]                      # 送り直しは新しい tag
    assert dev.control.counters() == {"read_retries": 1, "stale_responses": 1}


def test_a_read_that_never_gets_a_reply_reports_how_many_requests_were_sent():
    fake = FakeDevice()
    dev = Device(fake)
    _lose_replies(fake, 3)
    with pytest.raises(ControlTimeout) as info:
        dev.read(g.SYSTEM_SCRATCH, 1, timeout_retries=2)
    assert (info.value.attempts, info.value.op, info.value.executed) == (3, "READ", None)
    assert dev.control.read_retries == 2
    with pytest.raises(ValueError):
        dev.read(g.SYSTEM_SCRATCH, 1, timeout_retries=-1)


def test_writes_and_exchanges_have_no_way_to_be_resent():
    import inspect
    for method in (Device.write, Device.exchange, Device.write32):
        assert "timeout_retries" not in inspect.signature(method).parameters


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
