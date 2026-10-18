"""ホストライブラリ: SerialLink（COBS・CRC・フレーム・多重化）と、その上の Device / pyOCD の接続口。

相手は仕様（m11-serial-link-design §2）どおりに振る舞う FPGA 側の Python モデル（偽の COM ポート）。
"""

import struct
import threading
import time

import pytest

from fprobe import ControlError, Device
from fprobe import generated as g
from fprobe.serial_link import (CH_CONTROL, CH_DAP, CH_LINE_CODING, CH_STREAM, CH_SWO, CH_UART0, MAX_PAYLOAD,
                                SerialLink, cobs_decode, cobs_encode, crc16, decode_frame, encode_frame)
from fprobe.transport import LinkTransport

from test_control import CAPS

MEMORY = 0x8000_0000          # モデルの読み出し専用の窓（値 = アドレス）。長い応答の試験用
MEMORY_WORDS = 4096


class FpgaModel:
    """偽の COM ポート（pyserial の write / read / in_waiting / close）。書かれたフレームを FPGA 側として解釈し、
    応答フレームを受信側に積む。"""

    def __init__(self):
        self.words = {g.SYSTEM_MAGIC: g.SYSTEM_MAGIC_VALUE, g.SYSTEM_VERSION: g.PROTOCOL_VERSION,
                      g.SYSTEM_SCRATCH: 0}
        self.timeout = 0.02
        self._rx = bytearray()                  # ホストへ（ホストの read が取る）
        self._cond = threading.Condition()
        self._frame = bytearray()               # ホストから（区切りまで）
        self.baud: int | None = None
        self.uart_out = bytearray()
        self.corrupt = 0                        # 次の応答フレームのうち、この数を化けさせる
        self.dap_requests: list[bytes] = []
        self.closed = False

    # --- pyserial の I/F -------------------------------------------------------------

    @property
    def in_waiting(self):
        with self._cond:
            return len(self._rx)

    def write(self, data):
        for byte in bytes(data):
            if byte:
                self._frame.append(byte)
            elif self._frame:
                self._receive(*decode_frame(bytes(self._frame)))
                self._frame.clear()
        return len(data)

    def read(self, size):
        with self._cond:
            self._cond.wait_for(lambda: self._rx, self.timeout)
            data = bytes(self._rx[:size])
            del self._rx[:size]
            return data

    def close(self):
        self.closed = True

    # --- FPGA 側 ---------------------------------------------------------------------

    def send(self, ch, payload, corrupt=False):
        frame = bytearray(encode_frame(ch, payload))
        if corrupt or self.corrupt:
            self.corrupt = max(0, self.corrupt - 1)
            frame[1] ^= 0x01 if frame[1] != 0x01 else 0x02     # 区切り（0x00）にはしない
        with self._cond:
            self._rx += frame
            self._cond.notify_all()

    def send_packet(self, ch, data):
        """CONTROL の応答: 512 バイトずつ、短いフレーム（倍数ちょうどなら空）で終える。"""
        for i in range(0, len(data) + 1, MAX_PAYLOAD):
            self.send(ch, data[i:i + MAX_PAYLOAD])

    def stream(self, ch, data):
        for i in range(0, len(data), MAX_PAYLOAD):
            self.send(ch, data[i:i + MAX_PAYLOAD])

    def _receive(self, ch, payload):
        if ch == CH_CONTROL:
            self.send_packet(CH_CONTROL, self._control(payload))
        elif ch == CH_DAP:
            self.dap_requests.append(payload)
            self.send(CH_DAP, self._dap(payload))
        elif ch == CH_UART0:
            self.uart_out += payload
        elif ch == CH_LINE_CODING:
            (self.baud,) = struct.unpack("<I", payload)

    def _access(self, address, write=None):
        if g.CAPS_BASE <= address < g.CAPS_BASE + g.CAPS_SIZE:
            offset = address - g.CAPS_BASE
            return int.from_bytes(CAPS[offset:offset + 4].ljust(4, b"\0"), "little")
        if MEMORY <= address < MEMORY + 4 * MEMORY_WORDS:
            return address
        if address not in self.words:
            return None
        if write is not None:
            self.words[address] = write
        return self.words[address]

    def _control(self, message):
        assert len(message) <= MAX_PAYLOAD
        op, tag, count, address = struct.unpack_from("<BBHI", message)
        data, status, done = [], g.STATUS_OK, 0
        for i in range(count):
            value = self._access(address + 4 * i,
                                 struct.unpack_from("<I", message, 8 + 4 * i)[0] if op == g.OP_WRITE else None)
            if value is None:
                status = g.STATUS_BUS_ERROR
                break
            done += 1
            if op == g.OP_READ:
                data.append(value)
        return struct.pack(f"<{len(data)}IBBH", *data, tag, status, done)

    @staticmethod
    def _dap(command):
        if command[0] != 0x00:                           # DAP_Info 以外は折り返す
            return command
        value = {0x09: b"2.1.0\0", 0x04: b"0.1.0\0", 0xF0: bytes([0x01]), 0xFE: bytes([4]),
                 0xFF: struct.pack("<H", 512)}.get(command[1], b"")
        return bytes([0x00, len(value)]) + value


