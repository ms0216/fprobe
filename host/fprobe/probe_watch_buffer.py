"""変数のライブ観測の、ターゲット側のバッファの吸い出し（watch_buffer_*。配列 + 書いた数をホストが周期で読む）の部品（U43）。

WatchService（fprobe.probe_watch）に mixin される。リングの読みの本体は fprobe.watch_buffer。
"""

import collections
from dataclasses import dataclass, field

from .debug import PyocdAccess
from .dwarf.cexpr import evaluate
from .dwarf.typesys import ArrayType, BaseType, EnumType, PointerType, strip, type_name
from .watch_buffer import TargetRing
from .watch_value import _frame

BUFFER_LIMIT = 200_000                 # 吸い出した要素を残す数


@dataclass
class _Buffer:
    ring: TargetRing
    array: str
    head: str
    rate_hz: float
    samples: collections.deque = field(default_factory=lambda: collections.deque(maxlen=BUFFER_LIMIT))
    cursor: int = 0                                       # 既定の読み手の次の通し番号


class WatchBufferMixin:
    """ターゲット側のバッファの吸い出し。"""

    def watch_buffer_start(self, array: str, head: str, counter: bool = True, rate_hz: float = 100.0) -> dict:
        """ターゲット側のバッファを吸い出す（STM32CubeMonitor の Snapshot 方式）。ファームが配列 array に値（構造体でも）
        を書き、head を進める（counter = True: 書いた総数。False: 次に書く位置）。PC は rate_hz ごとに head を読み、増えた
        要素だけを読んで DWARF の型で解釈する（速い変化を取りこぼさず、ターゲットの周期に同期する）。読みは
        watch_buffer_read。1 つだけ（呼び直すと前のを止める）。"""
        if not 0 < rate_hz <= 10_000:
            raise ValueError("rate_hz は 0 より大きく 10000 以下")
        info = self._watch_info()
        if info is None:
            raise RuntimeError("ELF に DWARF が無い（watch_buffer は配列の型を使う）")
        with self._host._lock:
            access = PyocdAccess(self._host.target)
            frame = _frame(access.read_memory)
            va, vh = evaluate(array, info, frame), evaluate(head, info, frame)
            s = strip(va.type)
            if not isinstance(s, ArrayType) or va.address is None or s.length is None or not s.element.size:
                raise ValueError(f"{array!r} は長さの分かるメモリ上の配列ではない（{type_name(va.type)}）")
            hs = strip(vh.type)
            if vh.address is None or vh.bit_size is not None or not isinstance(hs, (BaseType, EnumType, PointerType)) \
                    or hs.size not in (1, 2, 4, 8):
                raise ValueError(f"{head!r} は整数の変数ではない（{type_name(vh.type)}）")
            ring = TargetRing(s.element, va.address, s.length, vh.address, hs.size, counter)
            ring.poll(access.read_memory)                                  # いまの位置に合わせる
            self._watch_buffer = _Buffer(ring, array, head, rate_hz)
            self._host._add_job("watch.buffer", 1.0 / rate_hz, self._buffer_once)
        return self._buffer_info()

    def _buffer_once(self) -> None:
        """背景の仕事（_lock の中で）: 増えた要素を吸い出す。"""
        buf = self._watch_buffer
        if buf is None:
            self._host._remove_job("watch.buffer")
            return
        got = buf.ring.poll(PyocdAccess(self._host.target).read_memory)
        if got:
            t = self._host.session.device.time()
            buf.samples.extend((seq, t, value) for seq, value in got)

    def _buffer_info(self) -> dict:
        buf = self._watch_buffer
        if buf is None:
            raise RuntimeError("吸い出していない（watch_buffer_start を先に）")
        r = buf.ring
        return {"array": buf.array, "head": buf.head, "counter": r.counter, "rate_hz": buf.rate_hz,
                "address": f"0x{r.base:08X}", "length": r.length, "element": type_name(r.element),
                "total": r.seq, "lost": r.lost, "error": self._host._job_errors.get("watch.buffer")}

    def watch_buffer_read(self, since: int | None = None, limit: int = 1000) -> dict:
        """吸い出した要素（機器には触れない）: samples = [{"seq"（通し番号）, "time_us"（PC が読んだ時刻）, "value"}]。
        since を省くと前回の続きから（読むと進む）。since を渡すとそこから（画面用。進まない）。lost は一周されて
        読めなかった要素の数、evicted はホストで持つ数の上限（kept_limit = BUFFER_LIMIT）を超えて捨てた、since より後の
        要素の数、next は次の since。"""
        with self._host._lock:                                                   # 背景の吸い出しと取り合わない
            buf = self._watch_buffer
            if buf is None:
                raise RuntimeError("吸い出していない（watch_buffer_start を先に）")
            first = buf.cursor if since is None else since
            items = [s for s in buf.samples if s[0] >= first][:limit]
            nxt = items[-1][0] + 1 if items else max(first, buf.samples[0][0] if buf.samples else first)
            if since is None:
                buf.cursor = nxt
            hz = self._host.session.device.capabilities().timebase_hz
            evicted = max(0, buf.samples[0][0] - first) if buf.samples else 0
            return {"samples": [{"seq": seq, "time_us": t * 1e6 / hz, "value": v} for seq, t, v in items],
                    "next": nxt, "evicted": evicted, "kept_limit": BUFFER_LIMIT, **self._buffer_info()}

    def watch_buffer_stop(self) -> dict:
        """吸い出しを止める（吸い出した要素は watch_buffer_read で読める）。"""
        with self._host._lock:
            self._host._remove_job("watch.buffer")
            return self._buffer_info()
