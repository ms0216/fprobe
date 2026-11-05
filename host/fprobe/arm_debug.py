"""Arm の A プロファイルのコア（ARMv7-A の Cortex-A7）と ARM11（ARMv6 の ARM1176JZF-S）を、停止デバッグ（halting
debug）で動かす共通の部分（U32）。pyOCD はこれらのコアのデバッグを持たない。

どちらのコアも外部デバッグの作りは同じ: DSCR（状態と設定）・ITR（命令を入れてコアに実行させる）・DTR（コアとの値の
受け渡し。DCC）・止める / 再開する要求・BVR / BCR（ブレークポイント）・WVR / WCR（ウォッチポイント）。違うのは
それらへの道だけなので、道（CoreDebugPort）を分けて、その上の手順をここに 1 つだけ書く。

- ARMv7-A（fprobe.armv7a_debug）: JTAG-DP → APB-AP の向こうの CoreSight のデバッグレジスタ（メモリの番地）。
- ARM11（fprobe.arm11_debug）: JTAG の TAP の命令とスキャンチェーン（1 = DSCR、4 = ITR、5 = DTR、7 = BVR 等）。

手順（OpenOCD の arm_dpm・cortex_a・arm11 と同じ考え方）:
- 止めたら ITR を有効にし、使い回すレジスタ（r0・r1）と PC・CPSR を読んで覚える（_enter）。PC は "MOV r0, pc" の
  値から ARM なら 8、Thumb なら 4 を引く。r2〜r14 はその都度 DCC で直接読み書きする。
- 再開の前に、書き替えた CPSR・PC と、借りた r0・r1 を書き戻し、ITR を無効にする（_leave）。
- メモリはコアに LDR / STR（後置きの番地の加算）を実行させて読み書きする。番地はコアから見た番地（MMU が有効なら
  仮想番地）、キャッシュもコアと同じに見える（止めたときの読みは一貫する）。書いたら、その範囲の D キャッシュを
  統一点（PoU）まで掃き出し、I キャッシュと分岐予測を無効にする（書いた命令が実行される）。アボートは DSCR の
  sticky ビットで見つけて消し、番地を添えて断る。
- ステップは、最後のブレークポイントの組（ステップ専用に取っておく）を「今の PC に一致しない」（mismatch）で置いて
  再開する。割り込みは ARMv7 なら DSCR.INTdis、ARM11 なら CPSR の I・F を 1 命令の間だけ立てて止める。
- ブレークポイント・ウォッチポイントはハードウェアの組だけ（ソフトの BKPT は使わない）。

呼び手（fprobe.arm_a_target）は、レジスタ・メモリ・ブレークポイントを触る前にコアを止めておく。

分け方: ここには道の型・状態と実行（止める・再開・ステップ・入る / 出る）・複数コアまとめの関数を置く。レジスタは arm_debug_regs.py、
メモリは arm_debug_memory.py、キャッシュは arm_debug_cache.py、ブレークポイントは arm_debug_points.py（いずれも ArmDebugCore に混ぜ込む部品）。
"""

import time
from typing import Protocol

from .arm_debug_cache import _CacheMixin
from .arm_debug_memory import _MemoryMixin
from .arm_debug_points import _PointsMixin, _byte_select
from .arm_debug_regs import _RegistersMixin

__all__ = ["ArmDebugCore", "ArmDebugError", "CoreDebugPort", "REGISTER_NAMES", "arm_mcr", "arm_mrc"]

from .arm_debug_defs import (  # noqa: F401  (再公開)
    DSCR_HALTED, DSCR_RESTARTED, DSCR_MOE_SHIFT, DSCR_SDABORT, DSCR_ADABORT, DSCR_UND,
    DSCR_STICKY, DSCR_INTDIS, DSCR_UDCCDIS, DSCR_ITREN, DSCR_HDBGEN, DSCR_MDBGEN,
    DSCR_EXTDCC, DSCR_EXTDCC_FAST, DSCR_INSTRCOMPL, DSCR_TXFULL, DSCR_RXFULL, _DSCR_KEEP,
    _MOE, CPSR_T, CPSR_F, CPSR_I, BP_ENABLE, BP_ANY_PRIVILEGE,
    BP_MISMATCH, WP_LOAD, WP_STORE, WP_KINDS, REGISTER_NAMES, arm_mcr,
    arm_mrc, _to_dtr, _from_dtr, MOV_R0_PC, MOV_PC_R0, MRS_R0_CPSR,
    MSR_CPSR_R0, LOAD_STORE, LDC_DTRTX, DSB, ISB, CP15,
    CP15_READABLE, MODE_MASK, WORD_MASK, MODE_USR, MODE_SYS, _FAST_READ_ROUND_TRIPS,
    ArmDebugError,
)