@pytest.fixture
def model():
    return FpgaModel()


@pytest.fixture
def link(model):
    with SerialLink(model) as link:
        yield link


# --- 符号化 -------------------------------------------------------------------------------


@pytest.mark.parametrize("data", [b"", b"\0", b"\0\0", b"\x11\0\x22", b"\x11\x22\0", bytes(range(1, 254)),
                                  bytes(range(1, 255)), bytes(range(1, 256)), bytes(range(256)) * 3,
                                  b"\xff" * 254 + b"\0" + b"\xff" * 255, bytes(515)])
def test_cobs_round_trip(data):
    encoded = cobs_encode(data)
    assert 0 not in encoded
    assert len(encoded) <= len(data) + 1 + len(data) // 254
    assert cobs_decode(encoded) == data


def test_cobs_known_vectors():
    assert cobs_encode(b"") == b"\x01"
    assert cobs_encode(b"\0") == b"\x01\x01"
    assert cobs_encode(b"\x11\x22\0\x33") == b"\x03\x11\x22\x02\x33"
    run = bytes(range(1, 255))                           # 254 バイトの塊: 末尾 0x01 は付けても付けなくても同じ
    assert cobs_decode(b"\xff" + run) == cobs_decode(b"\xff" + run + b"\x01") == run
    for bad in [b"\x00", b"\x03\x11", b"\x02\x00"]:
        with pytest.raises(ValueError):
            cobs_decode(bad)


def test_crc16_ccitt_false():
    assert crc16(b"123456789") == 0x29B1
    assert crc16(b"") == 0xFFFF


def test_frame_round_trip_and_rejection():
    frame = encode_frame(CH_DAP, b"\x00\x04")
    assert frame[-1] == 0 and 0 not in frame[:-1]
    assert decode_frame(frame[:-1]) == (CH_DAP, b"\x00\x04")
    assert decode_frame(encode_frame(CH_STREAM, bytes(512))[:-1]) == (CH_STREAM, bytes(512))
    body = bytearray(cobs_decode(frame[:-1]))
    body[1] ^= 0x80
    with pytest.raises(ValueError, match="CRC"):
        decode_frame(cobs_encode(bytes(body)))
    with pytest.raises(ValueError):
        encode_frame(CH_DAP, bytes(513))


# --- リンク ------------------------------------------------------------------------------


def test_device_over_serial_reads_and_writes_csr(model, link):
    dev = Device(LinkTransport(link))
    dev.write32(g.SYSTEM_SCRATCH, 0xCAFEBABE)
    assert dev.read32(g.SYSTEM_SCRATCH) == 0xCAFEBABE
    assert dev.capabilities().board == "Fake"
    with pytest.raises(ControlError) as e:
        dev.read(g.SYSTEM_SCRATCH, 3)
    assert e.value.status == g.STATUS_BUS_ERROR


@pytest.mark.parametrize("count", [127, 128, 300, 1024])   # 512 バイトちょうど（空フレームで終端）・複数フレーム
def test_long_control_responses_span_frames(link, count):
    dev = Device(LinkTransport(link))
    assert dev.read(MEMORY, count) == [MEMORY + 4 * i for i in range(count)]


