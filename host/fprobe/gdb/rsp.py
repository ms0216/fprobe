"""GDB リモートシリアルプロトコル（RSP）の枠: パケットの組み立てと読み取り、バイナリの逃がし、リトルエンディアンの 16 進。

    $<中身>#<検査和 2 桁>     検査和は中身（逃がした後の線上のバイト）の和の下位 8 ビット
    +  -                     受け取った / 壊れていた（ack モードのとき）
    0x03                     実行中の割り込み（^C）
中身の `$ # } *` は `}` と元のバイト XOR 0x20 に逃がす（GDB の read_frame はすべてのパケットで逃がしを戻す）。
"""

from dataclasses import dataclass

__all__ = ["ACK", "CORRUPT", "INTERRUPT", "NAK", "Packet", "PacketReader", "escape", "frame", "from_le_hex",
           "le_hex", "unescape"]

_ESCAPED = frozenset(b"$#}*")


@dataclass(frozen=True)
class Packet:
    payload: bytes            # 逃がしを戻した中身


class _Signal:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


ACK = _Signal("ACK")
NAK = _Signal("NAK")
INTERRUPT = _Signal("INTERRUPT")
CORRUPT = _Signal("CORRUPT")          # 検査和が合わないパケット（ack モードなら '-' を返す）


def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in _ESCAPED:
            out += bytes((0x7D, b ^ 0x20))
        else:
            out.append(b)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    out = bytearray()
    it = iter(data)
    for b in it:
        out.append(next(it, 0x20) ^ 0x20 if b == 0x7D else b)
    return bytes(out)


def frame(payload: bytes) -> bytes:
    body = escape(payload)
    return b"$" + body + b"#%02x" % (sum(body) & 0xFF)


class PacketReader:
    """受け取ったバイトを順に与え、出来事（Packet / ACK / NAK / INTERRUPT / CORRUPT）の列を得る。パケットの途中で
    切れたバイトは次の feed に持ち越す。パケットの外の他のバイトは捨てる。"""

    def __init__(self):
        self._body: bytearray | None = None      # '$' の後、'#' まで
        self._check: bytearray | None = None     # '#' の後の 2 桁

    def feed(self, data: bytes) -> list:
        events = []
        for b in data:
            if self._check is not None:
                self._check.append(b)
                if len(self._check) == 2:
                    events.append(self._finish())
            elif self._body is not None:
                if b == 0x23:                                 # '#'
                    self._check = bytearray()
                else:
                    self._body.append(b)
            elif b == 0x24:                                   # '$'
                self._body = bytearray()
            elif b == 0x2B:
                events.append(ACK)
            elif b == 0x2D:
                events.append(NAK)
            elif b == 0x03:
                events.append(INTERRUPT)
        return events

    def _finish(self):
        body, check = bytes(self._body), bytes(self._check)
        self._body = self._check = None
        try:
            ok = int(check, 16) == sum(body) & 0xFF
        except ValueError:
            ok = False
        return Packet(unescape(body)) if ok else CORRUPT


def le_hex(value: int | None, bits: int) -> str:
    """値をリトルエンディアンのバイト列の 16 進に。None（読めないレジスタ）は 'x' で埋める。"""
    if value is None:
        return "x" * (bits // 4)
    return value.to_bytes(bits // 8, "little").hex()


def from_le_hex(text: str) -> int:
    return int.from_bytes(bytes.fromhex(text), "little")