class CoreDebugPort(Protocol):
    """1 つのコアの外部デバッグへの道（ARMv7 の APB のレジスタ / ARM11 の JTAG のスキャンチェーン）。"""

    architecture: int                                    # 7 = ARMv7-A、6 = ARMv6（ARM11）

    def didr(self) -> int: ...                           # DBGDIDR（ブレークポイント・ウォッチポイントの数）

    def dscr(self) -> int: ...

    def write_dscr(self, value: int) -> None: ...

    def halt_request(self) -> None: ...

    def restart_request(self) -> None: ...               # sticky ビットも消してから再開を求める

    def clear_sticky(self) -> None: ...

    def execute(self, instruction: int) -> int | None: ...   # ITR で 1 命令を実行し、完了を待つ。読めれば DSCR

    def to_core(self, value: int) -> None: ...           # DBGDTRRX に置く（コアは MRC p14 で受け取る）

    def from_core(self) -> int: ...                      # DBGDTRTX を受け取る（コアが MCR p14 で置いた値）

    def write_breakpoint(self, index: int, value: int, control: int) -> None: ...

    def write_watchpoint(self, index: int, value: int, control: int) -> None: ...



class ArmDebugCore(_RegistersMixin, _MemoryMixin, _CacheMixin, _PointsMixin):
    """1 つのコアの停止デバッグ（CoreDebugPort の上）。"""

    def __init__(self, port: CoreDebugPort, timeout_s: float = 0.5, didr: int | None = None):
        """didr: 読んである DBGDIDR（まとめて読んだとき。無ければ読む）。"""
        self.port = port
        self.timeout_s = timeout_s
        didr = port.didr() if didr is None else didr
        self.breakpoint_count = ((didr >> 24) & 0xF) + 1      # 最後の 1 組はステップに使う
        self.watchpoint_count = ((didr >> 28) & 0xF) + 1
        self.debug_version = (didr >> 16) & 0xF
        self._cache: dict[str, int] | None = None             # 止まっている間の r0・r1・pc・cpsr
        self._dirty: set[str] = set()
        self._moe: int | None = None
        self._stepped = False
        self._line: int | None = None                         # D キャッシュの最小の行の長さ（バイト）
        self._mp: bool | None = None                          # MP 拡張（内側の共有域へのキャッシュ操作）
        self.thumb = False                                    # 最後に止まったときの CPSR.T（ブレークポイントの BAS）

    @property
    def v7(self) -> bool:
        return self.port.architecture == 7

    def info(self) -> dict:
        return {"architecture": "armv7-a" if self.v7 else "armv6", "debug_version": self.debug_version,
                "breakpoints": self.breakpoint_count - 1, "watchpoints": self.watchpoint_count}

    # --- 待つ ------------------------------------------------------------------------------------------

    def _wait(self, mask: int, what: str) -> int:
        deadline = time.monotonic() + self.timeout_s
        while True:
            dscr = self.port.dscr()
            if dscr & mask:
                return dscr
            if time.monotonic() >= deadline:
                raise ArmDebugError(f"{what}（DSCR 0x{dscr:08X}）")
            time.sleep(0.001)

    # --- 状態・実行 ------------------------------------------------------------------------------------

    def enable(self) -> None:
        """停止デバッグ（DSCR.HDBGen）を有効にする。止めはしない。"""
        enable_all([self])

    def disable(self) -> None:
        """停止デバッグ（DSCR.HDBGen）と命令の実行（ITRen）を切る。止まっているコアは止まったまま（ITRen が立ったままでは再開できない）。"""
        dscr = self.port.dscr()
        self.port.write_dscr(dscr & _DSCR_KEEP)

    def state(self) -> str:
        dscr = self.port.dscr()
        if dscr & DSCR_HALTED:
            self._ensure_entered(dscr)
            return "halted"
        self._cache = None
        return "running"

    def halt(self) -> None:
        dscr = self.port.dscr()
        if not dscr & DSCR_HALTED:
            self.port.halt_request()
            dscr = self._wait(DSCR_HALTED, "止まらない（DBGEN の線が無効・コアの電源やクロックが止まっている）")
            self._stepped = False
        self._ensure_entered(dscr)

    def resume(self) -> None:
        self.prepare_restart()
        self.port.restart_request()
        self._wait(DSCR_RESTARTED | DSCR_HALTED, "再開しない")

    def prepare_restart(self) -> None:
        """再開の要求の直前まで（止まっていることを確かめ、レジスタを戻して ITR を無効にする）。コアは止まったまま。要求は呼び手が
        書く（複数のコアの要求を 1 回の往復にまとめるため。fprobe.armv7a_debug.restart_request_all）。"""
        prepare_restart_all([self])

    @property
    def entered(self) -> bool:
        """止まった後に入って、レジスタ（r0・r1・PC・CPSR）を覚えている。"""
        return self._cache is not None

    def forget(self) -> None:
        """走っているのを読んだ: 覚えた値を捨てる（`state` が走っているのを読んだときと同じ）。"""
        self._cache = None

    def step(self, mask_interrupts: bool = True) -> None:
        """1 命令進める（mismatch のブレークポイント）。割り込みは mask_interrupts の間止める。"""
        cache = self._require_halted()
        pc, thumb = cache["pc"], bool(cache["cpsr"] & CPSR_T)
        slot = self.breakpoint_count - 1
        masked = None
        if mask_interrupts and not self.v7:                  # ARM11: CPSR の I・F を 1 命令の間だけ立てる
            masked = cache["cpsr"] & (CPSR_I | CPSR_F)
            self._set_cached("cpsr", cache["cpsr"] | CPSR_I | CPSR_F)
        self.port.write_breakpoint(slot, pc & ~3, BP_MISMATCH | _byte_select(pc, thumb) | BP_ANY_PRIVILEGE | BP_ENABLE)
        try:
            self._leave(mask_interrupts=mask_interrupts and self.v7)
            restart = getattr(self.port, "restart_and_read", None)   # 要求と、その後の DSCR を 1 回の往復で（ARMv7）
            if restart is not None:
                dscr = restart()
            else:
                self.port.restart_request()
                dscr = self.port.dscr()
            deadline = time.monotonic() + self.timeout_s
            requested = False
            while not dscr & DSCR_HALTED:
                if time.monotonic() >= deadline:              # WFI 等で次の命令に進まない: 止めて終える
                    self.port.halt_request()
                    dscr = self._wait(DSCR_HALTED, "ステップの後に止まらない")
                    requested = True
                    break
                time.sleep(0.001)
                dscr = self.port.dscr()
        finally:
            self.port.write_breakpoint(slot, 0, 0)
        self._ensure_entered(dscr)                            # 止まったのを読んだ DSCR（止まったコアは止まったまま）
        self._stepped = not requested                         # 要求で止めたら理由は DSCR.MOE のまま（step と言わない）
        if masked is not None:
            cpsr = self._cache["cpsr"]
            self._set_cached("cpsr", (cpsr & ~(CPSR_I | CPSR_F)) | masked)

    def halt_cause(self) -> str | None:
        if self._cache is None and self.state() != "halted":
            return None
        if self._stepped:
            return "step"
        return _MOE.get(self._moe, "unknown")

    def _require_halted(self) -> dict[str, int]:
        if self._cache is None:
            dscr = self.port.dscr()
            if not dscr & DSCR_HALTED:
                raise ArmDebugError("コアが走っている（レジスタ・メモリ・ステップは止めてから）")
            self._enter(dscr)
        return self._cache

    def _ensure_entered(self, dscr: int) -> None:
        if self._cache is None:
            self._enter(dscr)

    def _enter(self, dscr: int) -> None:
        """止まった直後: ITR を有効にし、借りるレジスタと PC・CPSR を覚え、書き込みを完了させる（DSB）。`enter_all` の 1 コア分。"""
        enter_all([self], [dscr])

    def _enter_saving(self, dscr: int) -> list[tuple]:
        """入るの前半の列: ITR を有効にし（sticky の印を消し）、借りる r0・r1 を読む（列の中でレジスタを書き替えない: やり直せる）。"""
        return ([("dscr", (dscr & _DSCR_KEEP) | DSCR_HDBGEN | DSCR_ITREN)]
                + ([("clear_sticky",)] if dscr & DSCR_STICKY else [])
                + [("read", _to_dtr(0)), ("read", _to_dtr(1))])

    def _enter_reading(self) -> list[tuple]:
        """入るの後半の列: PC（MOV r0, pc）と CPSR（MRS）を r0 経由で読み、書き込みを完了させる（r0 は前半で覚えたので壊してよい。
        r0 に毎回値を入れ直すので、頭からやり直せる）。"""
        return [("exec", MOV_R0_PC), ("read", _to_dtr(0)), ("exec", MRS_R0_CPSR), ("read", _to_dtr(0)),
                *self._barrier_steps()]

    def _entered(self, dscr: int, r0: int, r1: int, raw_pc: int, cpsr: int) -> None:
        self.thumb = bool(cpsr & CPSR_T)
        self._cache = {"r0": r0, "r1": r1, "cpsr": cpsr, "pc": (raw_pc - (4 if self.thumb else 8)) & WORD_MASK}
        self._dirty = set()
        self._moe = (dscr >> DSCR_MOE_SHIFT) & 0xF

    def _leave(self, mask_interrupts: bool) -> None:
        """再開の直前: 書き替えた CPSR・PC と借りた r0・r1 を戻し、ITR を無効にする。`leave_all` の 1 コア分。"""
        leave_all([self], mask_interrupts)

    def _leave_steps(self) -> list[tuple]:
        """戻す列（CPSR・PC は書き替えたときだけ。どれも決まった値を入れ直すので、頭からやり直せる）。"""
        cache, steps = self._cache, []
        if "cpsr" in self._dirty:
            steps += [("put", cache["cpsr"], _from_dtr(0)), ("exec", MSR_CPSR_R0), *self._sync_steps()]
        if "pc" in self._dirty:
            steps += [("put", cache["pc"], _from_dtr(0)), ("exec", MOV_PC_R0)]
        return steps + [("put", cache["r1"], _from_dtr(1)), ("put", cache["r0"], _from_dtr(0))]

    # --- DCC の受け渡し --------------------------------------------------------------------------------

    def _run(self, steps: list[tuple]) -> list[int]:
        """命令の列（fprobe.armv7a_debug の説明）を実行し、"read" の値を順に返す。道がまとめられれば（ARMv7: `run`）1 回の往復で
        確かめながら、まとめられなければ（ARM11）1 命令ずつ。列は頭から実行し直しても同じ結果になるものだけ、"put" を含む列では
        r0 を壊してよいこと（呼ぶ側が守る）。"""
        run = getattr(self.port, "run", None)
        if run is not None:
            return run(steps)
        out = []
        for s in steps:
            if s[0] == "dscr":
                self.port.write_dscr(s[1])
            elif s[0] == "clear_sticky":
                self.port.clear_sticky()
            elif s[0] == "put":
                self.port.to_core(s[1] & WORD_MASK)
                self.port.execute(s[2])
            else:
                self.port.execute(s[1])
                if s[0] == "read":
                    out.append(self.port.from_core())
        return out

    def _barrier_steps(self) -> list[tuple]:
        """書き込みを完了させる列（ARMv7 は DSB、ARMv6 は CP15 の操作。r0 を壊す）。"""
        return [("exec", DSB)] if self.v7 else [("put", 0, _from_dtr(0)), ("exec", CP15["v6_dsb"])]

    def _sync_steps(self) -> list[tuple]:
        """命令の取り込みを揃える列（ARMv7 は ISB、ARMv6 は CP15 の操作。r0 を壊す）。"""
        return [("exec", ISB)] if self.v7 else [("put", 0, _from_dtr(0)), ("exec", CP15["v6_prefetch_flush"])]

    def _barrier(self) -> None:
        self._run(self._barrier_steps())

    def _sync_instruction(self) -> None:
        self._run(self._sync_steps())

    def _mode_steps(self, cpsr: int) -> list[tuple]:
        """CPSR に cpsr を書く列（モードの切り替え。r0 を壊す）。"""
        return [("put", cpsr, _from_dtr(0)), ("exec", MSR_CPSR_R0), *self._sync_steps()]

    def read_state(self, cp15: tuple[str, ...], sp_modes: tuple[int, ...]) -> list[int]:
        """CP15 のレジスタとモードごとの SP をまとめて読む（1 つの列 = ARMv7 なら 1 回の往復）。値は cp15、sp_modes の順。"""
        return read_state_all([self], cp15, sp_modes)[0]

    def entry_reason(self) -> int | None:
        """最後にデバッグ状態に入った理由（DSCR.MOE の数値）。走っているときは None。ステップで止まった後は、ステップの仕組みの
        理由のまま（`halt_cause` は "step" と言う）。"""
        if self._cache is None and self.state() != "halted":
            return None
        return self._moe


