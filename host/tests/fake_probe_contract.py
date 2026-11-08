"""偽のデーモンの Probe（FakeProbe / DemoProbe）を本物の Probe に照らす（caps_fakes.FakeDeviceBase と同じ考え方）。

- クラスを作るとき（FakeProbe.__init_subclass__）に、本物の Probe と同じ名前のメソッドは、本物の引数をすべて受けること
  （本物に無い引数は既定値付き）を確かめる。ずれていれば TypeError。MCP・画面は本物の引数で呼ぶので、受けない偽物は
  「道具の失敗」に見えてしまう。
- 偽の機器が実装しない API のメソッドは NOT_IN_DEMO に名前で書く。API に足したメソッドは、実装するか、ここに書くかを
  試験が迫る。呼ばれたら AttributeError ではなく NotInDemo（「偽の機器は X に対応しない」）を返す。
"""

import inspect

from fprobe.mcp_server import Probe

__all__ = ["NOT_IN_DEMO", "NotInDemo", "signature_problems", "not_in_demo", "unsupported_arg"]


class NotInDemo(Exception):
    """偽の機器（--demo）が対応しない API のメソッド。実機・本物のデーモンでは動く（偽物の不足であって道具の失敗ではない）。"""


NOT_IN_DEMO = frozenset({
    "ap_read", "ap_write", "call_function", "csr_read", "csr_write", "cycles_clock", "cycles_measure", "cycles_read",
    "debug_mode_get", "debug_mode_set", "dp_read", "dp_write", "elf_size", "elf_watch", "etm_decode", "etm_history",
    "etm_instructions", "etm_packets", "etm_start", "etm_stop", "events_read", "events_start", "events_stop",
    "flash_algorithm_guard_get", "flash_algorithm_guard_set", "flash_breakpoints_restore", "fpga_obs_close",
    "fpga_obs_count", "fpga_obs_open", "fpga_obs_read", "fpga_obs_register_read", "fpga_obs_register_write",
    "fpga_obs_select", "fpga_obs_signals_get", "fpga_obs_signals_set", "fpga_obs_start", "fpga_obs_status",
    "fpga_obs_stop", "gpio_read", "gpio_release", "gpio_sequence", "gpio_set", "halt_risk_get", "heap_scan",
    "heap_trace_events", "heap_trace_report", "heap_trace_start", "heap_trace_stop", "jtag_clock", "jtag_reset",
    "jtag_sample_delay_scan", "jtag_sample_delay_set", "jtag_scan", "jtag_shift", "la_arm", "la_collect",
    "la_decoder_load", "la_deep_arm", "la_deep_capture", "la_deep_collect", "la_export", "la_load", "la_raw_to_vcd",
    "la_stream",
    "linux_agent_deploy", "linux_agent_start", "linux_agent_status", "linux_agent_stop", "linux_capabilities",
    "linux_connect", "linux_core_fetch", "linux_core_limit_set", "linux_core_pattern_set", "linux_core_status",
    "linux_disconnect", "linux_events_read", "linux_ftrace_disable", "linux_ftrace_enable", "linux_gdb_batch",
    "linux_gdb_serve", "linux_gdb_status", "linux_gdb_stop", "linux_halt_close", "linux_halt_debug_attach",
    "linux_halt_debug_break_insert", "linux_halt_debug_break_remove", "linux_halt_debug_close", "linux_halt_debug_halt",
    "linux_halt_debug_memory", "linux_halt_debug_read", "linux_halt_debug_run", "linux_halt_debug_status",
    "linux_halt_debug_select", "linux_halt_debug_step", "linux_halt_debug_threads", "linux_halt_gdb_serve",
    "linux_halt_gdb_status", "linux_halt_gdb_stop", "linux_halt_halt_all", "linux_halt_open", "linux_halt_processes",
    "linux_halt_profile_load", "linux_halt_profile_save", "linux_halt_recover", "linux_halt_rescue", "linux_halt_resume_all", "linux_halt_status", "linux_halt_threads",
    "linux_log_read", "linux_memap_connect", "linux_memap_connect_jtag", "linux_memap_disconnect",
    "linux_perf_record", "linux_perf_stat", "linux_probe_add", "linux_probe_list", "linux_probe_remove",
    "linux_ring_open", "linux_ring_read", "linux_ring_unwatch", "linux_ring_watch", "linux_sync", "linux_targets",
    "linux_trace_start",
    "linux_trace_stop", "log_annotate", "log_decode", "log_read", "log_start", "log_status", "log_stop",
    "option_bytes_read", "option_bytes_write", "pins_write", "probe_design_get", "probe_design_switch",
    "probe_name_set", "profile_export", "profile_timeline", "recording_correlate", "recording_cpu_load",
    "recording_density", "recording_durations", "recording_interrupts",
    "recording_overview", "recording_transactions", "recording_trigger_clear",
    "recording_trigger_set", "recording_trigger_status", "recording_waveform", "rtos_read", "rtos_timeline_blocking",
    "rtos_timeline_events", "rtos_timeline_flow", "rtos_timeline_interval", "rtos_timeline_jobs",
    "rtos_timeline_memory", "rtos_timeline_overhead", "rtos_timeline_profile", "rtos_timeline_queues", "rtt_measure",
    "rtt_stop", "script_check", "script_list", "scvd_load", "scvd_view", "semihosting_configure", "snapshot_close",
    "snapshot_export_core", "snapshot_info", "snapshot_list", "snapshot_open", "snapshot_restore", "snapshot_save",
    "spi_transfer", "stack_paint", "stack_usage", "stream_close", "stream_list", "stream_log_start",
    "stream_log_stop", "stream_serve", "swo_decode", "swo_read", "swo_text", "target_core_select", "target_diagnose",
    "target_speed_test", "target_unlock", "testpoint_aggregate", "testpoint_calls", "testpoint_coverage",
    "testpoint_export", "testpoint_instrument", "testpoint_report", "testpoint_start", "testpoint_status",
    "testpoint_stop", "trace_events", "trace_import", "trace_snapshot", "trace_start", "trace_status", "trace_stop",
    "uart_flow", "uart_lines", "user_trace_get", "user_trace_start", "user_trace_stats", "user_trace_stop",
    "watch_buffer_read", "watch_buffer_start", "watch_buffer_stop",
})


