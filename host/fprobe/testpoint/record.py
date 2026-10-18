"""テストポイントの解析を共通の時間軸の記録（F3 の Recorder）に stream "testpoint" として書く。

    sink = RecorderSink(recorder, hz=100e6, offset_ticks=start)
    an = Analyzer(table, hz=100e6, keep_calls=False, **sink.callbacks())   # 長時間でもメモリが一定
    an.feed(items); recorder.flush()

出来事（仕様の 5 節）:
- call:    t = 入口, t_end = 出口, a = 入口の ID, b = 深さ, body = {function, self_s, complete}
- hit:     分岐・変数の印。a = ID, b = 値（無ければ 0）, body = {kind, function, label, line[, value]}
- anomaly: a = ID（無ければ 0）, body = {type, function, detail}
"""

from collections.abc import Iterable

from ..recording import Recorder
from .analysis import Analyzer, Anomaly, Call, Report
from .probes import Probe, ProbeTable
from .trace import Hit, Item

__all__ = ["STREAM", "RecorderSink", "record_testpoints"]

STREAM = "testpoint"


class RecorderSink:
    """Analyzer のコールバックを受けて Recorder に書く。時刻は offset_ticks + round(t × 記録の Hz / hz)。
    uncertainty: 時刻の写しの不確かさ（タイムベースの単位。ターゲットのクロックの時刻をホストの時計で写したとき）。"""

    def __init__(self, recorder: Recorder, hz: float, offset_ticks: int = 0, uncertainty: float | None = None):
        self.rec = recorder
        self.hz = hz
        self.offset = offset_ticks
        self.scale = recorder.timebase_hz / hz
        self.uncertainty = uncertainty

    def ticks(self, t: int) -> int:
        return self.offset + round(t * self.scale)

    def callbacks(self) -> dict:
        return {"on_call": self.call, "on_hit": self.hit, "on_anomaly": self.anomaly}

    def call(self, c: Call) -> None:
        self.rec.add(STREAM, "call", self.ticks(c.t0), a=c.entry_id, b=c.depth, t_end=self.ticks(c.t1),
                     body={"function": c.function, "self_s": c.self_ticks / self.hz, "complete": c.complete},
                     uncertainty=self.uncertainty)

    def hit(self, h: Hit, probe: Probe, function: str) -> None:
        body = {"kind": probe.kind, "function": function, "label": probe.label, "line": probe.line}
        if h.value is not None:
            body["value"] = h.value
        self.rec.add(STREAM, "hit", self.ticks(h.t), a=h.id, b=h.value or 0, body=body, uncertainty=self.uncertainty)

    def anomaly(self, a: Anomaly) -> None:
        self.rec.add(STREAM, "anomaly", self.ticks(a.t), a=a.probe_id or 0,
                     body={"type": a.type, "function": a.function, "detail": a.detail}, uncertainty=self.uncertainty)


def record_testpoints(recorder: Recorder, items: Iterable[Item], table: ProbeTable, hz: float, *,
                      offset_ticks: int = 0, **options) -> Report:
    """解析しながら記録に書き、最後に flush して要約を返す。options は Analyzer へ。"""
    sink = RecorderSink(recorder, hz, offset_ticks)
    an = Analyzer(table, hz, **sink.callbacks(), **options)
    an.feed(items)
    report = an.finish()
    recorder.update_meta(testpoint={"probes": len(table), "hz": hz,
                                    "coverage": {"c0": report.coverage.c0, "c1": report.coverage.c1}})
    recorder.flush()
    return report
