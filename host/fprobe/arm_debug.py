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
"""

import time
from typing import Protocol

__all__ = ["ArmDebugCore", "ArmDebugError", "CoreDebugPort", "REGISTER_NAMES", "arm_mcr", "arm_mrc"]

# --- DSCR（ARMv7 と ARMv6 で同じ位置） ----------------------------------------------------------------

DSCR_HALTED, DSCR_RESTARTED = 1 << 0, 1 << 1
DSCR_MOE_SHIFT = 2
DSCR_SDABORT, DSCR_ADABORT, DSCR_UND = 1 << 6, 1 << 7, 1 << 8
DSCR_STICKY = DSCR_SDABORT | DSCR_ADABORT | DSCR_UND
DSCR_INTDIS, DSCR_UDCCDIS, DSCR_ITREN, DSCR_HDBGEN, DSCR_MDBGEN = 1 << 11, 1 << 12, 1 << 13, 1 << 14, 1 << 15
DSCR_EXTDCC = 3 << 20
DSCR_EXTDCC_FAST = 2 << 20                                # 速いモード: DTRTX を読むたびに ITR の命令を実行し直す
DSCR_INSTRCOMPL = 1 << 24
DSCR_TXFULL, DSCR_RXFULL = 1 << 29, 1 << 30
_DSCR_KEEP = DSCR_UDCCDIS | DSCR_MDBGEN                  # 止めても再開しても、ターゲットの設定のまま残すビット

# 止まった理由（DSCR.MOE。ARMv7-A ARM C11.11.20。8 = OS Unlock catch・9 = 同期の watchpoint（v7.1）以外の値は予約）。
# 表に無い値は "unknown"（停止の要求等の名前を当てない）
_MOE = {0: "halt_request", 1: "breakpoint", 2: "watchpoint", 3: "bkpt", 4: "external", 5: "vector_catch",
        8: "os_unlock_catch", 0xA: "watchpoint"}

# --- CPSR ---------------------------------------------------------------------------------------------
CPSR_T, CPSR_F, CPSR_I = 1 << 5, 1 << 6, 1 << 7

# --- BCR / WCR -----------------------------------------------------------------------------------------
BP_ENABLE, BP_ANY_PRIVILEGE = 1 << 0, 3 << 1
BP_MISMATCH = 4 << 20                                    # ARMv7 の BT = 0b0100 / ARM11 の bits[22:21] = 0b10（同じ値）
WP_LOAD, WP_STORE = 1 << 3, 2 << 3
WP_KINDS = {"read": WP_LOAD, "write": WP_STORE, "access": WP_LOAD | WP_STORE}

REGISTER_NAMES = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "cpsr"]


def arm_mcr(cp: int, opc1: int, rt: int, crn: int, crm: int, opc2: int) -> int:
    """MCR p<cp>, opc1, Rt, CRn, CRm, opc2（ARM の符号化、条件は常に）。"""
    return 0xEE000010 | opc1 << 21 | crn << 16 | rt << 12 | cp << 8 | opc2 << 5 | crm


def arm_mrc(cp: int, opc1: int, rt: int, crn: int, crm: int, opc2: int) -> int:
    return arm_mcr(cp, opc1, rt, crn, crm, opc2) | 1 << 20


def _to_dtr(rt: int) -> int:                              # MCR p14, 0, Rt, c0, c5, 0（Rt → DBGDTRTX）
    return arm_mcr(14, 0, rt, 0, 5, 0)


def _from_dtr(rt: int) -> int:                            # MRC p14, 0, Rt, c0, c5, 0（DBGDTRRX → Rt）
    return arm_mrc(14, 0, rt, 0, 5, 0)


MOV_R0_PC, MOV_PC_R0, MRS_R0_CPSR, MSR_CPSR_R0 = 0xE1A0000F, 0xE1A0F000, 0xE10F0000, 0xE12FF000
# 大きさ（バイト）→ (LDR r1, [r0], #n, STR r1, [r0], #n)
LOAD_STORE = {4: (0xE4901004, 0xE4801004), 2: (0xE0D010B2, 0xE0C010B2), 1: (0xE4D01001, 0xE4C01001)}
LDC_DTRTX = 0xECB05E01                                     # LDC p14, c5, [r0], #4（メモリの語を DTRTX へ。速い読み）
DSB, ISB = 0xF57FF04F, 0xF57FF06F                        # ARMv7 の命令（ARMv6 は CP15 の操作で代わりにする）
CP15 = {
    "ctr": arm_mrc(15, 0, 0, 0, 0, 1), "mpidr": arm_mrc(15, 0, 0, 0, 0, 5),
    "dccmvau": arm_mcr(15, 0, 0, 7, 11, 1),              # ARMv7: D キャッシュの行を PoU まで掃き出す（MVA）
    "iciallu": arm_mcr(15, 0, 0, 7, 5, 0), "bpiall": arm_mcr(15, 0, 0, 7, 5, 6),
    "iciallis": arm_mcr(15, 0, 0, 7, 1, 0), "bpiallis": arm_mcr(15, 0, 0, 7, 1, 6),   # 内側の共有域（MP 拡張）
    "v6_clean_dline": arm_mcr(15, 0, 0, 7, 10, 1),       # ARMv6: D キャッシュの行を掃き出す（MVA）
    "v6_dsb": arm_mcr(15, 0, 0, 7, 10, 4), "v6_prefetch_flush": arm_mcr(15, 0, 0, 7, 5, 4),
}


class ArmDebugError(RuntimeError):
    pass


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


def _byte_select(address: int, thumb: bool) -> int:
    """BCR の BAS: ARM の命令は 4 バイト（0b1111）、Thumb は番地の bit 1 で下か上の半語。"""
    if address & 2:
        return 0xC << 5
    return (0x3 if thumb else 0xF) << 5


class ArmDebugCore:
    """1 つのコアの停止デバッグ（CoreDebugPort の上）。"""

    def __init__(self, port: CoreDebugPort, timeout_s: float = 0.5):
        self.port = port
        self.timeout_s = timeout_s
        didr = port.didr()
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
        dscr = self.port.dscr()
        self.port.write_dscr((dscr & _DSCR_KEEP) | DSCR_HDBGEN | (dscr & DSCR_ITREN if dscr & DSCR_HALTED else 0))
        if not self.port.dscr() & DSCR_HDBGEN:
            raise ArmDebugError("DSCR.HDBGen が立たない（停止デバッグが許されていない: DBGEN の線・認証）")

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
        self._require_halted()
        self._leave(mask_interrupts=False)
        self._stepped = False
        self.port.restart_request()
        self._wait(DSCR_RESTARTED | DSCR_HALTED, "再開しない")

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
            self.port.restart_request()
            deadline = time.monotonic() + self.timeout_s
            requested = False
            while not self.port.dscr() & DSCR_HALTED:
                if time.monotonic() >= deadline:              # WFI 等で次の命令に進まない: 止めて終える
                    self.port.halt_request()
                    self._wait(DSCR_HALTED, "ステップの後に止まらない")
                    requested = True
                    break
                time.sleep(0.001)
        finally:
            self.port.write_breakpoint(slot, 0, 0)
        self._ensure_entered(self.port.dscr())
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
        """止まった直後: ITR を有効にし、借りるレジスタと PC・CPSR を覚え、書き込みを完了させる（DSB）。"""
        self.port.write_dscr((dscr & _DSCR_KEEP) | DSCR_HDBGEN | DSCR_ITREN)
        if dscr & DSCR_STICKY:
            self.port.clear_sticky()
        cache = {"r0": self._get(0), "r1": self._get(1)}
        self.port.execute(MOV_R0_PC)
        raw_pc = self._get(0)
        self.port.execute(MRS_R0_CPSR)
        cache["cpsr"] = self._get(0)
        self.thumb = bool(cache["cpsr"] & CPSR_T)
        cache["pc"] = (raw_pc - (4 if self.thumb else 8)) & 0xFFFFFFFF
        self._cache, self._dirty = cache, set()
        self._moe = (dscr >> DSCR_MOE_SHIFT) & 0xF
        self._barrier()

    def _leave(self, mask_interrupts: bool) -> None:
        """再開の直前: 書き替えた CPSR・PC と借りた r0・r1 を戻し、ITR を無効にする。"""
        cache = self._cache
        if "cpsr" in self._dirty:
            self._set_r0(cache["cpsr"])
            self.port.execute(MSR_CPSR_R0)
            self._sync_instruction()
        if "pc" in self._dirty:
            self._set_r0(cache["pc"])
            self.port.execute(MOV_PC_R0)
        self._put(1, cache["r1"])
        self._put(0, cache["r0"])
        dscr = self.port.dscr()
        self.port.write_dscr((dscr & _DSCR_KEEP) | DSCR_HDBGEN | (DSCR_INTDIS if mask_interrupts else 0))
        self._cache, self._dirty = None, set()

    # --- DCC の受け渡し --------------------------------------------------------------------------------

    def _get(self, rt: int) -> int:
        self.port.execute(_to_dtr(rt))
        return self.port.from_core()

    def _put(self, rt: int, value: int) -> None:
        self.port.to_core(value & 0xFFFFFFFF)
        self.port.execute(_from_dtr(rt))

    def _set_r0(self, value: int) -> None:
        self._put(0, value)

    def _set_cached(self, name: str, value: int) -> None:
        self._cache[name] = value & 0xFFFFFFFF
        self._dirty.add(name)

    # --- レジスタ --------------------------------------------------------------------------------------

    @staticmethod
    def register_number(name: str) -> int | str:
        key = name.strip().lower()
        if key in ("pc", "r15"):
            return "pc"
        if key == "cpsr":
            return "cpsr"
        aliases = {"sp": 13, "lr": 14, "fp": 11, "ip": 12, "sb": 9, "sl": 10}
        if key in aliases:
            return aliases[key]
        if key.startswith("r") and key[1:].isdigit() and 0 <= int(key[1:]) <= 14:
            return int(key[1:])
        raise ValueError(f"レジスタ {name!r} を知らない（r0〜r15・sp・lr・pc・cpsr。今のモードのもの。"
                         "他のモードのバンクのレジスタ・SPSR・CP15 は未対応）")

    def read_register(self, name: str) -> int:
        number = self.register_number(name)
        cache = self._require_halted()
        if number in ("pc", "cpsr"):
            return cache[number]
        if number in (0, 1):
            return cache[f"r{number}"]
        return self._get(number)

    def write_register(self, name: str, value: int) -> None:
        number = self.register_number(name)
        self._require_halted()
        if number in ("pc", "cpsr"):
            self._set_cached(number, value)
        elif number in (0, 1):
            self._set_cached(f"r{number}", value)
        else:
            self._put(number, value)

    # --- メモリ ----------------------------------------------------------------------------------------

    def _check_abort(self, dscr: int | None, address: int, what: str) -> None:
        if dscr is None or not dscr & DSCR_STICKY:
            return
        self.port.clear_sticky()
        if dscr & DSCR_UND:
            raise ArmDebugError(f"0x{address:08X} の{what}: コアが命令を未定義として扱った（DSCR 0x{dscr:08X}）")
        raise ArmDebugError(f"0x{address:08X} の{what}でアボート（番地はコアの見る番地 = MMU が有効なら仮想番地。"
                            f"変換が無い・アクセス権が無い・存在しない番地。DSCR 0x{dscr:08X}）")

    def read_memory(self, address: int, count: int, size: int = 4) -> list[int]:
        """size（1 / 2 / 4）バイトの値を count 個、コアに読ませる。"""
        load = LOAD_STORE[size][0]
        self._require_halted()
        self._dirty |= {"r0", "r1"}
        self._set_r0(address)
        out = []
        for i in range(count):
            here = address + i * size
            self._check_abort(self.port.execute(load), here, "読み")
            try:
                out.append(self._get(1))
            except ArmDebugError:
                self._check_abort(self.port.dscr(), here, "読み")
                raise
        self._check_abort(self.port.dscr(), address, "読み")
        return out

    def read_memory_fast(self, address: int, count: int) -> list[int]:
        """32 ビットの語を count 個、ARMv7 の DCC の速いモードで読む（OpenOCD の cortex_a_read_cpu_memory_fast と同じ手順）。
        LDC を ITR に書き、DTRTX を count 回まとめて読む（読むたびにコアが次の語を読む。1 語ごとの往復が無い）。
        最後の 1 語は非ブロックのモードに戻してから読む。ポートがまとめて読めなければ read_memory と同じ。"""
        block = getattr(self.port, "read_dtrtx_fast", None)
        if not self.v7 or block is None or count < 2:
            return self.read_memory(address, count)
        self._require_halted()
        self._dirty |= {"r0"}
        self._set_r0(address)
        dscr = self.port.dscr()
        normal = dscr & ~DSCR_EXTDCC & ~(DSCR_HALTED | DSCR_RESTARTED | DSCR_STICKY | DSCR_INSTRCOMPL
                                          | DSCR_TXFULL | DSCR_RXFULL | (0xF << DSCR_MOE_SHIFT))
        self.port.write_dscr(normal | DSCR_EXTDCC_FAST)
        failed = True
        try:
            # 速いモードでは ITR に書いても実行されず、DTRTX を読むと「今の値を返してから」命令を実行する（実機の Cortex-A7、
            # 2026-10-23）。よって最初の読みは前の値（捨てる）で、count 回の読みで count−1 語、最後の 1 語は下で読む
            out = block(LDC_DTRTX, count)[1:]
            failed = False
        finally:
            self.port.write_dscr(normal)                    # どの道でも非ブロックのモードに戻す
            if failed:
                self.port.clear_sticky()                    # アボートの印を残したまま、再開の手順の命令を流さない
        dscr = self._wait(DSCR_INSTRCOMPL, "速い読みの最後の命令が終わらない")
        self._check_abort(dscr, address, f"{count} 語の速い読み")
        out.append(self.port.from_core())
        return out

    def write_memory(self, address: int, values: list[int], size: int = 4) -> None:
        """size バイトの値の列を、コアに書かせる。書いた範囲は命令として実行されても良いようにキャッシュを揃える。"""
        if not values:
            return
        store = LOAD_STORE[size][1]
        self._require_halted()
        self._dirty |= {"r0", "r1"}
        self._set_r0(address)
        mask = (1 << (8 * size)) - 1
        for i, value in enumerate(values):
            self._put(1, value & mask)
            self._check_abort(self.port.execute(store), address + i * size, "書き込み")
        self._check_abort(self.port.dscr(), address, "書き込み")
        self._sync_caches(address, len(values) * size)

    # --- キャッシュ・バリア ------------------------------------------------------------------------------

    def _barrier(self) -> None:
        if self.v7:
            self.port.execute(DSB)
        else:
            self._set_r0(0)
            self.port.execute(CP15["v6_dsb"])

    def _sync_instruction(self) -> None:
        if self.v7:
            self.port.execute(ISB)
        else:
            self._set_r0(0)
            self.port.execute(CP15["v6_prefetch_flush"])

    def _read_cp15(self, name: str) -> int:
        self.port.execute(CP15[name])
        return self._get(0)

    def line_size(self) -> int:
        if self._line is None:
            ctr = self._read_cp15("ctr")
            self._line = 4 << ((ctr >> 16) & 0xF) if ctr >> 29 == 0b100 else 1 << (((ctr >> 12) & 3) + 3)
        return self._line

    def _sync_caches(self, address: int, length: int) -> None:
        """書いた範囲: D キャッシュを PoU まで掃き出し、I キャッシュ・分岐予測を無効にする（ARMv7 で MP 拡張が
        あれば内側の共有域の全コアに）。"""
        self._dirty.add("r0")
        line = self.line_size()
        if self.v7 and self._mp is None:
            self._mp = bool(self._read_cp15("mpidr") >> 31)
        clean = CP15["dccmvau"] if self.v7 else CP15["v6_clean_dline"]
        for at in range(address & ~(line - 1), address + length, line):
            self._set_r0(at)
            self.port.execute(clean)
        self._set_r0(0)
        if self.v7:
            self.port.execute(DSB)
            self.port.execute(CP15["iciallis" if self._mp else "iciallu"])
            self.port.execute(CP15["bpiallis" if self._mp else "bpiall"])
            self.port.execute(DSB)
        else:
            self.port.execute(CP15["v6_dsb"])
            self.port.execute(CP15["iciallu"])
            self.port.execute(CP15["bpiall"])

    # --- ブレークポイント・ウォッチポイント --------------------------------------------------------------

    def set_breakpoint(self, index: int, address: int, thumb: bool | None = None) -> None:
        """index（0〜breakpoint_count - 2）の組を、番地の一致に置く。thumb は命令の種類（None なら止まったときの
        CPSR.T）: ARM は 4 バイト、Thumb は半語を見る。"""
        if not 0 <= index < self.breakpoint_count - 1:
            raise ValueError(f"ブレークポイントの組は 0〜{self.breakpoint_count - 2}（最後の組はステップ用）")
        thumb = self.thumb if thumb is None else thumb
        self.port.write_breakpoint(index, address & ~3, _byte_select(address, thumb) | BP_ANY_PRIVILEGE | BP_ENABLE)

    def clear_breakpoint(self, index: int) -> None:
        self.port.write_breakpoint(index, 0, 0)

    def set_watchpoint(self, index: int, address: int, size: int, kind: str) -> bool:
        """index の組を、番地の size バイト（1 / 2 / 4、1 語の中）の kind（read / write / access）に置く。"""
        if size not in (1, 2, 4) or (address & 3) + size > 4 or address % size:
            return False
        if not 0 <= index < self.watchpoint_count:
            raise ValueError(f"ウォッチポイントの組は 0〜{self.watchpoint_count - 1}")
        byte_select = ((1 << size) - 1) << (address & 3)
        self.port.write_watchpoint(index, address & ~3,
                                   byte_select << 5 | WP_KINDS[kind] | BP_ANY_PRIVILEGE | BP_ENABLE)
        return True

    def clear_watchpoint(self, index: int) -> None:
        self.port.write_watchpoint(index, 0, 0)
