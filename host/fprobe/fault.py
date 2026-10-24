"""Cortex-M の故障の解析: 止まったコアの故障レジスタ（CFSR・HFSR・DFSR・MMFAR・BFAR）と例外のフレームを読み、
原因・故障した命令の番地・その時のレジスタを言葉で返す（IAR の Fault exception viewer・Ozone の相当）。

    report = analyze(target, symbols)       # target: read32 / read_core_register（pyOCD の CortexM と同じ）
    report["causes"]                        # [{"bit": "NOCP", "register": "UFSR", "meaning": ..., "hint": ...}]

ARMv7-M / ARMv8-M（M3・M4・M7・M33）は CFSR で原因まで分かる。ARMv6-M（M0・M0+）は HardFault だけ（原因の
レジスタが無い）なので、例外のフレームの位置だけを返す。
参考: ARMv7-M Architecture Reference Manual B3.2.15〜B3.2.18、B1.5.6（例外のフレーム）。
"""

from dataclasses import dataclass

__all__ = ["analyze", "decode_cfsr", "decode_hfsr", "decode_dfsr", "dfsr_bits", "halt_reason"]

CPUID, ICSR, CFSR, HFSR, DFSR, MMFAR, BFAR, AFSR, CPACR = (0xE000ED00, 0xE000ED04, 0xE000ED28, 0xE000ED2C,
                                                           0xE000ED30, 0xE000ED34, 0xE000ED38, 0xE000ED3C, 0xE000ED88)
WRITE_ONE_TO_CLEAR = ("CFSR", "HFSR", "DFSR")   # ARMv7-M / v8-M の rc_w1（1 を書いたビットを消す）
_V6M_PARTNOS = {0xC20, 0xC60, 0xD20}       # Cortex-M0 / M0+ / M23（M23 は v8-M Baseline: CFSR が無い）
EXCEPTIONS = {1: "Reset", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault", 6: "UsageFault", 7: "SecureFault",
              11: "SVCall", 12: "DebugMonitor", 14: "PendSV", 15: "SysTick"}


@dataclass(frozen=True)
class _Bit:
    position: int
    name: str
    meaning: str
    hint: str = ""


# hint はアーキテクチャの定義から言えることだけ（ARMv7-M ARM B3.2.15〜B3.2.16）。経験的な原因の候補は書かない
_CFSR_BITS = (
    # MemManage（MMFSR）
    _Bit(0, "IACCVIOL", "命令の読み出しが MPU に違反した", "XN（実行禁止）の領域からの読み出しも含む（MPU が無効でも）"),
    _Bit(1, "DACCVIOL", "データのアクセスが MPU に違反した", "MMFAR に番地（MMARVALID のとき）。積まれた PC が故障した命令"),
    _Bit(3, "MUNSTKERR", "例外から戻るときの取り出しで MPU 違反"),
    _Bit(4, "MSTKERR", "例外に入るときの積み込みで MPU 違反"),
    _Bit(5, "MLSPERR", "浮動小数点の文脈の遅延保存で MPU 違反"),
    _Bit(7, "MMARVALID", "MMFAR に故障の番地が入っている"),
    # BusFault（BFSR）
    _Bit(8, "IBUSERR", "命令の読み出しでバスエラー"),
    _Bit(9, "PRECISERR", "データのアクセスでバスエラー（正確）", "BFAR に番地（BFARVALID のとき）。積まれた PC が故障した命令"),
    _Bit(10, "IMPRECISERR", "データの書き込みでバスエラー（不正確）",
         "書き込みバッファのため、積まれた PC は故障の後の命令。BFAR は入らない"),
    _Bit(11, "UNSTKERR", "例外から戻るときの取り出しでバスエラー"),
    _Bit(12, "STKERR", "例外に入るときの積み込みでバスエラー"),
    _Bit(13, "LSPERR", "浮動小数点の文脈の遅延保存でバスエラー"),
    _Bit(15, "BFARVALID", "BFAR に故障の番地が入っている"),
    # UsageFault（UFSR）
    _Bit(16, "UNDEFINSTR", "未定義の命令を実行した"),
    _Bit(17, "INVSTATE", "Thumb の状態でない実行（EPSR.T = 0）",
         "分岐先・ベクタ表の番地の bit0 が 0 だった（EPSR.T はそこから入る）"),
    _Bit(18, "INVPC", "例外からの戻りの値（EXC_RETURN）が不正"),
    _Bit(19, "NOCP", "コプロセッサ（FPU）の命令を、無効のまま実行した",
         "CPACR の CP10・CP11 が有効でないか、コプロセッサが無い"),
    _Bit(20, "STKOF", "スタックの下限（PSPLIM・MSPLIM）を越えた（ARMv8-M）"),
    _Bit(24, "UNALIGNED", "揃っていない番地へのアクセス（UNALIGN_TRP が 1）"),
    _Bit(25, "DIVBYZERO", "0 で割った（DIV_0_TRP が 1）"),
)
_HFSR_BITS = (
    _Bit(1, "VECTTBL", "例外のベクタ表の読み出しでバスエラー", "読んだ番地は VTOR + 4 × 例外の番号"),
    _Bit(30, "FORCED", "設定できる故障（MemManage・BusFault・UsageFault）が HardFault に格上げされた",
         "その故障が無効か、優先度で入れなかった。元の故障は CFSR に出る"),
    _Bit(31, "DEBUGEVT", "デバッグの出来事が HardFault になった",
         "停止のデバッグ（DHCSR.C_DEBUGEN）もデバッグモニタも無効のときの BKPT 等"),
)
_DFSR_BITS = (
    _Bit(0, "HALTED", "止める要求・1 命令の実行で止まった"),
    _Bit(1, "BKPT", "ブレークポイント（BKPT 命令・FPB）で止まった"),
    _Bit(2, "DWTTRAP", "ウォッチポイント（DWT）で止まった"),
    _Bit(3, "VCATCH", "ベクタキャッチ（例外に入るところ）で止まった"),
    _Bit(4, "EXTERNAL", "外からの止める要求（EDBGRQ）で止まった"),
)


