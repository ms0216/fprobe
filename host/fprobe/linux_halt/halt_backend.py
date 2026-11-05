"""バックエンドを開く（`HaltOptions` から fprobe の `ArmACores` か OpenOCD の `OpenOcdCores` を作る）と、コアの口の錠。

`open_backend` は開いた事実（info）と閉じ方・バックエンドの誤りの型を返す。`_Locked` はコアの口の各メソッドを 1 回ずつ
錠を取って呼ぶ（gdb のスレッドとデーモンの他の操作が、同じ USB の口を同時に使わない）。
"""
from __future__ import annotations

from typing import Any, Callable

from .halt_options import UNMEASURED, HaltOptions, _ocd_boards


def _ns(bound):
    """"unmeasured" → None（バックエンドの「分からない」）。"""
    return None if bound == UNMEASURED else bound

class _Locked:
    """コアの口の各メソッドを、1 回ずつ lock を取って呼ぶ（属性はそのまま）。gdb のスレッドと他の操作を同じ口で混ぜない。"""

    def __init__(self, cores, lock):
        object.__setattr__(self, "_cores", cores)
        object.__setattr__(self, "_lock", lock)

    def __getattr__(self, name):
        value = getattr(self._cores, name)
        if not callable(value):
            return value
        lock = self._lock

        def locked(*args, **kwargs):
            with lock:
                return value(*args, **kwargs)
        return locked

    def __setattr__(self, name, value):
        setattr(self._cores, name, value)


def open_backend(o: HaltOptions, layout, *, dap_link: Callable[[], Any] | None, device) -> tuple:
    """o のバックエンドを開く。dap_link: fprobe の DAP の接続口を開く関数（Session.dap_access）、device: fprobe の機器
    （FPGA の停止の見張り・イベント記録）。-> (cores, close, backend_errors, info)。info は開いた事実。"""
    off, phys, end = layout.o["page_offset"], layout.o["phys_offset"], layout.o["linear_end"]
    if o.backend == "openocd":
        from .backend_openocd import OcdError, OpenOcdCores
        cores = OpenOcdCores.open("127.0.0.1", o.ocd_port, board=_ocd_boards()[o.ocd_board], jtag_khz=o.jtag_khz,
                                  page_offset=off, phys_offset=phys, linear_end=end, hw_slots=o.hw_slots,
                                  watch_slots=o.watch_slots, timeout=o.ocd_timeout_s)
        try:
            cores.setup()
        except BaseException:
            cores.o.close()
            raise
        info = {"backend": "openocd", "n_cores": cores.n_cores, "hw_slots": cores.hw_slots,
                "watch_slots": cores.watch_slots, "actual_jtag_khz": cores.actual_jtag_khz}

        def close():
            try:
                cores.release()
            finally:
                cores.o.close()
        return cores, close, (OcdError,), info
    from fprobe.arm_a_target import arm_a_part
    from fprobe.arm_debug import ArmDebugError
    from .backend_arma import ArmACores
    cores = ArmACores.open(dap_link(), arm_a_part(o.part), frequency_hz=o.jtag_khz * 1000,
                           page_offset=off, phys_offset=phys, linear_end=end)
    info = {"backend": "fprobe", "part": o.part, "n_cores": cores.n_cores, "hw_slots": cores.hw_slots,
            "watch_slots": cores.watch_slots, "debug_ap": cores.debug_ap, "discovery": cores.discovery,
            "cti": None, "fpga_halt_watch": None, "fpga_request_times": None}
    try:
        if o.cti:
            from fprobe.plugins import zynq_cti
            from .cti import ApbIo
            board = zynq_cti.ZYNQ7000
            apb_offset = board.apb_offset_for(cores.discovery)
            cross = zynq_cti.cross_halt(board, ApbIo(cores._dap, cores.debug_ap), apb_offset=apb_offset,
                                        ack_timeout_polls=o.cti_ack_polls)
            cores.use_cross_halt(cross, request_skew_upper_bound_ns=o.cti_request_bound_ns,
                                 event_skew_upper_bound_ns=_ns(o.cti_event_bound_ns), event_trigin=board.dbgack_in,
                                 hardware_halt_wait_s=o.cti_hw_wait_s)
            info["cti"] = {"board": o.cti_board, "apb_offset": apb_offset, "event_trigin": board.dbgack_in}
        if o.fpga_halt_watch:
            from fprobe.halt_watch import HaltWatch
            cores.use_fpga_halt_watch(HaltWatch(device), event_skew_upper_bound_ns=_ns(o.fpga_halt_watch_bound_ns),
                                      poll_s=o.fpga_halt_watch_poll_s)
            info["fpga_halt_watch"] = {"bound_ns": o.fpga_halt_watch_bound_ns, "poll_s": o.fpga_halt_watch_poll_s}
        if o.fpga_request_times:
            from .request_times import EventRequestTimes
            times = EventRequestTimes.open(device)
            cores.use_fpga_request_times(times)
            # 記録の無い構成: 上限はどれも PC の時計で、停止の報告の根拠の文がそう言う
            info["fpga_request_times"] = {"event_log": times.log is not None}
    except BaseException:
        cores.close()
        raise
    return cores, cores.close, (ArmDebugError,), info
