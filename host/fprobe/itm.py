"""ITM / DWT のパケットの解読（SWO のバイト列 → 型付きのパケット）。

根拠: ARMv7-M Architecture Reference Manual, Appendix D4「Debug ITM and DWT Packet Protocol」。
TPIU のフォーマッタは無効（SWO の UART 方式の普通の設定）のストリームを前提とする。
仕様: docs/superpowers/specs/2026-10-17-f1-rtt-itm-semihosting-design.md の 1 節。

    dec = ItmDecoder()                  # 受信の最初から来るなら synced=True（既定）。途中からなら False
    packets = dec.feed(chunk)           # chunk はどこで切れていてもよい
    lines = PortText().feed(packets)    # ポートごとの printf の行
    timed = Timestamper().feed(packets) # ローカル・グローバルのタイムスタンプを付ける

入出力を持たない（バイト列を受けてパケットを返すだけ）。FPGA でパケットの区切りを付ける場合も、意味づけはこれを使う。
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum

__all__ = ["DataTraceAddress", "DataTracePc", "DataTraceValue", "DecodeError", "EventCounter", "ExceptionAction",
           "ExceptionTrace", "Extension", "GlobalTimestamp1", "GlobalTimestamp2", "Instrumentation", "ItmDecoder",
           "LocalTimestamp", "Overflow", "Packet", "PcSample", "PortText", "Resync", "Sync", "TextLine", "TimedPacket",
           "TimestampRelation", "Timestamper"]


# --- パケット（offset は先頭のバイトの通し番号。比較には含めない） ---------------------------------

@dataclass(frozen=True)
class Sync:
    """同期パケット（0 が 47 ビット以上 + 1）。skipped = 同期を失っていた間に捨てたバイト数。"""
    skipped: int = field(default=0, compare=False)
    offset: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Resync:
    """同期パケットを待たずに解読を再開した（続く RESYNC_PACKETS 個のパケットが規定どおりに解ける位置から）。
    skipped = 捨てたバイト数。落ちたバイトの位置の推定なので、直前の数パケットは失われている。"""
    skipped: int = field(default=0, compare=False)
    offset: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Overflow:
    """ITM の FIFO があふれ、1 つ以上のパケットが捨てられた。"""
    offset: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Instrumentation:
    """ソフトウェアのソース（ITM のスティミュラス・ポート 0〜31 への 1 / 2 / 4 バイトの書き込み）。"""
    port: int
    value: int
    size: int
    offset: int = field(default=0, compare=False)

    @property
    def data(self) -> bytes:
        return self.value.to_bytes(self.size, "little")


@dataclass(frozen=True)
class EventCounter:
    """DWT のイベント・カウンタ（8 ビット）があふれた印。"""
    flags: int
    offset: int = field(default=0, compare=False)

    cpi = property(lambda self: bool(self.flags & 0x01))
    exc = property(lambda self: bool(self.flags & 0x02))
    sleep = property(lambda self: bool(self.flags & 0x04))
    lsu = property(lambda self: bool(self.flags & 0x08))
    fold = property(lambda self: bool(self.flags & 0x10))
    cyc = property(lambda self: bool(self.flags & 0x20))


class ExceptionAction(IntEnum):
    ENTER = 1        # 例外に入った
    EXIT = 2         # 例外から出た
    RETURN = 3       # 例外から戻った先で再開した


@dataclass(frozen=True)
class ExceptionTrace:
    """例外トレース（例外番号: 1〜15 はコアの例外、16 以上は外部割り込み = IRQ 番号 + 16）。"""
    number: int
    action: ExceptionAction
    offset: int = field(default=0, compare=False)


@dataclass(frozen=True)
class PcSample:
    """周期的な PC のサンプル。pc が None ならコアは眠っていた（WFI / WFE）。"""
    pc: int | None
    offset: int = field(default=0, compare=False)


@dataclass(frozen=True)
class DataTracePc:
    """データトレース: 比較器が一致したときの命令の番地。"""
    comparator: int
    pc: int
    offset: int = field(default=0, compare=False)


@dataclass(frozen=True)
class DataTraceAddress:
    """データトレース: 比較器が一致したデータの番地の下位 16 ビット。"""
    comparator: int
    address_low: int
    offset: int = field(default=0, compare=False)


@dataclass(frozen=True)
class DataTraceValue:
    """データトレース: 比較器が一致した読み書きの値。"""
    comparator: int
    write: bool
    value: int
    size: int
    offset: int = field(default=0, compare=False)


class TimestampRelation(IntEnum):
    """ローカルのタイムスタンプと、それが計る出来事の関係（形式 1 の TC）。"""
    SYNC = 0                 # 正確
    TIMESTAMP_DELAYED = 1    # タイムスタンプの出力が遅れた
    DATA_DELAYED = 2         # 出来事のパケットの出力が遅れた
    BOTH_DELAYED = 3


@dataclass(frozen=True)
class LocalTimestamp:
    """直前のローカルのタイムスタンプからの差分（TPIU の前置分周後のクロック数）。"""
    delta: int
    relation: TimestampRelation
    offset: int = field(default=0, compare=False)


@dataclass(frozen=True)
class GlobalTimestamp1:
    """グローバルのタイムスタンプの下位（value は下位 bits ビット。4 バイトの形のときだけ wrap・clock_change が有効）。"""
    value: int
    bits: int
    wrap: bool = False             # 上位のビットが変わった（GlobalTimestamp2 が続く）
    clock_change: bool = False     # クロックが変わった（上位を無効にする）
    offset: int = field(default=0, compare=False)


@dataclass(frozen=True)
class GlobalTimestamp2:
    """グローバルのタイムスタンプの上位（ビット 26 から上）。"""
    value: int
    offset: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Extension:
    """拡張パケット。source=0 なら ITM のスティミュラス・ポートのページ（value = ページ番号）。"""
    value: int
    source: int
    offset: int = field(default=0, compare=False)


@dataclass(frozen=True)
class DecodeError:
    """解読できないバイト。ここで同期を失い、次の同期パケットまでのバイトは捨てる（数は Sync.skipped）。"""
    reason: str
    data: bytes = field(default=b"", compare=False)
    offset: int = field(default=0, compare=False)


Packet = (Sync | Overflow | Instrumentation | EventCounter | ExceptionTrace | PcSample | DataTracePc | DataTraceAddress
          | DataTraceValue | LocalTimestamp | GlobalTimestamp1 | GlobalTimestamp2 | Extension | DecodeError)


# --- 解読器 -----------------------------------------------------------------------------

_SIZES = {1: 1, 2: 2, 3: 4}           # ソースのパケットのヘッダの下位 2 ビット → ペイロードの長さ
_SYNC_ZEROS = 5                       # 同期パケットの 0 のバイト数の下限（47 ビット = 5 バイト + 7 ビット）
_INCOMPLETE = None                    # 「まだバイトが足りない」の印
RESYNC_PACKETS = 6                    # 同期を失ったとき、この数のパケットが続けて解ける位置から再開する


class _Invalid(Exception):
    """ヘッダ（またはペイロード）が規定に合わない。"""


class ItmDecoder:
    """SWO のバイト列を増分で解読する。feed() に何回に分けて与えても、まとめて与えたのと同じ結果になる。"""

    def __init__(self, synced: bool = True, resync: bool = True):
        """resync: 同期を失ったら、同期パケット（DWT の SYNCTAP。遅いと十数秒に 1 回）を待たずに、続くパケットが
        規定どおりに解ける位置から再開する（落ちたバイト・古いバイトとの継ぎ目から復帰する）。False なら同期パケットを
        待つ（厳密）。"""
        self.resync = resync
        self._buf = bytearray()
        self._base = 0               # _buf[0] の通し番号
        self._synced = synced
        self._skipped = 0            # 同期を失ってから捨てたバイト数

    @property
    def synced(self) -> bool:
        return self._synced

    @property
    def consumed(self) -> int:
        """解読し終えた（パケットにした・捨てた）バイトの数。これより後のバイトはまだ途中。"""
        return self._base

    def feed(self, data: bytes) -> list[Packet]:
        self._buf += data
        out: list[Packet] = []
        while self._buf:
            if not self._synced:
                if not (self._realign(out) if self.resync else self._hunt(out)):
                    break
                continue
            try:
                result = self._parse()
            except _Invalid as e:
                out.append(DecodeError(str(e), bytes(self._buf[:1]), self._base))
                self._consume(1)
                self._synced, self._skipped = False, 0
                continue
            if result is _INCOMPLETE:
                break
            packet, length = result
            out.append(packet)
            self._consume(length)
        return out

    # --- 内部 ---------------------------------------------------------------------------

    def _consume(self, n: int) -> None:
        del self._buf[:n]
        self._base += n

    def _hunt(self, out: list[Packet]) -> bool:
        """同期パケットを探す。見つけたら Sync を出して True。見つからなければ、同期の始まりかもしれない
        末尾の 0 の並びだけを残して捨て、False。"""
        zeros = 0
        for i, b in enumerate(self._buf):
            if b == 0:
                zeros += 1
            elif b == 0x80 and zeros >= _SYNC_ZEROS:
                start = i - zeros
                self._skipped += start
                self._consume(start)
                out.append(Sync(self._skipped, self._base))
                self._consume(zeros + 1)
                self._synced, self._skipped = True, 0
                return True
            else:
                zeros = 0
        drop = len(self._buf) - zeros
        self._skipped += drop
        self._consume(drop)
        return False

    def _realign(self, out: list[Packet]) -> bool:
        """同期を取り直す: 先頭から順に、そこから RESYNC_PACKETS 個のパケットが解ける位置を探す（同期パケットも
        パケットとして解けるので、同期パケットがあればそこでも合う）。見つけたら Resync を出して True。判断に
        バイトが足りない位置で止まったら、その位置より前を捨てて False（次の feed で続ける。分けて与えても同じ結果）。"""
        for start in range(len(self._buf)):
            verdict = self._trial(start)
            if verdict is None:                               # バイトが足りない: 続きを待つ
                self._skipped += start
                self._consume(start)
                return False
            if verdict:
                self._skipped += start
                self._consume(start)
                out.append(Resync(self._skipped, self._base))
                self._synced, self._skipped = True, 0
                return True
        self._skipped += len(self._buf)
        self._consume(len(self._buf))
        return False

    def _trial(self, start: int) -> bool | None:
        """start から RESYNC_PACKETS 個が解けるか（True / False / 足りなくて分からない None）。"""
        probe = ItmDecoder(synced=True, resync=False)
        probe._buf = bytearray(self._buf[start:])
        for _ in range(RESYNC_PACKETS):
            if not probe._buf:
                return None
            try:
                result = probe._parse()
            except _Invalid:
                return False
            if result is _INCOMPLETE:
                return None
            probe._consume(result[1])
        return True

    def _parse(self):
        """先頭の 1 パケットを解く。(パケット, 長さ) か _INCOMPLETE を返し、規定に合わなければ _Invalid。"""
        buf, at = self._buf, self._base
        h = buf[0]
        if h == 0x00:
            zeros = 0
            for b in buf:
                if b != 0:
                    break
                zeros += 1
            if zeros == len(buf):
                return _INCOMPLETE
            if buf[zeros] == 0x80 and zeros >= _SYNC_ZEROS:
                return Sync(0, at), zeros + 1
            raise _Invalid("incomplete synchronization packet")
        if h == 0x70:
            return Overflow(at), 1
        if h & 0x0F == 0x00:
            return self._local_timestamp(h, at)
        if h == 0x94:
            payload = self._continuation(4)
            if payload is _INCOMPLETE:
                return _INCOMPLETE
            return self._global1(payload, at), 1 + len(payload)
        if h == 0xB4:
            payload = self._continuation(6)
            if payload is _INCOMPLETE:
                return _INCOMPLETE
            return GlobalTimestamp2(_seven_bit(payload), at), 1 + len(payload)
        if h & 0x0B == 0x08:
            payload = self._continuation(4) if h & 0x80 else []
            if payload is _INCOMPLETE:
                return _INCOMPLETE
            value = (h >> 4) & 0x07 | _seven_bit(payload) << 3
            return Extension(value, (h >> 2) & 1, at), 1 + len(payload)
        if h & 0x03:
            size = _SIZES[h & 0x03]
            if h & 0x04:
                _check_hardware(h >> 3, size)
            if len(buf) < 1 + size:
                return _INCOMPLETE
            value = int.from_bytes(buf[1:1 + size], "little")
            if h & 0x04:
                return _hardware(h >> 3, value, size, at), 1 + size
            return Instrumentation(h >> 3, value, size, at), 1 + size
        raise _Invalid(f"reserved header 0x{h:02X}")

    def _local_timestamp(self, h: int, at: int):
        if h & 0x80 == 0:                                   # 形式 2: 差分はヘッダの中（1〜6）
            return LocalTimestamp((h >> 4) & 0x07, TimestampRelation.SYNC, at), 1
        if h & 0xC0 != 0xC0:
            raise _Invalid(f"reserved header 0x{h:02X}")
        payload = self._continuation(4)
        if payload is _INCOMPLETE:
            return _INCOMPLETE
        return LocalTimestamp(_seven_bit(payload), TimestampRelation((h >> 4) & 0x03), at), 1 + len(payload)

    @staticmethod
    def _global1(payload: list[int], at: int) -> GlobalTimestamp1:
        if len(payload) == 4:                               # 4 バイト目: 0 Wrap ClkCh TS[25:21]
            last = payload[3]
            value = _seven_bit(payload[:3]) | (last & 0x1F) << 21
            return GlobalTimestamp1(value, 26, bool(last & 0x40), bool(last & 0x20), at)
        return GlobalTimestamp1(_seven_bit(payload), 7 * len(payload), offset=at)

    def _continuation(self, limit: int):
        """ヘッダに続く、bit7 が「続く」の印のバイト列（最大 limit バイト）。"""
        payload = []
        for b in self._buf[1:1 + limit]:
            payload.append(b)
            if not b & 0x80:
                return payload
        if len(payload) == limit:
            raise _Invalid("continuation too long")
        return _INCOMPLETE


def _seven_bit(payload: Iterable[int]) -> int:
    value = 0
    for i, b in enumerate(payload):
        value |= (b & 0x7F) << (7 * i)
    return value


def _check_hardware(ident: int, size: int) -> None:
    """DWT の識別子と大きさの組み合わせ（ヘッダだけで分かる誤り）。"""
    allowed = {0: (1,), 1: (2,), 2: (4, 1)}
    if ident in allowed:
        ok = size in allowed[ident]
    elif 8 <= ident <= 15:
        ok = size == (2 if ident & 1 else 4)
    elif 16 <= ident <= 23:
        ok = True
    else:
        raise _Invalid(f"undefined hardware source {ident}")
    if not ok:
        raise _Invalid(f"hardware source {ident} with size {size}")


def _hardware(ident: int, value: int, size: int, at: int) -> Packet:
    if ident == 0:
        return EventCounter(value, at)
    if ident == 1:
        action = (value >> 12) & 0x03
        if action == 0:
            raise _Invalid("reserved exception trace function")
        return ExceptionTrace(value & 0x1FF, ExceptionAction(action), at)
    if ident == 2:
        return PcSample(value if size == 4 else None, at)
    comparator = (ident >> 1) & 0x03
    if ident < 16:
        return DataTraceAddress(comparator, value, at) if ident & 1 else DataTracePc(comparator, value, at)
    return DataTraceValue(comparator, bool(ident & 1), value, size, at)


# --- 時刻の付与 -------------------------------------------------------------------------

@dataclass(frozen=True)
class TimedPacket:
    packet: Packet
    local_time: int | None           # ローカルのタイムスタンプの累積（最初のタイムスタンプからのクロック数）
    global_time: int | None          # グローバルのタイムスタンプ（上位が分からなければ None）
    after_overflow: bool = False     # 同じ区切りにオーバーフローがあった（時刻の基準が怪しい）


class Timestamper:
    """ローカルのタイムスタンプは、それが計る出来事のパケットの後に来る。区切りまでのパケットをためて、
    タイムスタンプが来たらその累積時刻を付けて吐き出す。タイムスタンプのパケット自身は吐き出さない。"""

    def __init__(self):
        self._pending: list[Packet] = []
        self._local = 0
        self._overflow = False
        self._low = 0                    # グローバルの下位 26 ビット
        self._high: int | None = None    # グローバルの上位（ビット 26 から上）

    @property
    def global_time(self) -> int | None:
        return None if self._high is None else self._high << 26 | self._low

    def feed(self, packets: Iterable[Packet]) -> list[TimedPacket]:
        out: list[TimedPacket] = []
        for p in packets:
            if isinstance(p, LocalTimestamp):
                self._local += p.delta
                out += self._release(self._local)
            elif isinstance(p, GlobalTimestamp1):
                mask = (1 << p.bits) - 1
                self._low = (self._low & ~mask) | p.value
                if p.wrap or p.clock_change:
                    self._high = None        # 次の GlobalTimestamp2 まで上位は分からない
            elif isinstance(p, GlobalTimestamp2):
                self._high = p.value
            else:
                self._overflow |= isinstance(p, Overflow)
                self._pending.append(p)
        return out

    def flush(self) -> list[TimedPacket]:
        """最後のタイムスタンプより後のパケット（ローカルの時刻は未定）。"""
        return self._release(None)

    def _release(self, local: int | None) -> list[TimedPacket]:
        out = [TimedPacket(p, local, self.global_time, self._overflow) for p in self._pending]
        self._pending, self._overflow = [], False
        return out


# --- ポートごとの文字列 ---------------------------------------------------------------------

@dataclass(frozen=True)
class TextLine:
    port: int
    text: str


class PortText:
    """スティミュラス・ポートに書かれたバイトを、ポートごとに改行で区切った行にする（printf 用）。"""

    def __init__(self, ports: Iterable[int] | None = None):
        self._ports = None if ports is None else frozenset(ports)
        self._buffers: dict[int, bytearray] = {}

    def feed(self, packets: Iterable[Packet]) -> list[TextLine]:
        out: list[TextLine] = []
        for p in packets:
            if not isinstance(p, Instrumentation) or (self._ports is not None and p.port not in self._ports):
                continue
            buf = self._buffers.setdefault(p.port, bytearray())
            buf += p.data
            while (end := buf.find(b"\n")) >= 0:
                out.append(TextLine(p.port, _text(buf[:end])))
                del buf[:end + 1]
        return out

    def pending(self, port: int) -> str:
        """まだ改行の来ていない途中の文字列。"""
        return _text(self._buffers.get(port, b""))


def _text(data: bytes | bytearray) -> str:
    return bytes(data).rstrip(b"\r").decode("utf-8", errors="replace")
