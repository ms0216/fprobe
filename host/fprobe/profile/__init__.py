"""性能解析（F5）: PC サンプリングのプロファイル・割り込みの時間・2 点間の時間・カバレッジ（近似）。

仕様: docs/superpowers/specs/2026-10-17-f5-profile-design.md
入出力を持たない計算だけ（実機の設定は書く番地と値の一覧を返し、本流が書く）。

    plan = plan_itm_sampling(16e6, 10_000, 2_000_000, exception_trace=True)   # plan.writes を本流が書く
    samples = PcSamples.from_itm(Timestamper().feed(ItmDecoder().feed(swo_bytes)))
    prof = Profiler(Symbols(elf), Disassembler.from_elf(elf)).profile(samples)
    prof.top(10)                                   # 関数ごとのサンプル数と割合
    coverage(prof, Disassembler.from_elf(elf))     # 近似のカバレッジ
    measure(recording, start, end)                 # 2 点間の時間
"""

from .coverage import Coverage, FunctionCoverage, coverage, elf_functions
from .dwt import (RegisterWrite, SamplingPlan, SwoPlan, itm_port_writes, plan_itm_sampling, plan_pcsr_sampling,
                  plan_swo)
from .exceptions import Activation, ExceptionProfile, ExceptionState, exception_name, exception_profile
from .pcprofile import Profile, Profiler
from .samples import PcSample, PcSamples
from .stats import Distribution
from .timing import IntervalReport, interval_report, limit_intervals, measure, pair_times

__all__ = ["Activation", "Coverage", "Distribution", "ExceptionProfile", "ExceptionState", "FunctionCoverage",
           "IntervalReport", "PcSample", "PcSamples", "Profile", "Profiler", "RegisterWrite", "SamplingPlan", "SwoPlan",
           "coverage", "elf_functions", "exception_name", "exception_profile", "interval_report", "itm_port_writes",
           "limit_intervals", "measure", "pair_times", "plan_itm_sampling", "plan_pcsr_sampling", "plan_swo"]
