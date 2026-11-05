"""Arm の A プロファイルの停止デバッグの定義: DSCR のビット・ITR に入れる命令の符号（MCR/MRC・LDR/STR・CP15 の操作）・
レジスタ名・ArmDebugError。

状態を持たない定数と符号化だけ（fprobe.arm_debug の ArmDebugCore が使う。fprobe.arm_debug が再公開する）。
"""

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
    # 変換の設定・プロセスの文脈・システム制御（読むだけ。read_cp15 が使う。Linux のプロセスを再現するのに要る）
    "ttbr0": arm_mrc(15, 0, 0, 2, 0, 0), "ttbr1": arm_mrc(15, 0, 0, 2, 0, 1), "ttbcr": arm_mrc(15, 0, 0, 2, 0, 2),
    "contextidr": arm_mrc(15, 0, 0, 13, 0, 1), "sctlr": arm_mrc(15, 0, 0, 1, 0, 0),
}
CP15_READABLE = ("ttbr0", "ttbr1", "ttbcr", "contextidr", "sctlr")
MODE_MASK = 0x1F
WORD_MASK = 0xFFFFFFFF                                 # 32 ビットの語
MODE_USR, MODE_SYS = 0x10, 0x1F
_FAST_READ_ROUND_TRIPS = 4                               # read_memory_fast の往復（ARMv7 の JTAG。語の数によらない）


class ArmDebugError(RuntimeError):
    pass
