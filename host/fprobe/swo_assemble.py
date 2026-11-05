"""SWO で受けた ITM のパケットの組み立て（U11）。入出力を持たない。設定の計算は swo_plan。

- `TimedItmStream`: デバイスの時刻付きの SWO のバイト（記録の stream "swo"・イベント記録）を増分で解読し、
  パケットの先頭のバイトの時刻を付ける。
- `DataTraceAssembler`: 同じ比較器の PC（または番地の下位）と値のパケットを 1 回のアクセスにまとめる。
- `CounterTotals`: DWT のイベントカウンタ（8 ビット）の溢れのパケットを数えて 256 倍する。
"""

from dataclasses import dataclass

from .itm import (DataTraceAddress, DataTracePc, DataTraceValue, DecodeError, EventCounter, ItmDecoder, Overflow,
                  Packet, Sync)
from .swo_plan import COUNTERS, ComparatorUse, _window_mask

__all__ = ["CounterTotals", "DataAccess", "DataTraceAssembler", "TimedItmStream"]


# --- 時刻付きの解読 ------------------------------------------------------------------------------

class TimedItmStream:
    """デバイスの時刻付きの SWO のバイト (時刻, バイト) を増分で解読し、(パケットの先頭のバイトの時刻, パケット)。
    synced=False なら最初の同期パケットまで捨てる（ITM が前から動いていて、受信がパケットの途中から始まるとき）。"""

    def __init__(self, synced: bool = True):
        self._decoder = ItmDecoder(synced)
        self._times: list[int] = []
        self._base = 0                    # _times[0] のバイトの通し番号

    @property
    def pending_bytes(self) -> int:
        return len(self._times)

    def feed(self, byte_events) -> list[tuple[int, Packet]]:
        data = bytearray()
        for t, b in byte_events:
            self._times.append(t)
            data.append(b)
        packets = self._decoder.feed(bytes(data))
        out = [(self._times[min(p.offset - self._base, len(self._times) - 1)], p) for p in packets]
        drop = self._decoder.consumed - self._base     # 解読し終えたバイトの時刻は要らない
        del self._times[:drop]
        self._base += drop
        return out


# --- データトレースの組み立て ------------------------------------------------------------------

@dataclass(frozen=True)
class DataAccess:
    t: int                                # デバイスのタイムベース（最初のパケットの先頭のバイト）
    local_time: int | None                # ITM のローカルのタイムスタンプの累積（有効なとき）
    comparator: int
    name: str
    role: str                             # data_trace / value_watch
    pc: int | None
    address: int | None                   # offset を出すとき（または変数の大きさが比較の窓と同じとき）の番地。窓が広く offset が無ければ None
    value: int | None
    size: int | None                      # アクセスの大きさ（値のパケットから）
    write: bool | None
    complete: bool                        # 頼んだパケットがそろった（溢れで欠けると False）


_KINDS = {DataTracePc: "pc", DataTraceAddress: "offset", DataTraceValue: "value"}


class DataTraceAssembler:
    """比較器ごとに、1 回のアクセスのパケット（PC か番地の下位 → 値。ARMv7-M D4.3 の順）を 1 つにまとめる。
    先頭のパケット（PC・番地）が来たら値を待ち、値が来たら出す。値を待っている間に次の先頭が来た・先頭なしに値が来た
    （間が溢れで欠けた）ものは complete=False で出す。Overflow・解読の誤りで組み立て中のものは捨てる。"""

    def __init__(self, comparators: list[ComparatorUse]):
        self._uses = {u.index: u for u in comparators if u.emits}
        masks = {c.index: c.mask for c in comparators}
        self._masks = {i: _window_mask(u, masks) for i, u in self._uses.items()}
        self._pending: dict[int, tuple[int, int | None, dict]] = {}
        self.lost = 0                     # Overflow・解読の誤りで捨てた組み立て中のもの
        self.outside = 0                  # 番地が変数の外（比較の範囲が変数より広い）で捨てたもの
        self.unknown = 0                  # 割り当てていない比較器・頼んでいない種類のパケット

    def feed(self, items) -> list[DataAccess]:
        """items: (時刻, ローカルの時刻か None, パケット) の列。"""
        out: list[DataAccess] = []
        for t, local, p in items:
            if isinstance(p, (Overflow, DecodeError)) or (isinstance(p, Sync) and p.skipped):
                self.lost += len(self._pending)
                self._pending.clear()
                continue
            kind = _KINDS.get(type(p))
            if kind is None:
                continue
            use = self._uses.get(p.comparator)
            if use is None or kind not in use.emits:
                self.unknown += 1
                continue
            pending = self._pending.pop(use.index, None)
            if kind != "value":                                       # 先頭: 前のものは値が来ないまま
                if pending is not None:
                    self._emit(use, pending, out)
                pending = (t, local, {kind: p})
                if "value" in use.emits:
                    self._pending[use.index] = pending
                else:
                    self._emit(use, pending, out)
                continue
            if pending is None:
                pending = (t, local, {})
            elif local is not None and pending[1] is None:
                pending = (pending[0], local, pending[2])
            pending[2]["value"] = p
            self._emit(use, pending, out)
        return out

    def flush(self) -> list[DataAccess]:
        out: list[DataAccess] = []
        for index in list(self._pending):
            self._emit(self._uses[index], self._pending[index], out)
        return out

    def _emit(self, use: ComparatorUse, pending, out: list[DataAccess]) -> None:
        self._pending.pop(use.index, None)
        t, local, parts = pending
        pc = parts.get("pc")
        offset = parts.get("offset")
        value = parts.get("value")
        address = None
        if offset is not None:
            address = (use.comp & ~0xFFFF) | offset.address_low
        elif use.address is not None and (1 << self._masks[use.index]) <= max(use.size, 1):
            address = use.address
        if address is not None and use.address is not None and use.role == "data_trace" \
                and not use.address <= address < use.address + use.size:
            self.outside += 1
            return
        out.append(DataAccess(t, local, use.index, use.name, use.role, pc.pc if pc else None, address,
                              value.value if value else None, value.size if value else None,
                              value.write if value else None, set(parts) == set(use.emits)))


# --- イベントカウンタ ----------------------------------------------------------------------------

class CounterTotals:
    """EventCounter のパケット（8 ビットのカウンタが溢れた印）を数える。合計はカウンタの増分の 256 単位の部分。"""

    def __init__(self):
        self.overflows = {name: 0 for name in COUNTERS}
        # 溢れの印ごとの (時刻 = その印のパケットの先頭のバイトの時刻か None, カウンタ, その時点の合計)。feed_timed から
        self.events: list[tuple[int | None, str, int]] = []

    def feed(self, packets) -> None:
        self.feed_timed((None, p) for p in packets)

    def feed_timed(self, items) -> None:
        """(時刻, パケット) の列。溢れの印を数え、印ごとの時刻を events に残す（最後のバイトの時刻で代えない）。"""
        for t, p in items:
            if isinstance(p, EventCounter):
                for name, (_, _, flag) in COUNTERS.items():
                    if p.flags & flag:
                        self.overflows[name] += 1
                        if t is not None:
                            self.events.append((t, name, 256 * self.overflows[name]))

    def as_dict(self) -> dict[str, int]:
        return {name: 256 * n for name, n in self.overflows.items()}