# --- 複数のコアをまとめて（同じ道なら、全コアの列を 1 回の往復で） ----------------------------------------------

def _port_many(cores: list[ArmDebugCore], name: str):
    """全コアの道が同じ型で、その型が複数の道をまとめる name（fprobe.armv7a_debug の run_all 等）を持てば、それを返す。"""
    if not cores or any(type(c.port) is not type(cores[0].port) for c in cores):
        return None
    return getattr(type(cores[0].port), name, None)


def _dscr_all(cores: list[ArmDebugCore]) -> list[int]:
    many = _port_many(cores, "dscr_many")
    return many([c.port for c in cores]) if many is not None else [c.port.dscr() for c in cores]


def _write_dscr_all(cores: list[ArmDebugCore], values: list[int]) -> None:
    many = _port_many(cores, "write_dscr_many")
    if many is not None:
        many([(c.port, v) for c, v in zip(cores, values)])
    else:
        for c, v in zip(cores, values):
            c.port.write_dscr(v)


def enable_all(cores: list[ArmDebugCore]) -> None:
    """各コアの停止デバッグ（DSCR.HDBGen）を有効にする。止めはしない。同じ道なら、読み・書き・確かめの読みが全コアで 1 回ずつ。"""
    dscrs = _dscr_all(cores)
    _write_dscr_all(cores, [(d & _DSCR_KEEP) | DSCR_HDBGEN | (d & DSCR_ITREN if d & DSCR_HALTED else 0) for d in dscrs])
    for d in _dscr_all(cores):
        if not d & DSCR_HDBGEN:
            raise ArmDebugError("DSCR.HDBGen が立たない（停止デバッグが許されていない: DBGEN の線・認証）")


