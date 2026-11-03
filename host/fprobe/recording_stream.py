"""ロジアナの流し続けを共通の時間軸の記録へ（U23: 日単位の連続記録）。

`LaStream`（深いバッファを FIFO にした連続ストリーミング）を別スレッドで受け取り、chunk_s 秒ごとの波形のかたまりに
して記録（Recorder / SegmentedRecorder）の add_waveform へ入れる。長期の記録と組むと、波形は分割ごとのファイルに
なり、縮図（かたまりの要約）は索引に入る。

- 時刻の基準（サンプル 0 のピンの時間軸の時刻。docs/superpowers/specs/2026-11-01-time-accuracy-design.md）: 回路が
  サンプル 0 の時刻を記録する構成（TLV TIMING の la_deep_start を申告する。段階 S1 の R1）は、CSR la_deep.start_time の
  1 点（fprobe.timing.stream_start_recorded。method "circuit"、不確かさ 0）。記録しない古い回路は、流し始める前後の
  タイムベースの読みと、深いバッファのトリガ時刻（条件なしのトリガは最初のサンプル以後に成立する: サンプル 0 はそれより
  後ではない）から、確かに言える範囲 [t_lo, t_hi] だけを出す（fprobe.timing.stream_start_window。範囲の中のどこかは
  推定しない）。波形は範囲の中点に置き、記録の不確かさ（± 範囲の半分）がちょうど範囲を覆う。範囲と根拠は記録の
  stream "la"（kind "stream_start"）に残す。1 周期に複数のサンプルの深いバッファ（TLV TIMING の la_deep_lanes。段階 S1b）は、
  回路の起点 start_time から細かい辺 start_time × lanes − latency（fprobe.timing.stream_start_lanes）。記録の時刻は刻みの整数なので
  切り捨ての刻みに置き、不確かさは端数（細かい辺は basis.fine_sample に残す）。
  ch ごとの時刻のずれ（fprobe.timing.la_channel_skew）は記録の波形に持たせる。
- 機器の呼び出しは 1 回ずつロックの中で（`LockedDevice`）。流している間もほかの道具（デバッガ等）は動く。
- 深いバッファとイベント記録は読み出しの経路を共有するので、流している間は collector のイベント記録の取り込みを
  止める（呼び手）。イベントも流す構成（ケイパビリティ la_stream_events。R1）では、イベントも同じ流れで届くので、
  events_from（collector が読んだところの続き）から記録へ入れ（add_events。ポストされた読みの持ち越しも引き継ぐ）、
  終わったら続きの番号（events_next）と持ち越し（carry）を呼び手が collector に返す。
- 欠けの区間（gaps = True。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）: 回路が拍を捨てた区間は、記録の stream "la" の
  出来事 "gap"（時刻 = 欠けの最初のサンプルの位置の時刻、t_end = 欠けの後の最初のサンプルの時刻、body = samples・duration_ticks・beat）にし、波形のかたまりはその前で閉じて
  欠けの後から新しく始める（欠けの区間に値を置かない）。時刻は値のあるサンプルと同じ式（起点 + 位置 × サンプルの間隔）。
"""

from __future__ import annotations

import contextlib
import threading
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import generated as g
from .collector import add_events, observe_reference
from .events import DapEvent
from .la import DeepAnalyzer, LaStream, Transitions
from .la_wave import _np
from .timing import source_timing, stream_start_lanes, stream_start_recorded, stream_start_window

_DTYPES = {"H": "uint16", "Q": "uint64"}


class _Pieces:
    """受け取りごとの変化点の配列（numpy があれば StreamChunk.changes の numpy の配列、無ければ array）を、かたまりを
    閉じるまで並べて持つ（受け取りのたびに 1 つの配列へ写し足さない）。"""

    def __init__(self, code: str):
        self.code, self._parts = code, []

    def add(self, part) -> None:
        if len(part):
            self._parts.append(part)

    def join(self):
        if _np is not None:
            dtype = _np.dtype(_DTYPES[self.code])
            if not self._parts:
                return _np.empty(0, dtype=dtype)
            return _np.concatenate([_np.asarray(p, dtype=dtype) for p in self._parts])
        out = array(self.code)
        for p in self._parts:
            out.extend(p)
        return out


def _prepend(values, first: int, code: str):
    """values の頭に first を 1 つ足した新しい配列。"""
    if _np is not None:
        return _np.concatenate([_np.array([first], dtype=_DTYPES[code]), values])
    return array(code, [first]) + values


def _relative(starts, first: int):
    """通し番号の開始から first を引いたもの（かたまりの中の位置）。numpy があれば一括で。"""
    if _np is not None:
        return starts - _np.uint64(first)
    return array("Q", (x - first for x in starts))

