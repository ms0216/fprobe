"""コアを触る口。Linux の知識の層（mmu・kernel・bp・engine）はこの口だけを見る。

実装は口の外に置く（OpenOCD 経由、ArmATarget 経由、テスト用の模擬）。同時停止の方式と時間差の上限は、実装が毎回の停止で
返す事実で、上の層は推測しない。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

# DBGDSCR.MOE（デバッグに入った理由）。ARMv7 のデバッグアーキテクチャの値。
MOE_HALT, MOE_BKPT_EVT, MOE_WATCH_ASYNC, MOE_BKPT_INSN, MOE_EXT = 0, 1, 2, 3, 4
MOE_WATCH_SYNC = 10                 # ARMv7.1: 当たった命令が完了する前に止まる
WATCH_MOES = (MOE_WATCH_ASYNC, MOE_WATCH_SYNC)


class OutsideLinearMap(ValueError):
    """物理アドレスが、カーネルの線形マップの外（highmem など）にある。オフセットだけでは仮想アドレスに直せない。"""


@dataclass(frozen=True)
class CoreState:
    """止まったコアの事実（`snapshot` が返す）。tid は CONTEXTIDR>>8（エンジンが sp_svc で置き換える。-1 は対象のスレッドではない）。"""
    halted: bool
    pc: int = 0
    cpsr: int = 0
    ctx: int = 0
    moe: int = -1                   # 停止の理由（止まっているときだけ有効）
    sp_svc: int = 0                 # バンクされた SVC の SP: そのコアが走らせているスレッドのカーネルスタックの中にある
    tid: int = 0

    @property
    def user(self) -> bool:
        return self.cpsr & 0x1F == 0x10

    @property
    def thumb(self) -> bool:
        return bool(self.cpsr & 0x20)


@dataclass(frozen=True)
class StopReport:
    """1 回の `halt_all` の事実。

    method: "sequential"（コアを順に止めた）か "simultaneous"（ハードウェアが同時に止めた）。
    cores_halted: 実際に止まったコアの番号。要求したコア全部とは限らない。
    skew_upper_bound_ns: コア間の停止の時間差の上限（ns）。None は「分からない」で、0 ではない。
    basis: 上限の根拠（何をどう測った値か）。
    assumptions: skew_upper_bound_ns がよりどころにした仮定（測っていない事柄）。空 = 仮定なし。使う側が判断する。
    bound_without_assumptions_ns: 仮定を使わない上限（assumptions が空でないときだけ。None = 仮定なしでは分からない）。
    """
    method: str
    cores_halted: tuple[int, ...]
    skew_upper_bound_ns: int | None
    basis: str
    assumptions: tuple[str, ...] = ()
    bound_without_assumptions_ns: int | None = None


class CoreAccess(Protocol):
    n_cores: int

    def halt_all(self) -> StopReport: ...
    def resume_all(self) -> None: ...
    def resume(self, cores: Iterable[int]) -> None:
        """指定したコアだけを再開する。途中で別のコアが止まったら、残りは再開せず `parked` に残してよい（任意の属性）。"""
    def snapshot(self, cores: Iterable[int] | None = None) -> dict[int, "CoreState"]: ...
    def regs(self, core: int) -> list[int]:
        """r0-r15, cpsr（17 個）"""
    def set_regs(self, core: int, vals: list[int]) -> None: ...
    def fetch(self, core: int, va: int, width: int) -> int:
        """そのコアの MMU の文脈で va の命令語を読む。"""
    def halted(self) -> tuple[bool, ...]: ...
    def wait_event(self, timeout_s: float) -> tuple[int, ...]:
        """自分で止まったコア（ブレークポイント等）の番号。無ければ空。"""
    def step(self, core: int) -> None: ...
    def read_reg(self, core: int, name: str) -> int: ...
    def write_reg(self, core: int, name: str, value: int) -> None: ...
    def read_cp15(self, core: int, name: str) -> int:
        """name: ttbr0 / ttbr1 / ttbcr / contextidr / sctlr"""
    def read_kernel(self, va: int, length: int) -> bytes:
        """カーネルの仮想アドレスを、コア 0 の MMU を通して（特権モードで）読む。vmalloc（VMAP_STACK のスタック）も読める。"""
    def write_kernel(self, va: int, data: bytes) -> None:
        """同じく書く。D キャッシュを PoU までクリーンする（命令側は sync_caches）。"""
    def read_phys(self, addr: int, length: int) -> bytes:
        """物理アドレス（線形マップの中だけ。外は OutsideLinearMap）。"""
    def write_phys(self, addr: int, data: bytes) -> None: ...
    def set_hw_bp(self, core: int, slot: int, addr: int, width: int) -> None: ...
    def clear_hw_bp(self, core: int, slot: int) -> None: ...
    def set_hw_watch(self, core: int, slot: int, addr: int, length: int, kind: str) -> None:
        """kind: "w" / "r" / "rw" """
    def clear_hw_watch(self, core: int, slot: int) -> None: ...
    def sync_caches(self, cores: Iterable[int] | None = None) -> None:
        """書いたコードが命令フェッチから見えるようにする（D キャッシュを PoU までクリーン、指定コアの I キャッシュと分岐予測を無効化）。"""
