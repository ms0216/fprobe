"""ロジアナの流し続けを共通の時間軸の記録へ（U23: 日単位の連続記録）。

`LaStream`（深いバッファを FIFO にした連続ストリーミング）を別スレッドで受け取り、chunk_s 秒ごとの波形のかたまりに
して記録（Recorder / SegmentedRecorder）の add_waveform へ入れる。長期の記録と組むと、波形は分割ごとのファイルに
なり、縮図（かたまりの要約）は索引に入る。

- 時刻の基準（サンプル 0 のタイムベース）: 流し始める前後にタイムベースを読み、深いバッファのトリガ時刻（条件なしの
  トリガは最初のサンプルで成立する）がその間にあればそれを使う（不確かさ = 数サンプル）。無ければ前後の読みの中点
  （不確かさ = 往復の半分）。基準は記録の stream "la"（kind "stream_start"）に残す。
- 機器の呼び出しは 1 回ずつロックの中で（`LockedDevice`）。流している間もほかの道具（デバッガ等）は動く。
- 深いバッファとイベント記録は読み出しの経路を共有するので、流している間は collector のイベント記録の取り込みを
  止める（呼び手）。イベントも流す構成（ケイパビリティ la_stream_events。R1）では、イベントも同じ流れで届くので、
  events_from（collector が読んだところの続き）から記録へ入れ（add_events。ポストされた読みの持ち越しも引き継ぐ）、
  終わったら続きの番号（events_next）と持ち越し（carry）を呼び手が collector に返す。
"""

from __future__ import annotations

import contextlib
import threading
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import generated as g
from .collector import add_events
from .events import DapEvent
from .la import DeepAnalyzer, LaStream, Transitions

__all__ = ["LaRecordingStream", "LockedDevice", "StreamAnchor", "anchor_stream"]

STREAM = "la"
TRIGGER_SLACK_SAMPLES = 4          # 符号器がトリガを見るまでの遅れ（数サンプル）を不確かさに入れる


class LockedDevice:
    """機器の呼び出しを 1 回ずつ lock の中で行う代理（長い受け取りの間も、ほかの道具が割り込める）。"""

    def __init__(self, device: Any, lock: Any):
        self._device, self._lock = device, lock

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._device, name)
        if not callable(attr):
            return attr

        def locked(*args, **kwargs):
            with self._lock:
                return attr(*args, **kwargs)
        return locked


@dataclass(frozen=True)
class StreamAnchor:
    ticks: int                     # サンプル 0 のタイムベース
    uncertainty: float             # タイムベースの単位
    method: str                    # trigger / host