__all__ = ["LaRecordingStream", "LockedDevice", "StreamAnchor", "anchor_stream"]

STREAM = "la"


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
    """サンプル 0 のピンの時間軸の時刻: 範囲 [t_lo, t_hi]（両端を含む）と、波形を置いた時刻 ticks（範囲の中点）。"""
    ticks: int                     # 波形を置いた時刻（範囲の中点。推定ではなく置き場所の決まり）
    uncertainty: float             # 範囲の半分（ticks ± これが範囲を覆う。記録の不確かさの列。細かいサンプルの構成は刻みの端数）
    method: str                    # "circuit"（回路が記録した起点の 1 点）/ 範囲の終わりを決めたもの: "trigger"（トリガ時刻）/
                                   # "host"（後の読み）
    t_lo: int = 0
    t_hi: int = 0
    basis: dict | None = None      # fprobe.timing.stream_start_recorded / stream_start_window の根拠（起点・前後の読み・トリガ時刻・段数）


def anchor_stream(device: Any, start: Callable[[], LaStream], timebase_hz: float | None = None) -> tuple[LaStream, StreamAnchor]:
    """流し始め、サンプル 0 の時刻（範囲）を決める（timebase_hz は使わない。古い呼び手のための引数）。"""
    caps = device.capabilities()
    before = device.time()
    stream = start()
    after = device.time()
    recorded = source_timing(caps, "la_deep_start")
    lanes_point = source_timing(caps, "la_deep_lanes")
    start_valid = None
    if lanes_point is not None and lanes_point["delay"] is not None and lanes_point["unit"] == "fine_sample":
        # 1 周期に複数のサンプルの深いバッファ（段階 S1b の A9）: 回路が最初に符号にした語の時刻 start_time を持つ。
        # サンプル 0 は細かい辺 start_time × lanes − latency（刻みに直すと端数が出る: 波形は切り捨ての刻みに置き、
        # 不確かさは端数の分。fprobe.timing.stream_start_lanes）
        start_valid = bool(device.read32(g.LA_DEEP_INFO) >> g.LA_DEEP_INFO_START_VALID[0] & 1)
        if start_valid:
            lo, hi = device.read(g.LA_DEEP_START_TIME_LO, 2)
            lanes = caps.la_sample_hz // caps.sys_clock_hz
            w = stream_start_lanes(hi << 32 | lo, lanes, lanes_point["delay"], before, after)
            frac = (w["fine_sample"] - w["t_lo"] * lanes) / lanes
            basis = {**w["basis"], "fine_sample": w["fine_sample"]}
            return stream, StreamAnchor(w["t_lo"], frac, "circuit", w["t_lo"], w["t_hi"], basis)
    elif recorded is not None and recorded["delay"] is not None and recorded["span"] is not None:
        # 回路がサンプル 0 の時刻を記録する（R1）。start_valid は run の 0→1 で 0 に戻り、最初のサンプルで 1 になる
        # （最初のサンプルは run の数周期後なので、ふつうは後の読みより前に記録される）
        start_valid = bool(device.read32(g.LA_DEEP_INFO) >> g.LA_DEEP_INFO_START_VALID[0] & 1)
        if start_valid:
            lo, hi = device.read(g.LA_DEEP_START_TIME_LO, 2)
            w = stream_start_recorded(hi << 32 | lo, recorded["delay"], recorded["span"], before, after)
            return stream, StreamAnchor(w["t_lo"], 0.0, "circuit", w["t_lo"], w["t_hi"], w["basis"])
    status = device.read32(g.LA_DEEP_STATUS)
    trigger = None
    if status >> g.LA_DEEP_STATUS_TRIGGERED[0] & 1:
        lo, hi = device.read(g.LA_DEEP_TRIGGER_TIME_LO, 2)
        trigger = hi << 32 | lo
    w = stream_start_window(before, after, trigger, caps.la_delay_deep, caps.la_phase_span)
    if start_valid is not None:                           # 記録する回路なのに、読んだ時に起点がまだ無かった（事実として残す）
        w["basis"]["start_valid"] = start_valid
    t_lo, t_hi = w["t_lo"], w["t_hi"]
    method = "trigger" if w["basis"]["bounds"]["t_hi"] == "trigger" else "host"
    return stream, StreamAnchor((t_lo + t_hi) // 2, (t_hi - t_lo) / 2, method, t_lo, t_hi, w["basis"])


class LaRecordingStream:
    """ロジアナの流し続けを記録へ入れる（別スレッド）。stop で止め、残りを出し切ってから終わる。"""

    def __init__(self, device: Any, rec: Any, *, rate_hz: float | None = None, rle: bool = True,
                 chunk_s: float = 1.0, lock: Any = None, on_end: Callable[[LaRecordingStream], None] | None = None,
                 events_from: int | None = None, carry: DapEvent | None = None, obs: Any = None,
                 pack_channels: int | None = None, gaps: bool = False, gap_hold_bytes: int | None = None,
                 control_read_retries: int = 0):
        if not chunk_s > 0:
            raise ValueError("chunk_s は正の秒数")
        self.obs = obs                                     # FPGA の観測の復号器（Collector と共有）
        self.rec = rec
        self._lock = lock if lock is not None else contextlib.nullcontext()
        self.device = LockedDevice(device, self._lock)
        self.rate_hz, self.rle, self.chunk_s = rate_hz, rle, chunk_s
        self.pack_channels = pack_channels                 # 使うチャンネルだけ詰めて流す（8・4・2。None = 詰めない）
        self.gaps = gaps                                   # 溢れても止めず欠けの区間を記録して続ける
        self.gap_hold_bytes = gap_hold_bytes               # 同: 位置の決まらない拍を持つ上限（gaps のとき使う側が明示する）
        self.control_read_retries = control_read_retries   # 見回りの READ を時間切れで送り直してよい回数（使う側が明示する。既定 0）
        self._on_end = on_end
        self.stream: LaStream | None = None
        self.anchor: StreamAnchor | None = None
        self.blocks = 0
        self.error: str | None = None
        self.events_from = events_from                            # イベントも流す構成: 最初のイベントの番号
        self.carry = carry                                        # 持ち越し（値がまだ返っていない AP の読み・PHY の設定。collector.Carry）
        caps = device.capabilities()
        self._line_time = caps.dap_line_time                      # DAP の時刻が線上の点（time-accuracy S2）
        self._output_ticks = caps.dap_output_ticks                # その点から端子までの段
        self.events = 0                                           # 記録へ入れたイベントの数
        self._skew: tuple[float | None, ...] = ()                 # ch ごとの時刻のずれ（start で決まる）
        self._thread: threading.Thread | None = None

    def start(self) -> StreamAnchor:
        analyzer = DeepAnalyzer(self.device)
        self.stream, self.anchor = anchor_stream(
            self.device, lambda: analyzer.stream(rate_hz=self.rate_hz, rle=self.rle, events_from=self.events_from,
                                    pack_channels=self.pack_channels, gaps=self.gaps,
                                    gap_hold_bytes=self.gap_hold_bytes,
                                    control_read_retries=self.control_read_retries),
            self.rec.timebase_hz)
        s = self.stream
        ticks = s.sample_period_s * self.rec.timebase_hz
        if abs(ticks - round(ticks)) > 1e-6 or round(ticks) < 1:
            # 記録の時刻はタイムベースの刻み（整数）。刻みより細かいサンプル（1 周期に複数のサンプルの構成の 800 MS/s など）を
            # 記録に入れると、変化の時刻が刻みに丸まって細かさを黙って失う。始めずに断る（VCD への垂れ流し la_stream は細かさを保つ）
            for _ in s.chunks(0):
                pass
            self.stream = None
            raise ValueError(f"サンプルの間隔 {s.sample_period_s * 1e9:g} ns がタイムベースの刻み（{1e9 / self.rec.timebase_hz:g} ns）の"
                             "整数倍でないので、記録に入れると時刻が丸まる。刻みの整数倍のサンプルレートにするか、la_stream（VCD）を使う")
        self._skew = tuple(analyzer.channel_skew[:s.channels])   # ch ごとの時刻のずれ（fprobe.timing）
        self.rec.add(STREAM, "stream_start", self.anchor.ticks, uncertainty=self.anchor.uncertainty,
                     body={"rate_hz": 1 / s.sample_period_s, "rle": self.rle, "channels": s.channels, "pack_channels": self.pack_channels,
                           "gaps": self.gaps,
                           "anchor": self.anchor.method, "events": s.carries_events,
                           "t_lo": self.anchor.t_lo, "t_hi": self.anchor.t_hi, "basis": self.anchor.basis,
                           "channel_skew": list(self._skew)})
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
                       sample_period_ns=st.sample_period_s * 1e9, link_buffer=st.link_buffer_dict,
                       gaps=len(st.gaps), gap_samples=st.gap_samples, unplaced_beats=st.unplaced_beats, control=st.control)
            if getattr(self.device.capabilities(), "la_stream_usage", False) and self.running:
                out["usage"] = self.stream.usage()            # リングと帯域の使用量の事実（読むたびに。判定はしない）
        if self.anchor is not None:
            out["anchor"] = {"ticks": self.anchor.ticks, "uncertainty_ticks": self.anchor.uncertainty,
                             "method": self.anchor.method, "t_lo": self.anchor.t_lo, "t_hi": self.anchor.t_hi,
                             "basis": self.anchor.basis}
        return out

    # --- 受け取り -------------------------------------------------------------------------------

    def _run(self) -> None:
        s, anchor = self.stream, self.anchor
        tps = s.sample_period_s * self.rec.timebase_hz
        per_block = max(1, round(self.chunk_s / s.sample_period_s))
        values, starts = _Pieces("H"), _Pieces("Q")              # かたまりの変化点（受け取りごとの配列を並べたもの）
        block_first, carry, end = 0, 0, 0                         # carry: かたまりの頭で続いている値
        # 生の拍で保存する記録（記録の形式の版 3。waveform_format = 2）: 変化点にせず、拍をそのままかたまりのファイルへ
        # （変化点にするのは読み手。docs/superpowers/specs/2026-11-03-recording-raw-waveform-design.md）
        raw = self.rec.waveform_format == 2
        pieces: list = []                                         # かたまりの拍（受け取ったバイト列。バッファは写さずに指しているだけなので持つ分は写す）
        block_previous: int | None = None                         # かたまりの最初のサンプルの直前の値

        def emit(upto: int) -> None:
            nonlocal values, starts, block_first, carry, pieces
            if upto <= block_first:
                return
            if raw:
                self.rec.add_raw_waveform(pieces, rle=self.rle, pack=s.pack, channels=s.channels, period_s=s.sample_period_s,
                                          previous=block_previous, start_ticks=anchor.ticks + round(block_first * tps),
                                          length=upto - block_first, channel_skew=self._skew)
                self.blocks += 1
                pieces, block_first = [], upto
                return
            v, st = values.join(), starts.join()
            if not len(st) or int(st[0]) != block_first:          # 頭は続いている値から始める
                v, st = _prepend(v, carry, "H"), _prepend(st, block_first, "Q")
            carry = int(v[-1])
            tr = Transitions(v, _relative(st, block_first), upto - block_first, 0, 0, s.sample_period_s, s.channels,
                             self._skew)
            self.rec.add_waveform(tr, anchor.ticks + round(block_first * tps))
            self.blocks += 1
            values, starts, block_first = _Pieces("H"), _Pieces("Q"), upto

        try:
            for chunk in s.chunks(float("inf")):
                if chunk.events:                                  # 時刻の 48 ビットは受け取った後の時刻で直す
                    reference = self.device.time()
                    if not observe_reference(self.rec, reference):         # タイムベースが 0 に戻った: 記録は終わった（§14.1）
                        self.error = "the timebase went back (restarted): the recording ended"
                        break
                    self.carry = add_events(self.rec, list(chunk.events), reference, carry=self.carry,
                                            obs=self.obs, line_time=self._line_time,
                                            output_ticks=self._output_ticks)
                    self.events += len(chunk.events)
                if chunk.gap is not None:                         # 欠け: そこまでの波形を閉じ、欠けを出来事にして、後から始め直す
                    emit(chunk.gap.start)
                    gap = chunk.gap
                    self.rec.add(STREAM, "gap", anchor.ticks + round(gap.start * tps),
                                 t_end=anchor.ticks + round((gap.start + gap.samples) * tps),
                                 body={"samples": gap.samples, "duration_ticks": round(gap.samples * tps), "beat": gap.beat})
                    values, starts, block_first = _Pieces("H"), _Pieces("Q"), chunk.first
                    end = chunk.first
                if not chunk.data:
                    continue
                if raw:                                          # 復号しない: 受け取った拍をそのまま持つ
                    if not pieces:
                        block_previous = chunk.previous
                    pieces.append(chunk.data if isinstance(chunk.data, bytes) else bytes(chunk.data))
                else:
                    v, st = chunk.changes()                     # numpy があれば numpy の配列のまま（写さない）
                    values.add(v)
                    starts.add(st)
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
                               "gaps": len(st.gaps), "gap_samples": st.gap_samples, "unplaced_beats": st.unplaced_beats,
                               "events": self.events if s.carries_events else None,
                               "link_buffer": st.link_buffer_dict})
            if st.overrun:
                self.rec.add("annotation", "la_overrun", anchor.ticks + round(end * tps),
                             body={"text": "ロジアナの流し続けが溢れて止まった（ホストが遅れた）"})
            self.rec.flush()
            if self._on_end is not None:
                self._on_end(self)
