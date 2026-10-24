"""テストポイント方式（DT+ 方式、F7）: ソースに短い出力のコードを自動で入れ、ロジアナ（GPIO）・SWO（ITM）・RTT で
時刻付きで集め、関数の遷移・関数ごとの時間・変数の値・カバレッジ・長時間の要約を解析する。

仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md

    from fprobe.testpoint import instrument_tree, Options, WatchVar, ProbeTable, SerialDecoder, Analyzer
    result = instrument_tree(Path("src"), Path("build/tp"), options=Options(variables=(WatchVar("state"),)))
    dec = SerialDecoder(clock_channel=4, data_channel=5, guard=1)
    an = Analyzer(result.table, hz=100e6)
    an.feed(dec.feed(transitions) + dec.flush())
    report = an.finish()
"""

from .analysis import (Analyzer, Anomaly, Call, CallTreeNode, Coverage, FunctionStats, IntervalStats, Report, Stats,
                       analyze, intervals, render_calls)
from .decode import (ItmDecoder, ParallelDecoder, RttDecoder, SerialDecoder, decode_gpio_parallel,
                     decode_gpio_serial, decode_itm, decode_rtt)
from .instrument import (FileResult, InstrumentResult, Options, WatchVar, header_path, instrument_source,
                         instrument_tree)
from .live import LiveAnalysis, TimedSwo, TimeMap
from .probes import FunctionInfo, Probe, ProbeTable
from .record import STREAM, RecorderSink, record_testpoints
from .trace import Decoded, Hit, Item, Loss

__all__ = ["Analyzer", "Anomaly", "Call", "CallTreeNode", "Coverage", "Decoded", "FileResult", "FunctionInfo",
           "FunctionStats", "Hit", "InstrumentResult", "IntervalStats", "Item", "ItmDecoder", "LiveAnalysis", "Loss", "Options",
           "ParallelDecoder", "Probe", "ProbeTable", "RecorderSink", "Report", "RttDecoder", "STREAM", "SerialDecoder",
           "Stats", "TimeMap", "TimedSwo", "WatchVar", "analyze", "decode_gpio_parallel", "decode_gpio_serial", "decode_itm", "decode_rtt",
           "header_path", "instrument_source", "instrument_tree", "intervals", "record_testpoints", "render_calls"]
