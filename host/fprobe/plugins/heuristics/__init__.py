"""プラグイン: 本体から外した経験則（ヒューリスティック）の「見方」を、本体の API の結果（測った事実）の上に載せる。

本体（fprobe）は測定器で、測った事実だけを返す（CLAUDE.md「fprobe 本体にヒューリスティックを入れない」）。ここには
本体から外した経験則の閾値・仮定に頼る推定・判定・並べ替えを、取り外せる層として置く（利用者の承認 2026-10-22）。

使い方（仕組みは fprobe.plugins の説明を参照。本体からは import しない）:

    from fprobe.plugins import ProbeCaller
    from fprobe.plugins import heuristics as h
    ranked = h.rank_causes(client.call("recording.correlate", effect="errors changed", causes=None, window_s=0.01))
    h.decode_uart_with_estimated_baud(client, channel=5, data_bits=8, parity="none", stop_bits=1, invert=False,
                                      lsb_first=True)

約束:
- 結果には必ず "heuristic"（経験則の名前）と "assumption"（仮定の短い説明）を入れる。使う人と AI が、その判断の根拠が
  測った事実ではなく経験則だと分かるように。各関数の docstring に、どの閾値・仮定か、外れる場合を書く。
- 閾値は元の本体と同じ値を既定にし、引数で変えられる。
- API を呼ぶ補助は、公開 API（host/api/fprobe_api.json）の名前と引数だけを `call(method, **params)` で呼ぶ。
  本体のライブラリ（データ型・逆アセンブラ等）は import して使うが、本体の API は変えない。
"""

from .timing import (  # noqa: F401
    outliers, outliers_of_intervals, rank_causes,
)
from .profile import (  # noqa: F401
    SWO_HEADROOM, TYPICAL_CORE_CLOCKS, _ASSUME_BINOMIAL, _with_ci, _z, aliasing_suspects, aliasing_suspects_from_api, clock_stderr, counter_overflow_risk, load_outside_interval, sampler_falling_behind, sampling_intervals, typical_core_clock, wilson_interval,
)
from .uart_spi import (  # noqa: F401
    SPI_FLASH_CONVENTIONAL_COMMANDS, STANDARD_BAUDS, _flash_command, decode_uart_with_estimated_baud, estimate_baud, spiflash_conventional_commands,
)
from .rtos import (  # noqa: F401
    PAYLOAD_CYCLES, RECORD_CYCLES, WORD_CYCLES, _TRACEX_ISR, infer_jobs, infer_jobs_from_api, trace_cpu_share, tracex_context_switches, wait_failed_as_timeout,
)
from .heap import (  # noqa: F401
    ALLOCATOR_WRAPPERS, heap_growing, heap_growing_from_api, heap_overflow_suspects, heap_trace_start_skipping_wrappers,
)
from .halt import (  # noqa: F401
    FATAL_FUNCTIONS, FAULT_HANDLER_PATTERN, GUARD_BYTES, HALT_RISK_PATTERNS, RTOS_GLOBALS, _ASSUME_FATAL, _FAULT_TYPICAL, _HALT_ORDER, _IRQ, fatal_kind, fault_hints, halt_risk_conventions, halt_risk_kinds, liveness_conventions, stack_warning,
)
from .link import (  # noqa: F401
    _ACK_CAUSES, _SOFT_FAILS, _TDO_CAUSES, _diagnose_severity, connect_script_name_hints, diagnose_hints, diagnose_verdict, jtag_guess_ir, link_cause_hints,
)
from .disasm import (  # noqa: F401
    disasm_before_pc, literal_pool_ranges, tbb_table_sizes,
)

__all__ = [
    "ALLOCATOR_WRAPPERS", "GUARD_BYTES", "PAYLOAD_CYCLES", "RECORD_CYCLES", "STANDARD_BAUDS", "TYPICAL_CORE_CLOCKS", "WORD_CYCLES",
    "decode_uart_with_estimated_baud", "diagnose_hints", "diagnose_verdict", "disasm_before_pc", "estimate_baud", "fault_hints",
    "heap_growing", "heap_growing_from_api", "heap_trace_start_skipping_wrappers","infer_jobs", "infer_jobs_from_api", "outliers", "outliers_of_intervals",
    "rank_causes", "stack_warning", "trace_cpu_share", "typical_core_clock",
    "SWO_HEADROOM", "aliasing_suspects", "aliasing_suspects_from_api", "counter_overflow_risk", "heap_overflow_suspects", "jtag_guess_ir",
    "link_cause_hints", "sampler_falling_behind", "wait_failed_as_timeout", "connect_script_name_hints",
    "tbb_table_sizes", "literal_pool_ranges",
    "FATAL_FUNCTIONS", "FAULT_HANDLER_PATTERN", "HALT_RISK_PATTERNS", "RTOS_GLOBALS", "fatal_kind",
    "halt_risk_conventions", "halt_risk_kinds", "liveness_conventions",
    "tracex_context_switches",
    "load_outside_interval", "sampling_intervals", "wilson_interval",
    "SPI_FLASH_CONVENTIONAL_COMMANDS", "spiflash_conventional_commands", "clock_stderr",
]
