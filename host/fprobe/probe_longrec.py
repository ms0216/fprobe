"""Probe の道具: 長期の記録（U23）。分割・間引き・容量の上限の方針で記録を始める、分割の一覧、全体の縮図（事前計算）、
ロジアナの流し続けを記録へ。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・session・_recorder・_collector・_reading・_la・_deep を
使う。機器に触るのは _lock の中（流し続けは機器の呼び出しごとに _lock を取る: fprobe.recording_stream）。

仕様: docs/superpowers/specs/2026-10-18-u23-u27-long-recording-heap-design.md
"""

from pathlib import Path

from .recording import Recorder, Recording
from .recording_long import DEFAULT_DECIMATE, Policy, SegmentedRecorder, SegmentedRecording
from .recording_stream import LaRecordingStream

__all__ = ["LongRecordingTools"]


class LongRecordingTools:
    def _init_longrec(self) -> None:
        self._la_record: LaRecordingStream | None = None

    # --- 記録を始める（recording_start から） ---------------------------------------------------------

    @staticmethod
    def _make_recorder(directory: Path, timebase_hz: float, meta: dict, segment_s: float | None,
                       segment_bytes: int | None, max_bytes: int | None, decimate_after_s: float | None,
                       decimate_streams: list[str] | None) -> Recorder | SegmentedRecorder:
        """方針のどれかを指定すれば長期の記録（分割）、無ければ普通の記録。"""
        if all(v is None for v in (segment_s, segment_bytes, max_bytes, decimate_after_s, decimate_streams)):
            return Recorder(directory, timebase_hz=timebase_hz, meta=meta)
        policy = Policy(segment_s=3600.0 if segment_s is None else segment_s, segment_bytes=segment_bytes,
                        max_bytes=max_bytes, decimate_after_s=decimate_after_s,
                        decimate_streams=DEFAULT_DECIMATE if decimate_streams is None else tuple(decimate_streams))
        return SegmentedRecorder(directory, timebase_hz, meta=meta, policy=policy)

    # --- ロジアナの流し続け -----------------------------------------------------------------------

    def _la_idle(self) -> None:
        """ロジアナ・イベント記録の道具の前に（_lock の中で）: 記録へ流している間は使えない。"""
        if self._la_record is not None and self._la_record.running:
            raise RuntimeError("ロジアナを記録へ流している（recording_la_stream_stop を先に）")

    def recording_la_stream_start(self, rate_hz: float | None = None, rle: bool = True, chunk_s: float = 1.0) -> dict:
        """記録中に、ロジアナを止めずに流し続けて記録へ入れる（止めるまで。日単位も可。chunk_s 秒ごとのかたまり）。
        イベントも流す構成（probe_design_get の features.la_stream_events。fprobe_la）では、イベント記録（DAP・UART・
        SWO の出来事）も同じ流れでデバイスの時刻のまま記録へ入り続ける（events_read 等の道具は流している間は使えない）。
        その他の構成では、流している間はイベント記録を取り込めない（読み出しの経路が同じ）。
        ロジアナの道具（la_capture 等）も使えない。"""
        with self._lock:
            if self._recorder is None:
                raise RuntimeError("記録していない（recording_start を先に）")
            self._la_idle()
            if self._la is not None or getattr(self, "_deep", None) is not None:
                raise RuntimeError("ロジアナの捕捉を仕掛けている（la_collect / la_deep_collect を先に）")
            device = self.session.device
            mixed = bool(getattr(device.capabilities(), "la_stream_events", False))
            events_from, carry = None, None
            if self._collector is not None:
                if mixed:                                          # いままでの出来事を取り込み、続きを流れに渡す
                    events_from, carry = self._collector.hand_over()
                else:
                    self._collector.pause_events(True)
            stream = LaRecordingStream(device, self._recorder, rate_hz=rate_hz, rle=rle, chunk_s=chunk_s,
                                       lock=self._lock, on_end=self._la_record_ended, events_from=events_from,
                                       carry=carry, obs=self._collector.obs if self._collector is not None else None)
            try:
                stream.start()
            except Exception:
                if self._collector is not None:
                    self._collector.take_back(events_from, carry)
                raise
            self._la_record = stream
            return stream.status()

    def _la_record_ended(self, stream: LaRecordingStream) -> None:
        collector = self._collector
        if collector is None:
            return
        if stream.carries_events:                                  # 流れが取り込んだ続きから再開する
            collector.take_back(stream.events_next, stream.carry)
        else:
            collector.pause_events(False)

    def recording_la_stream_stop(self) -> dict:
        """流し続けを止める（残りを出し切って記録へ入れる）。要約（サンプル数・溢れ・時刻の基準）を返す。"""
        stream = self._la_record
        if stream is None:
            raise RuntimeError("流していない（recording_la_stream_start を先に）")
        status = stream.stop()                                     # ロックの外で（受け取りの側がロックを取るため）
        self._la_record = None
        return status

    def _stop_la_record(self) -> None:
        if self._la_record is not None:
            self.recording_la_stream_stop()

    # --- 読む -----------------------------------------------------------------------------------

    def recording_segments(self) -> dict:
        """長期の記録の分割の一覧（state open / sealed / deleted・範囲・件数・大きさ・間引いた stream）と方針。"""
        view = self._reading()
        if not isinstance(view, SegmentedRecording):
            return {"segmented": False, "recording": view.directory.name}
        segments = view.segments()
        return {"segmented": True, "recording": view.directory.name, "policy": view.meta.get("policy"),
                "segments": segments, "bytes": sum(s["bytes"] for s in segments if s["state"] == "sealed")}

    def recording_overview(self, width: int = 1000, streams: list[str] | None = None) -> dict:
        """記録の全体の縮図（ミニマップ）: 画素ごと・stream ごとの件数、波形、分割の状態。長期の記録は事前計算した
        索引から（30 日でも速い。消した分割も粗い件数は残る）。画素の境は edges（タイムベース）。"""
        view = self._reading()
        out = view.overview(width, streams) if isinstance(view, SegmentedRecording) else _plain_overview(view, width, streams)
        return {"recording": view.directory.name, **out, "unchanged_channels": unchanged_channels(out.get("waveform", []))}


def unchanged_channels(waveform: list[str]) -> list[int]:
    """縮図の波形（ch ごとに画素の '0' / '1' / 'x' = 画素の中で変化 / '?' = 間引いた所でかたまりの中で変化 /
    '-' = 波形が無い）で、波形のある画素が全部同じ値の
    ch（記録の中で変化が無い）。波形の無い ch は入れない。"""
    out = []
    for ch, w in enumerate(waveform):
        levels = set(w.replace("-", ""))
        if len(levels) == 1 and not levels & {"x", "?"}:
            out.append(ch)
    return out


def _plain_overview(view: Recording, width: int, streams: list[str] | None) -> dict:
    span = view.span()
    if span is None:
        return {"span": None, "edges": [], "total": [], "streams": {}, "waveform": [], "segments": []}
    lo, hi = span[0], span[1] + 1
    edges = [lo + i * (hi - lo) // width for i in range(width + 1)]
    names = streams if streams is not None else view.distinct("stream")
    table = {s: view.density(lo, hi, width, [s]) for s in names}
    table = {s: c for s, c in table.items() if any(c)}
    total = [sum(col) for col in zip(*table.values())] if table else [0] * width
    return {"span": list(span), "edges": edges, "pixel_ticks": (hi - lo) / width, "total": total, "streams": table,
            "waveform": view.waveform_view(lo, hi, width), "segments": []}