def test_long_writes_fit_one_frame(model, link):
    dev = Device(LinkTransport(link))
    model.words.update({0x9000 + 4 * i: 0 for i in range(300)})
    dev.write(0x9000, list(range(300)))                      # 126 ワード（512 バイト）/ 要求に分かれる
    assert [model.words[0x9000 + 4 * i] for i in range(300)] == list(range(300))


def test_stream_bytes_arrive_in_order(model, link):
    transport = LinkTransport(link)
    data = bytes(i * 7 & 0xFF for i in range(5000))
    model.stream(CH_STREAM, data)
    head = transport.read_stream_some(3000)                   # 届いている分（最大 3000）
    assert 0 < len(head) <= 3000
    assert head + transport.read_stream(len(data) - len(head)) == data
    with pytest.raises(TimeoutError):
        transport.read_stream_some(16, timeout_ms=50)
    with pytest.raises(TimeoutError):
        transport.read_stream(16, timeout_ms=50)


def test_bad_frames_are_dropped_and_counted(model, link):
    model.send(CH_STREAM, b"lost", corrupt=True)
    model.send(CH_STREAM, b"kept")
    with model._cond:
        model._rx += b"\x05\x01\x02\0"                       # 形の崩れた COBS
        model._rx += b"\x01" * 2000 + b"\0"                  # 長すぎる
        model._rx += encode_frame(9, b"?")                   # 未知の ch
        model._cond.notify_all()
    model.send(CH_UART0, b"after")
    assert link.uart_read(16, timeout_s=1) == b"after"
    assert link.read_bytes(CH_STREAM, 16) == b"kept"
    assert link.bad_frames == 4


def test_lost_control_response_times_out_and_recovers(model, link, monkeypatch):
    monkeypatch.setattr("fprobe.transport.TIMEOUT_MS", 100)
    dev = Device(LinkTransport(link))
    model.corrupt = 1
    with pytest.raises(TimeoutError):
        dev.read32(g.SYSTEM_SCRATCH)
    model.corrupt = 1                                        # 長い応答の途中のフレームが欠けると短い応答になる
    with pytest.raises(Exception):
        dev.read(MEMORY, 300)
    assert dev.read(MEMORY, 300) == [MEMORY + 4 * i for i in range(300)]
    assert link.bad_frames == 2


def test_uart0_line_coding_and_pacing(model, link):
    link.set_uart_baud(115200)
    assert model.baud == 115200
    data = bytes(range(256)) * 2
    started = time.monotonic()
    assert link.uart_write(data) == len(data)
    elapsed = time.monotonic() - started
    assert model.uart_out == data
    assert elapsed >= (len(data) - 64 - 32) * 10 / 115200    # ターゲットのボーレートを超えて先行しない
    model.send(CH_UART0, b"hello")
    assert link.uart_read(timeout_s=1) == b"hello"
    assert link.uart_read() == b""


def test_close_stops_reader_and_owned_port(model):
    link = SerialLink(model, owns_port=True)
    link.close()
    link.close()
    assert model.closed and not link._thread.is_alive()


# --- pyOCD ------------------------------------------------------------------------------


def test_pyocd_dap_access_over_serial(model, link):
    from pyocd.probe.pydapaccess.dap_access_cmsis_dap import DAPAccessCMSISDAP

    from fprobe.session import LinkDapInterface

    interface = LinkDapInterface(link, "COMX")
    dap = DAPAccessCMSISDAP(None, interface=interface)
    dap.open()
    try:
        assert interface.get_packet_size() == 512 and interface.get_packet_count() == 1
        assert dap.get_unique_id() == "COMX"
        interface.start_swo()
        model.stream(CH_SWO, b"swo-data")
        deadline = time.monotonic() + 1
        swo = bytearray()
        while len(swo) < 8 and time.monotonic() < deadline:
            swo += interface.read_swo()
        assert swo == b"swo-data"
        interface.stop_swo()
    finally:
        dap.close()
    assert all(r[0] == 0x00 for r in model.dap_requests)   # DAP_Info だけ
