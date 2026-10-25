"""Probe の道具: 共通タイムラインのタイル（G01。画面が毎フレームの間引きに使う材料をバイナリで返す）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。Probe の _capture_wave（最後の捕捉）・_reading（読む記録）を使う。
機器には触らない（読みだけ）。

仕様: docs/superpowers/specs/2026-10-18-timeline-tiles-design.md
"""

from .api import NoData
from .api.binary import BinaryResult
from .timeline import DEFAULT_MAX_EDGES, DEFAULT_MAX_EVENTS, capture_tile, name_rows, recording_tile

__all__ = ["TimelineTools"]


class TimelineTools:
    def timeline_tiles(self, source: str = "capture", t0: int = 0, t1: int | None = None,
                       channels: list[int] | None = None, streams: list[str] | None = None, bin: int | None = None,
                       max_edges: int = DEFAULT_MAX_EDGES, max_events: int = DEFAULT_MAX_EVENTS) -> BinaryResult:
        """範囲 [t0, t1) のタイル（source = capture: 最後の捕捉、時刻はサンプル番号 / recording: 読む記録、時刻は
        タイムベース）。生データが max_edges・max_events を超える部分は幅 bin 程度の区間の要約。"""
        if source == "capture":
            if streams:
                raise ValueError("捕捉には出来事の段が無い（streams は recording だけ）")
            wave, info = self._capture_wave()
            return capture_tile(wave, info, t0, t1, channels, bin, max_edges)
        rec = self._reading()
        if t1 is None:
            span = rec.span()
            if span is None:
                raise NoData("記録が空")
            t1 = span[1] + 1
        tile = recording_tile(rec, t0, t1, channels, streams, bin, max_edges, max_events)
        name_rows(tile.result, "sample", self._sample_name)            # サンプラの語の段は式・変数の名前で
        return tile
