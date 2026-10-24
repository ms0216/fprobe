"""止まった理由の印が分からないとき、「外からの停止（user）」等の既定の名前を当てない（点検 2 回目 ?: 停止理由の既定値）。

レジスタの値が表に無い（DSCR.MOE の予約の値・dcsr.cause の新しい値・DFSR のビットが 1 つも無い等）なら、分からない
（None / "unknown"）と返す。"""

from types import SimpleNamespace

from fprobe.arm_a_target import ArmATarget
from fprobe.arm_debug import ArmDebugCore
from fprobe.riscv_target import RiscvTarget
from fprobe.stepping import dfsr_reason


def _arm_core(moe: int) -> ArmDebugCore:
    core = ArmDebugCore.__new__(ArmDebugCore)
    core._cache, core._stepped, core._moe = {"pc": 0, "cpsr": 0}, False, moe
    return core


def test_arm_debug_names_only_the_moe_values_of_the_architecture():
    assert _arm_core(0).halt_cause() == "halt_request"
    assert _arm_core(1).halt_cause() == "breakpoint"
    assert _arm_core(0xF).halt_cause() == "unknown"           # 予約の値: 停止の要求とみなさない


def test_arm_a_target_returns_none_for_an_unknown_cause():
    t = ArmATarget.__new__(ArmATarget)
    t.core = SimpleNamespace(halt_cause=lambda: "unknown")
    assert t.get_halt_reason() is None


def test_riscv_target_returns_none_for_an_unknown_dcsr_cause():
    t = RiscvTarget.__new__(RiscvTarget)
    t.dm = SimpleNamespace(halt_cause=lambda: None)            # dcsr.cause が表に無い（例 6 = group）
    t.breakpoints = {}
    assert t.get_halt_reason() is None


def test_snapshot_target_does_not_turn_unknown_into_user():
    from fprobe.probe_snapshot import SnapshotTarget
    t = SnapshotTarget.__new__(SnapshotTarget)
    t.snapshot = SimpleNamespace(info={"halt": {"reason": "unknown"}})
    assert t.get_halt_reason() is None
    t.snapshot = SimpleNamespace(info={"halt": {"reason": "user"}})
    from pyocd.core.target import Target
    assert t.get_halt_reason() == Target.HaltReason.USER


def test_dfsr_without_any_bit_after_running_is_unknown_not_user():
    assert dfsr_reason(0x01, stepping=False) == "user"          # HALTED: 停止の要求（ARMv7-M の DFSR.HALTED）
    assert dfsr_reason(0x00, stepping=False) == "unknown"       # 印が無い: 外からの停止とみなさない
    assert dfsr_reason(0x00, stepping=True) is None             # 1 命令の実行は進んだ（HALTED が消されていても）