def unsupported_arg(method: str, arg: str, value) -> NotInDemo:
    """本物の引数は受けるが、偽の機器がその値を扱えないとき: 黙って無視せず（結果が嘘になる）、偽物の不足と分かる誤りにする。"""
    return NotInDemo(f"偽の機器（デモ）の {method} は {arg}={value!r} に対応しない（本物の機器では使える。偽物の不足）")


def not_in_demo(name: str):
    def call(*args, **kwargs):
        raise NotInDemo(f"偽の機器（デモ）は {name} に対応しない（本物の機器では使える。偽物の不足）")
    call.__name__ = name
    call.__not_in_demo__ = True
    return call


_PROBE_BASES = set(inspect.getmro(Probe))


def _compatible(name: str, fake) -> str | None:
    want = [p for p in inspect.signature(getattr(Probe, name)).parameters.values()
            if p.name not in ("self", "caller") and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    params = [p for p in inspect.signature(fake).parameters.values() if p.name != "self"]
    if any(p.kind is p.VAR_KEYWORD for p in params):
        return None
    got = {p.name: p for p in params}
    missing = [p.name for p in want if p.name not in got]
    extra = [p.name for p in params if p.name not in {w.name for w in want} and p.default is p.empty
             and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    if missing or extra:
        return (f"{name}{tuple(got)} が本物の Probe.{name}{tuple(w.name for w in want)} と合わない"
                + (f"（足りない引数: {', '.join(missing)}）" if missing else "")
                + (f"（本物に無い引数に既定値が無い: extra {', '.join(extra)}）" if extra else ""))
    return None


def signature_problems(cls) -> list[str]:
    """cls（MRO 全体）のメソッドのうち、本物の Probe と同じ名前で引数が合わないもの。"""
    problems = []
    for name in dir(cls):
        if name.startswith("_") or not callable(getattr(Probe, name, None)):
            continue
        fake = getattr(cls, name, None)
        owner = next((c for c in inspect.getmro(cls) if name in vars(c)), None)
        if owner is None or owner in _PROBE_BASES or not inspect.isfunction(fake) or getattr(fake, "__not_in_demo__", False):
            continue
        problem = _compatible(name, fake)
        if problem:
            problems.append(problem)
    return problems
