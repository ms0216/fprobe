"""ログの流れの枠: 0x00 で区切るかたまり（`ZeroDelimited`）と、その中身の符号化 COBS・rzCOBS。

- rzCOBS（defmt-rtt の既定。github.com/Dirbaio/rzcobs）: かたまりを後ろから読む。符号語 0x01〜0x7F は 7 バイト分の
  「0 か、流れの 1 バイトか」のビットの並び（LSB から。1 = 0x00）、0x80〜0xFE は (n+7) バイトを流れから出して 0x00、
  0xFF は 134 バイトを流れから出す。解読の結果の末尾には 0 の詰め物が残る（読む側が必要な分だけ読む）。
- COBS（Trice の選べる枠の 1 つ等）: 標準の COBS（符号語 = 次の 0 までの距離）。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FRAME_LIMIT", "Dropped", "ZeroDelimited", "dropped_text", "cobs_decode", "cobs_encode", "rzcobs_decode", "rzcobs_encode", "tcobs_decode"]


FRAME_LIMIT = 1 << 16                # ZeroDelimited の limit の既定（捨てたときは Dropped に使った上限）


@dataclass(frozen=True)
class Dropped:
    """区切りの来ないまま上限を超えたので捨てたバイト（size）と、使った上限（limit）。"""
    size: int
    limit: int


class ZeroDelimited:
    """0x00 で区切られた流れからかたまりを切り出す（空のかたまりは捨てる）。区切りの来ないまま limit バイトを
    超えたら（同期を失った流れ）そこまでを捨て、feed の結果のその位置に Dropped を入れる。"""

    def __init__(self, limit: int = FRAME_LIMIT):
        if limit < 1:
            raise ValueError("limit は 1 以上")
        self._buf = bytearray()
        self.limit = limit
        self.dropped = 0                 # 捨てたバイトの合計

    @property
    def pending(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[bytes | Dropped]:
        out: list[bytes | Dropped] = []
        for b in data:
            if b == 0:
                if self._buf:
                    out.append(bytes(self._buf))
                    self._buf.clear()
            else:
                self._buf.append(b)
                if len(self._buf) > self.limit:
                    self.dropped += len(self._buf)
                    out.append(Dropped(len(self._buf), self.limit))
                    self._buf.clear()
        return out


def dropped_text(d: Dropped) -> str:
    return f"区切り（0x00）の来ないまま上限 {d.limit} バイトを超えた {d.size} バイトを捨てた"


def rzcobs_encode(data: bytes) -> bytes:
    """区切りの 0x00 を含まない符号化（試験と、往復の確かめ用）。"""
    out = bytearray()
    run = zeros = 0
    for b in data:
        if run < 7:
            if b == 0:
                zeros |= 1 << run
            else:
                out.append(b)
            run += 1
            if run == 7 and zeros != 0:
                out.append(zeros)
                run = zeros = 0
        elif b == 0:
            out.append((run - 7) | 0x80)
            run = zeros = 0
        else:
            out.append(b)
            run += 1
            if run == 134:
                out.append(0xFF)
                run = zeros = 0
    if 1 <= run <= 6:
        out.append((zeros | (0xFF << run)) & 0x7F)
    elif run >= 7:
        out.append((run - 7) | 0x80)
    return bytes(out)


def rzcobs_decode(frame: bytes) -> bytes:
    """区切りを除いた 1 つのかたまり → 中身（末尾に 0 の詰め物が付きうる）。壊れていれば ValueError。"""
    out = bytearray()
    i = len(frame)

    def take() -> int:
        nonlocal i
        if i == 0:
            raise ValueError("rzCOBS: かたまりが途中で終わった")
        i -= 1
        return frame[i]
    while i > 0:
        code = take()
        if code == 0:
            raise ValueError("rzCOBS: かたまりの中に 0x00")
        if code == 0xFF:
            for _ in range(134):
                out.append(take())
        elif code & 0x80:
            out.append(0)
            for _ in range((code & 0x7F) + 7):
                out.append(take())
        else:
            for bit in range(6, -1, -1):        # 後ろから読むので、ビットの並びも上から
                out.append(0 if code >> bit & 1 else take())
    out.reverse()
    return bytes(out)


def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])
    code_at, code = 0, 1
    for b in data:
        if b == 0:
            out[code_at] = code
            code_at, code = len(out), 1
            out.append(0)
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_at] = code
                code_at, code = len(out), 1
                out.append(0)
    out[code_at] = code
    return bytes(out)


def cobs_decode(frame: bytes) -> bytes:
    """区切りを除いた COBS のかたまり → 中身。壊れていれば ValueError。"""
    out = bytearray()
    i = 0
    while i < len(frame):
        code = frame[i]
        if code == 0:
            raise ValueError("COBS: かたまりの中に 0x00")
        end = i + code
        if end > len(frame) + (0 if code == 0xFF else 1) or end - 1 > len(frame):
            raise ValueError("COBS: 符号語が長さを超えている")
        out += frame[i + 1:end]
        i = end
        if code != 0xFF and i < len(frame):
            out.append(0)
    return bytes(out)


def tcobs_decode(frame: bytes) -> bytes:
    """TCOBS v1（Trice の既定の枠。github.com/rokath/tcobs docs/TCOBSv1Specification.md）の 1 つのかたまり
    （区切りを除く）→ 中身。後ろから読む: 印のバイト（sigil）は、種類（上位 3 ビット、R は上位 5 ビット）と、
    前の印までのバイト数（off）を持つ。N = 0 バイト、Z1〜Z3 = 00 を 1〜3 個、F2〜F4 = FF を 2〜4 個、
    R2〜R4 = 直前のバイトをさらに 2〜4 回。その後に off バイトの生のバイトが前に続く。壊れていれば ValueError。"""
    out = bytearray()                           # 後ろから組み立て、最後に反転する
    n = len(frame)
    while n > 0:
        b = frame[n - 1]
        sig = b & 0xE0
        if sig == 0:
            sig, off = b & 0xF8, b & 0x07
        else:
            off = b & 0x1F
        if off + 1 > n:
            raise ValueError("TCOBS: 印の off がかたまりの長さを超えている")
        n -= 1
        if sig == 0xA0:                         # N
            pass
        elif sig in (0x20, 0x40, 0x60):         # Z1 Z2 Z3
            out += b"\x00" * (sig >> 5)
        elif sig in (0xC0, 0xE0, 0x80):         # F2 F3 F4
            out += b"\xff" * {0xC0: 2, 0xE0: 3, 0x80: 4}[sig]
        elif sig in (0x08, 0x10, 0x18):         # R2 R3 R4
            if off == 0:
                if n < 2:
                    raise ValueError("TCOBS: 繰り返すバイトが無い")
                r = frame[n - 2]
            else:
                r = frame[n - 1]
            out += bytes([r]) * ((sig >> 3) + 1)
        else:
            raise ValueError(f"TCOBS: 知らない印 0x{b:02X}")
        if off:
            out += frame[n - off:n][::-1]
            n -= off
    out.reverse()
    return bytes(out)
