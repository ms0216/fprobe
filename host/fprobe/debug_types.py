"""デバッガ（fprobe.debug）の値の型と定数: 止まった様子 Stop・ウォッチ Watch・フレーム Frame・pyOCD 越しのメモリ/
レジスタの口 PyocdAccess、停止理由・ベクタキャッチ・リセットの種類の対応表。ターゲットの操作の手続きは debug.py。"""

from dataclasses import dataclass

from pyocd.core import exceptions as pyocd_errors
from pyocd.core.target import Target

from .dwarf.access import AccessError
from .symbols import Location

_WATCH_KINDS = {"read": Target.WatchpointType.READ, "write": Target.WatchpointType.WRITE,
                "access": Target.WatchpointType.READ_WRITE}
_REASONS = {Target.HaltReason.BREAKPOINT: "breakpoint", Target.HaltReason.WATCHPOINT: "watchpoint",
            Target.HaltReason.DEBUG: "step", Target.HaltReason.USER: "user",
            Target.HaltReason.VECTOR_CATCH: "vector_catch", Target.HaltReason.EXTERNAL: "external"}
# ベクタキャッチの名前 → pyOCD の VectorCatch（DEMCR の VC_CORERESET・VC_MMERR・VC_NOCPERR・VC_CHKERR・VC_STATERR・
# VC_BUSERR・VC_INTERR・VC_HARDERR・VC_SFERR）
VECTOR_CATCH = {"core_reset": Target.VectorCatch.CORE_RESET, "mem_manage": Target.VectorCatch.MEM_FAULT,
                "no_coprocessor": Target.VectorCatch.COPROCESSOR_ERR, "check_error": Target.VectorCatch.CHECK_ERR,
                "state_error": Target.VectorCatch.STATE_ERR, "bus_fault": Target.VectorCatch.BUS_FAULT,
                "interrupt_error": Target.VectorCatch.INTERRUPT_ERR, "hard_fault": Target.VectorCatch.HARD_FAULT,
                "secure_fault": Target.VectorCatch.SECURE_FAULT}
# リセットの種類 → pyOCD の ResetType（default は pyOCD の既定 = セッションの reset_type かコアの既定）
RESET_KINDS = {"default": None, "system": Target.ResetType.SYSRESETREQ, "core": Target.ResetType.VECTRESET,
               "hardware": Target.ResetType.HARDWARE, "emulated": Target.ResetType.EMULATED}
VTOR = 0xE000ED08
XPSR_THUMB = 1 << 24


@dataclass(frozen=True)
class Stop:
    """実行が止まったときの様子。reason: breakpoint / watchpoint / step / user / timeout / vector_catch / external。"""
    reason: str
    pc: int
    where: Location
    elapsed_s: float
    value: dict | None = None     # step_line の out: 出た関数の戻り値（{"type", "value"}）
    halt_bits: list[str] | None = None   # 最後の停止の DFSR の立っていたビット全部（読んだときだけ。reason は 1 語）
    steps: int | None = None      # step_line: ホストが発行した 1 命令ステップの数（測った事実。行が変わらなくても進んだと分かる）
    runs: int | None = None       # step_line: 踏み台・ブレークポイントまで走らせた（resume した）回数


@dataclass(frozen=True)
class Watch:
    address: int
    size: int
    kind: str                     # read / write / access


@dataclass(frozen=True)
class Frame:
    pc: int                       # 呼び出し元のフレームでは戻り先、例外で中断された処理では正確な番地
    where: Location               # 呼び出し元では呼び出し命令の行（戻り先 − 1 で引く）
    note: str = ""                # "exception" = 例外の入口で積まれたフレームから復元した位置、"lr_without_cfi" = CFI の
                                  # 無い最内の関数を葉と仮定して LR から出した（assume_leaf のときだけ。確かめていない）
    cfa: int | None = None        # CFI で巻き戻したときの CFA（呼び出し元の sp）
    stop: dict | None = None      # CFI で巻き戻したときの最後の段: その先へ巻き戻さなかった理由（dwarf.unwind）


class PyocdAccess:
    """pyOCD のターゲット（CortexM）を fprobe.dwarf の読み手（TargetAccess / WritableAccess）に包む。"""

    def __init__(self, target):
        self.target = target

    def read_memory(self, address: int, length: int) -> bytes:
        try:
            return bytes(self.target.read_memory_block8(address, length))
        except pyocd_errors.Error as e:
            raise AccessError(f"0x{address:08X} の {length} バイト: {e}") from None

    def write_memory(self, address: int, data: bytes) -> None:
        try:
            self.target.write_memory_block8(address, list(data))
            self.target.flush()
        except pyocd_errors.Error as e:
            raise AccessError(f"0x{address:08X} への書き込み: {e}") from None

    def read_register(self, name: str) -> int:
        if name.startswith("d") and name[1:].isdigit():          # d*n* = s*2n*:s*2n+1*（単精度の FPU にも d の名前で）
            n = int(name[1:])
            return self.read_register(f"s{2 * n}") | self.read_register(f"s{2 * n + 1}") << 32
        try:
            return self.target.read_core_register_raw(name)
        except (pyocd_errors.Error, KeyError, ValueError) as e:
            raise AccessError(f"レジスタ {name}: {e}") from None


