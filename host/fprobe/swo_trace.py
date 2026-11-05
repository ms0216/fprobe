"""SWO の DWT・ITM の設定の計算と、受けたパケットの組み立て（U11）の窓口。

実体は swo_plan（設定の計算: plan_trace・レジスタの定数）と swo_assemble（TimedItmStream・DataTraceAssembler・
CounterTotals）。呼ぶ側はこの名前（fprobe.swo_trace）から使う。
"""

from .swo_assemble import CounterTotals, DataAccess, DataTraceAssembler, TimedItmStream
from .swo_plan import (COUNTERS, CPUID, DWT_CYCCNT, LNK1ENA, NOPRFCNT, NOTRCPKT, ComparatorUse, CoreInfo, DataTrace,
                       DwtInfo, TracePlan, TraceRequest, ValueWatch, address_mask, core_info, dwt_comp, dwt_function,
                       dwt_mask, plan_trace)

__all__ = ["COUNTERS", "CPUID", "DWT_CYCCNT", "LNK1ENA", "NOPRFCNT", "NOTRCPKT", "ComparatorUse", "CoreInfo",
           "CounterTotals", "DataAccess", "DataTrace", "DataTraceAssembler", "DwtInfo", "TimedItmStream", "TracePlan",
           "TraceRequest", "ValueWatch", "address_mask", "core_info", "dwt_comp", "dwt_function", "dwt_mask",
           "plan_trace"]