def anchor_stream(device: Any, start: Callable[[], LaStream], timebase_hz: float) -> tuple[LaStream, StreamAnchor]:
    """流し始め、サンプル 0 の時刻を決める。"""
    before = device.time()
    stream = start()
    after = device.time()
    status = device.read32(g.LA_DEEP_STATUS)
    if status >> g.LA_DEEP_STATUS_TRIGGERED[0] & 1:
        lo, hi = device.read(g.LA_DEEP_TRIGGER_TIME_LO, 2)
        t = hi << 32 | lo
        if before <= t <= after:
            return stream, StreamAnchor(t, TRIGGER_SLACK_SAMPLES * stream.sample_period_s * timebase_hz, "trigger")
    return stream, StreamAnchor((before + after) // 2, (after - before) / 2, "host")


class LaRecordingStream:
    """ロジアナの流し続けを記録へ入れる（別スレッド）。stop で止め、残りを出し切ってから終わる。"""

    def __init__(self, device: Any, rec: Any, *, rate_hz: float | None = None, rle: bool = True,
                 chunk_s: float = 1.0, lock: Any = None, on_end: Callable[[LaRecordingStream], None] | None = None,
                 events_from: int | None = None, carry: DapEvent | None = None, obs: Any = None):
        if not chunk_s > 0:
            raise ValueError("chunk_s は正の秒数")
        self.obs = obs                                     # FPGA の観測の復号器（Collector と共有）
        self.rec = rec
        self._lock = lock if lock is not None else contextlib.nullcontext()
        self.device = LockedDevice(device, self._lock)
        self.rate_hz, self.rle, self.chunk_s = rate_hz, rle, chunk_s
        self._on_end = on_end
        self.stream: LaStream | None = None
        self.anchor: StreamAnchor | None = None
        self.blocks = 0
        self.error: str | None = None
        self.events_from = events_from                            # イベントも流す構成: 最初のイベントの番号
        self.carry = carry                                        # 値がまだ返っていない AP の読み（持ち越し）
        self.events = 0                                           # 記録へ入れたイベントの数
        self._thread: threading.Thread | None = None

    def start(self) -> StreamAnchor:
        analyzer = DeepAnalyzer(self.device)
        self.stream, self.anchor = anchor_stream(
            self.device, lambda: analyzer.stream(rate_hz=self.rate_hz, rle=self.rle, events_from=self.events_from),
            self.rec.timebase_hz)
        s = self.stream
        self.rec.add(STREAM, "stream_start", self.anchor.ticks, uncertainty=self.anchor.uncertainty,
                     body={"rate_hz": 1 / s.sample_period_s, "rle": self.rle, "channels": s.channels,
                           "anchor": self.anchor.method, "events": s.carries_events})
        self._thread = threading.Thread(target=self._run, name="fprobe-la-record", daemon=True)
        self._thread.start()
        return self.anchor

    @property
    def carries_events(self) -> bool:
        """イベントも同じ流れで記録へ入れている（イベントも流す構成）。"""
        return self.stream is not None and self.stream.carries_events

    @property
    def events_next(self) -> int | None:
        """次に取り込むべきイベントの番号（イベントも流す構成で。collector に返す）。"""
        return self.stream.events_next if self.carries_events else None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout_s: float = 30.0) -> dict:
        if self.stream is not None:
            self.stream.stop()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
        return self.status()

    def status(self) -> dict:
        out: dict = {"running": self.running, "blocks": self.blocks, "error": self.error,
                     "events": self.events if self.carries_events else None}
        if self.stream is not None:
            st = self.stream.stats()
            out.update(samples=st.samples, duration_s=st.duration_s, mb_per_s=st.mb_per_s, overrun=st.overrun,
                       sample_period_ns=st.sample_period_s * 1e9)
        if self.anchor is not None:
            out["anchor"] = {"ticks": self.anchor.ticks, "uncertainty_ticks": self.anchor.uncertainty,
                             "method": self.anchor.method}
        return out

    # --- 受け取り -------------------------------------------------------------------------------

    def _run(self) -> None:
        s, anchor = self.stream, self.anchor
        tps = s.sample_period_s * self.rec.timebase_hz
        per_block = max(1, round(self.chunk_s / s.sample_period_s))
        values, starts = array("H"), array("Q")
        block_first, carry, end = 0, 0, 0                         # carry: かたまりの頭で続いている値

        def emit(upto: int) -> None:
            nonlocal values, starts, block_first, carry
            if upto <= block_first:
                return
            if not len(starts) or starts[0] != block_first:
                values.insert(0, carry)
                starts.insert(0, block_first)
            carry = values[-1]
            rel = array("Q", (x - block_first for x in starts))
            tr = Transitions(values, rel, upto - block_first, 0, 0, s.sample_period_s, s.channels)
            self.rec.add_waveform(tr, anchor.ticks + round(block_first * tps))
            self.blocks += 1
            values, starts, block_first = array("H"), array("Q"), upto

        try:
            for chunk in s.chunks(float("inf")):
                if chunk.events:                                  # 時刻の 48 ビットは受け取った後の時刻で直す
                    self.carry = add_events(self.rec, list(chunk.events), self.device.time(), carry=self.carry,
                                            obs=self.obs)
                    self.events += len(chunk.events)
                if not chunk.data:
                    continue
                v, st = chunk.transitions()
                values.extend(v)
                starts.extend(st)
                end = chunk.first + chunk.length
                if end - block_first >= per_block:
                    emit(end)
        except Exception as e:                                    # USB の誤り等: それまでの分は記録に残る
            self.error = f"{type(e).__name__}: {e}"
        finally:
            emit(end)
            st = s.stats()
            self.rec.add(STREAM, "stream_stop", anchor.ticks + round(end * tps),
                         body={"samples": st.samples, "overrun": st.overrun, "error": self.error,
                               "events": self.events if s.carries_events else None})
            if st.overrun:
                self.rec.add("annotation", "la_overrun", anchor.ticks + round(end * tps),
                             body={"text": "ロジアナの流し続けが溢れて止まった（ホストが遅れた）"})
            self.rec.flush()
            if self._on_end is not None:
                self._on_end(self)