def _decode(value: int, bits, register) -> list[dict]:
    """立っているビット。register(位置) = そのビットのレジスタの名前（CFSR は MMFSR・BFSR・UFSR の部分）。"""
    return [{"bit": b.name, "register": register(b.position), "meaning": b.meaning, **({"hint": b.hint} if b.hint else {})}
            for b in bits if value >> b.position & 1]


def _cfsr_part(position: int) -> str:
    """CFSR の部分（ARMv7-M ARM B3.2.15: [7:0] MMFSR、[15:8] BFSR、[31:16] UFSR）。"""
    return "MMFSR" if position < 8 else "BFSR" if position < 16 else "UFSR"


def decode_cfsr(value: int) -> list[dict]:
    return _decode(value, _CFSR_BITS, _cfsr_part)


def decode_hfsr(value: int) -> list[dict]:
    return _decode(value, _HFSR_BITS, lambda _: "HFSR")


def decode_dfsr(value: int) -> list[dict]:
    return _decode(value, _DFSR_BITS, lambda _: "DFSR")


def dfsr_bits(dfsr: int) -> list[str]:
    """DFSR の立っているビットの名前（HALTED / BKPT / DWTTRAP / VCATCH / EXTERNAL。ビットの順）。"""
    return [b.name for b in _DFSR_BITS if dfsr >> b.position & 1]


def halt_reason(dfsr: int, instruction: int | None, breakpoint: bool = False) -> str | None:
    """止まった理由を 1 語で（DFSR は pyOCD が再開・ステップのたびにクリアするので、その停止の分だけ）。
    instruction: PC の 16 ビットの命令（BKPT 0xBExx なら、デバッガが置いたもの（breakpoint=True）でなければファームウェア
    自身の BKPT = panic・assert・セミホスティング等）。複数のビットなら 1 語にはそのうちの 1 つを選ぶ（BKPT・DWTTRAP・
    VCATCH・EXTERNAL・HALTED の順）。立っていたビットは全部 dfsr_bits で添えること（1 語で事実を潰さない）。"""
    if dfsr & 0x02:
        own = instruction is not None and instruction & 0xFF00 == 0xBE00 and not breakpoint
        return "bkpt_instruction" if own else "breakpoint"
    for bit, name in ((0x04, "watchpoint"), (0x08, "vector_catch"), (0x10, "external"), (0x01, "request")):
        if dfsr & bit:
            return name
    return None


def _place(symbols, address: int) -> dict:
    out = {"address": address}
    if symbols is not None:
        out["where"] = str(symbols.describe(address))
    return out