def _run_many(cores: list[ArmDebugCore], plans: list[list[tuple]]) -> list[list[int]]:
    """コアごとの命令の列を実行する。道の型が `run_many`（fprobe.armv7a_debug.run_all）を持てば全コアの列をまとめて、無ければ
    コアごとに。"""
    many = _port_many(cores, "run_many")
    if many is not None:
        return many([(c.port, plan) for c, plan in zip(cores, plans)])
    return [c._run(plan) for c, plan in zip(cores, plans)]


def enter_all(cores: list[ArmDebugCore], dscrs: list[int]) -> None:
    """止まっている cores（dscrs はそれぞれの止まった DSCR）に入る: ITR を有効にし、借りるレジスタと PC・CPSR を覚え、書き込みを
    完了させる。前半（r0・r1 を読む）と後半（r0 を使って PC・CPSR を読む）は別の往復: 1 つにまとめると、前半を確かめられずに
    やり直すとき、後半が r0 をすでに壊していることがある。同じ道なら全コアで 2 回の往復。"""
    saved = _run_many(cores, [c._enter_saving(d) for c, d in zip(cores, dscrs)])
    read = _run_many(cores, [c._enter_reading() for c in cores])
    for c, d, (r0, r1), (raw_pc, cpsr) in zip(cores, dscrs, saved, read):
        c._entered(d, r0, r1, raw_pc, cpsr)


