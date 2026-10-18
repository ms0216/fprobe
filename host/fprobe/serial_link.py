"""SerialLink: 論理チャンネルを UART 1 本に多重化するホストリンク（USB HS の PHY を持たないボード用）。

仕様: docs/superpowers/specs/2026-10-17-m11-serial-link-design.md §2

    UART: … 0x00 | COBS( ch(1) | payload(0..512) | crc16(2, LE) ) | 0x00 …

- CRC-16/CCITT-FALSE（多項式 0x1021、初期値 0xFFFF、反転なし）を ch と payload に対して計算する。
  合わない・形の崩れたフレームは捨てて bad_frames に数える。
- チャンネルの意味と振り分けは frame_link.FrameLink（EthLink と共通）。
"""

import struct
from typing import Protocol

from . import generated as g
from .frame_link import (CH_CONTROL, CH_DAP, CH_LINE_CODING, CH_STREAM, CH_SWO, CH_UART0, MAX_PAYLOAD,
                         FrameLink)

__all__ = ["CH_CONTROL", "CH_DAP", "CH_LINE_CODING", "CH_STREAM", "CH_SWO", "CH_UART0", "DEFAULT_BAUD",
           "LINK_SERIAL", "MAX_PAYLOAD", "SerialLink", "SerialPort", "cobs_decode", "cobs_encode", "crc16",
           "decode_frame", "encode_frame"]

LINK_SERIAL = getattr(g, "LINK_SERIAL", 2)           # ケイパビリティ host_link の値

DEFAULT_BAUD = 2_000_000
_MAX_BODY = 1 + MAX_PAYLOAD + 2
_MAX_ENCODED = _MAX_BODY + -(-_MAX_BODY // 254)      # COBS の最大の膨らみ


# --- 符号化 -------------------------------------------------------------------------------


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE。"""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1 ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    """COBS（区切りの 0x00 は含まない）。254 バイトの塊で終わるときも末尾に 0x01 を付ける（参照実装と同じ。
    復号側は付く・付かないのどちらも受け付けること）。"""
    out = bytearray()
    for block in data.split(b"\0"):
        # 0x00 で区切った各塊を、254 バイトごとに「長さ + 1」の符号バイト付きで出す
        while len(block) >= 254:
            out += b"\xff" + block[:254]
            block = block[254:]
        out += bytes([len(block) + 1]) + block
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    """cobs_encode の逆。形が崩れていれば ValueError。"""
    out = bytearray()
    pos = 0
    while pos < len(data):
        code = data[pos]
        block = data[pos + 1:pos + code]
        if code == 0 or len(block) != code - 1 or 0 in block:
            raise ValueError("COBS の塊が不正")
        out += block
        pos += code
        if code != 0xFF and pos < len(data):
            out += b"\0"
    return bytes(out)


def encode_frame(ch: int, payload: bytes) -> bytes:
    """1 フレーム（末尾の区切り 0x00 を含む）。"""
    if not 0 <= ch <= 0xFF or len(payload) > MAX_PAYLOAD:
        raise ValueError(f"フレームが不正: ch {ch}, {len(payload)} バイト")
    body = bytes([ch]) + payload
    return cobs_encode(body + struct.pack("<H", crc16(body))) + b"\0"


def decode_frame(encoded: bytes) -> tuple[int, bytes]:
    """区切りを除いた 1 フレームから (ch, payload)。CRC 不一致・形の崩れは ValueError。"""
    body = cobs_decode(encoded)
    if not 3 <= len(body) <= _MAX_BODY:
        raise ValueError(f"フレームの長さが不正: {len(body)} バイト")
    if crc16(body[:-2]) != struct.unpack_from("<H", body, len(body) - 2)[0]:
        raise ValueError("CRC 不一致")
    return body[0], body[1:-2]


# --- リンク -------------------------------------------------------------------------------


class SerialPort(Protocol):
    """pyserial の Serial の使う部分（read は port の timeout で戻ること）。"""

    in_waiting: int

    def write(self, data: bytes) -> int | None: ...

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


class SerialLink(FrameLink):
    """UART 上のフレーム（COBS + CRC16）の送受信。チャンネルの振り分けは FrameLink。"""

    name = "serial"
    dap_packet_count = 1          # FPGA の受信 FIFO は 64 バイト（流している間に次の 512 バイトは溢れる）

    def __init__(self, port: SerialPort, *, owns_port: bool = False):
        super().__init__()
        self._port = port
        self._owns_port = owns_port
        self._start()
        with self._tx_lock:
            self._port.write(bytes(1))                     # FPGA 側の受信途中のごみを区切る（区切りの 0x00）

    @classmethod
    def open(cls, port: str, baud: int = DEFAULT_BAUD) -> "SerialLink":
        from serial import Serial
        return cls(Serial(port, baud, timeout=0.05), owns_port=True)

    def _write_frame(self, ch: int, payload: bytes) -> None:
        self._port.write(encode_frame(ch, payload))

    def _close_transport(self) -> None:
        if self._owns_port:
            self._port.close()

    def _rx_loop(self) -> None:
        frame = bytearray()
        overflow = False
        while not self._stop.is_set():
            data = self._port.read(max(1, self._port.in_waiting))
            while data:
                end = data.find(0)
                part, data = (data, b"") if end < 0 else (data[:end], data[end + 1:])
                frame += part
                overflow |= len(frame) > _MAX_ENCODED
                if overflow:
                    frame.clear()                     # 長すぎるフレームは次の区切りまで捨てる
                if end < 0:
                    continue
                if overflow:
                    self._count_bad()
                elif frame:
                    try:
                        ch, payload = decode_frame(bytes(frame))
                    except ValueError:
                        self._count_bad()
                    else:
                        self._dispatch(ch, payload)
                frame.clear()
                overflow = False