def analyze(target, symbols=None, entry_sp: int | None = None) -> dict:
    """止まったコアの故障を調べる。例外の中で止まっていれば、積まれたフレーム（故障した命令の番地・レジスタ）も返す。
    entry_sp: ハンドラに入った時の sp（DWARF の CFI で求めたハンドラの CFA）。ハンドラが既にスタックに積んでいると
    いまの MSP はフレームを指さないため。None なら、いまの MSP / PSP（ハンドラの先頭で止まったとき正しい）。"""
    partno = target.read32(CPUID) >> 4 & 0xFFF
    v6m = partno in _V6M_PARTNOS
    xpsr = target.read_core_register("xpsr")
    ipsr = xpsr & 0x1FF
    out: dict = {"architecture": "ARMv6-M / v8-M Baseline" if v6m else "ARMv7-M / v8-M Mainline",
                 "exception": None, "causes": [], "faulted": False}
    if ipsr:
        out["exception"] = {"number": ipsr, "name": EXCEPTIONS.get(ipsr, f"IRQ{ipsr - 16}" if ipsr >= 16 else "?")}
    dfsr = target.read32(DFSR)
    out["halt_reasons"] = decode_dfsr(dfsr)
    if not v6m:
        cfsr, hfsr = target.read32(CFSR), target.read32(HFSR)
        out["registers"] = {"CFSR": cfsr, "HFSR": hfsr, "DFSR": dfsr, "MMFAR": None, "BFAR": None,
                            "CPACR": target.read32(CPACR)}
        # 読んだ番地（32 ビット）と、1 を書いたビットが消える（rc_w1）レジスタ。書く側が target.memory.write で書ける
        out["locations"] = {name: {"address": address, "size": 4}
                            for name, address in (("CFSR", CFSR), ("HFSR", HFSR), ("DFSR", DFSR), ("MMFAR", MMFAR),
                                                  ("BFAR", BFAR), ("CPACR", CPACR))}
        out["write_one_to_clear"] = list(WRITE_ONE_TO_CLEAR)
        out["causes"] = decode_hfsr(hfsr) + decode_cfsr(cfsr)
        if cfsr >> 7 & 1:
            out["registers"]["MMFAR"] = target.read32(MMFAR)
            out["fault_address"] = _place(symbols, out["registers"]["MMFAR"])
        if cfsr >> 15 & 1:
            out["registers"]["BFAR"] = target.read32(BFAR)
            out["fault_address"] = _place(symbols, out["registers"]["BFAR"])
        out["faulted"] = bool(cfsr or hfsr & 0xC0000002)
    elif ipsr == 3:
        out["faulted"] = True                       # v6-M: HardFault の中。原因のレジスタは無い
        out["causes"] = [{"bit": "HardFault", "meaning": "HardFault（ARMv6-M は原因のレジスタが無い）",
                          "hint": "原因はレジスタに残らない。積まれたフレーム（frame）に故障したときの PC とレジスタがある"}]
    if ipsr:
        out["frame"] = _stacked_frame(target, symbols, entry_sp)
    return out


def _stacked_frame(target, symbols, entry_sp: int | None) -> dict | None:
    """例外に入ったときに積まれたフレーム（r0〜r3・r12・lr・pc・xpsr）。LR（EXC_RETURN）で MSP か PSP かを選ぶ。
    ハンドラが LR を書き換えた後で止まっていると読めない（None）。sp_source: どの SP の値を使ったか（psp = 今の PSP /
    entry_sp = 渡された入った時の SP / msp_now = 今の MSP。ハンドラが積んだ後ならフレームからずれる）。"""
    lr = target.read_core_register("lr")
    if lr >> 24 != 0xFF:
        return None
    use_psp = bool(lr & 0x4)
    if use_psp:
        sp, source = target.read_core_register("psp"), "psp"   # ハンドラは MSP を使うので、PSP はフレームを指したまま
    elif entry_sp is not None:
        sp, source = entry_sp, "entry_sp"
    else:
        sp, source = target.read_core_register("msp"), "msp_now"
    words = [target.read32(sp + 4 * i) for i in range(8)]
    names = ("r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr")
    regs = dict(zip(names, words))
    extended = not lr & 0x10                       # 浮動小数点の文脈も積まれている
    caller_sp = sp + (0x68 if extended else 0x20) + (4 if regs["xpsr"] >> 9 & 1 else 0)
    return {"stack": "PSP" if use_psp else "MSP", "sp": sp, "sp_source": source, "exc_return": lr, "fp_context": extended,
            "registers": regs, "caller_sp": caller_sp,
            "locations": {name: {"address": sp + 4 * i, "size": 4} for i, name in enumerate(names)},
            "pc": _place(symbols, regs["pc"]), "lr": _place(symbols, regs["lr"] & ~1)}