def leave_all(cores: list[ArmDebugCore], mask_interrupts: bool) -> None:
    """再開の直前: 各コアの書き替えた CPSR・PC と借りた r0・r1 を戻し（同じ道なら全コアで 1 回の往復）、ITR を無効にする
    （DSCR の書き。ITR を切った後に列のやり直しはできないので、列を確かめた後の別の往復）。"""
    if not cores:
        return
    _run_many(cores, [c._leave_steps() for c in cores])
    values = []
    for c in cores:                                       # 列の最後の DSCR（列より後の読み）の、ターゲットの設定のビットを保つ
        dscr = c.port.last_dscrs[-1] if getattr(c.port, "last_dscrs", None) else c.port.dscr()
        values.append((dscr & _DSCR_KEEP) | DSCR_HDBGEN | (DSCR_INTDIS if mask_interrupts else 0))
    _write_dscr_all(cores, values)
    for c in cores:
        c._cache, c._dirty = None, set()


def prepare_restart_all(cores: list[ArmDebugCore]) -> None:
    """各コアの再開の要求の直前まで（`ArmDebugCore.prepare_restart`）。止まっていないコアがあれば、どのコアも戻す前に断る。
    同じ道なら、全コアのレジスタを戻すのに 1 回、ITR を切るのに 1 回の往復。"""
    for c in cores:
        c._require_halted()
    leave_all(cores, mask_interrupts=False)
    for c in cores:
        c._stepped = False


def read_state_all(cores: list[ArmDebugCore], cp15: tuple[str, ...], sp_modes: tuple[int, ...]) -> list[list[int]]:
    """止まっている各コアの CP15 のレジスタとモードごとの SP（`ArmDebugCore.read_state`）。同じ道なら全コアで 1 回の往復。"""
    for c in cores:
        c._require_halted()
        c._dirty.add("r0")
    return _run_many(cores, [c._state_steps(cp15, sp_modes) for c in cores])

