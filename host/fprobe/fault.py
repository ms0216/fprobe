"""Cortex-M の故障の解析: 止まったコアの故障レジスタ（CFSR・HFSR・DFSR・MMFAR・BFAR）と例外のフレームを読み、
原因・故障した命令の番地・その時のレジスタを言葉で返す（IAR の Fault exception viewer・Ozone の相当）。

    report = analyze(target, symbols)       # target: read32 / read_core_register（pyOCD の CortexM と同じ）
    report["causes"]                        # [{"bit": "NOCP", "meaning": ..., "hint": ...}]

ARMv7-M / ARMv8-M（M3・M4・M7・M33）は CFSR で原因まで分かる。ARMv6-M（M0・M0+）は HardFault だけ（原因の
レジスタが無い）なので、例外のフレームの位置だけを返す。
参考: ARMv7-M Architecture Reference Manual B3.2.15〜B3.2.18、B1.5.6（例外のフレーム）。
"""

from dataclasses import dataclass

__all__ = ["analyze", "decode_cfsr", "decode_hfsr", "decode_dfsr"]

CPUID, ICSR, CFSR, HFSR, DFSR, MMFAR, BFAR, AFSR, CPACR = (0xE000ED00, 0xE000ED04, 0xE000ED28, 0xE000ED2C,
                                                           0xE000ED30, 0xE000ED34, 0xE000ED38, 0xE000ED3C, 0xE000ED88)
_V6M_PARTNOS = {0xC20, 0xC60, 0xD20}       # Cortex-M0 / M0+ / M23（M23 は v8-M Baseline: CFSR が無い）
EXCEPTIONS = {1: "Reset", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault", 6: "UsageFault", 7: "SecureFault",
              11: "SVCall", 12: "DebugMonitor", 14: "PendSV", 15: "SysTick"}


@dataclass(frozen=True)
class _Bit:
    position: int
    name: str
    meaning: str
    hint: str = ""


_CFSR_BITS = (
    # MemManage（MMFSR）
    _Bit(0, "IACCVIOL", "命令の読み出しが MPU に違反した", "実行禁止（XN）の領域・MPU で許していない番地へ分岐した"),
    _Bit(1, "DACCVIOL", "データのアクセスが MPU に違反した", "MMFAR の番地（MMARVALID のとき）。NULL ポインタの参照が多い"),
    _Bit(3, "MUNSTKERR", "例外から戻るときの取り出しで MPU 違反", "スタックの壊れ"),
    _Bit(4, "MSTKERR", "例外に入るときの積み込みで MPU 違反", "スタックのあふれ（スタックの下限を越えた）"),
    _Bit(5, "MLSPERR", "浮動小数点の文脈の遅延保存で MPU 違反", "スタックのあふれ"),
    _Bit(7, "MMARVALID", "MMFAR に故障の番地が入っている"),
    # BusFault（BFSR）
    _Bit(8, "IBUSERR", "命令の読み出しでバスエラー", "存在しない番地・電源や時計の止まった領域へ分岐した"),
    _Bit(9, "PRECISERR", "データのアクセスでバスエラー（正確）", "BFAR の番地（BFARVALID のとき）。積まれた PC が故障した命令"),
    _Bit(10, "IMPRECISERR", "データの書き込みでバスエラー（不正確）",
         "書き込みバッファのため、積まれた PC は故障の後の命令。時計の入っていない周辺への書き込みが多い"),
    _Bit(11, "UNSTKERR", "例外から戻るときの取り出しでバスエラー", "スタックの壊れ"),
    _Bit(12, "STKERR", "例外に入るときの積み込みでバスエラー", "スタックのあふれ（SRAM の外へ）"),
    _Bit(13, "LSPERR", "浮動小数点の文脈の遅延保存でバスエラー", "スタックのあふれ"),
    _Bit(15, "BFARVALID", "BFAR に故障の番地が入っている"),
    # UsageFault（UFSR）
    _Bit(16, "UNDEFINSTR", "未定義の命令を実行した", "壊れたコード・データ領域への分岐・別の CPU 向けのビルド"),
    _Bit(17, "INVSTATE", "Thumb の状態でない実行（EPSR.T = 0）",
         "番地の bit0 が 0 の関数ポインタへ分岐した（関数ポインタの壊れ・ベクタ表の bit0）"),
    _Bit(18, "INVPC", "例外からの戻りの値（EXC_RETURN）が不正", "LR の壊れ・スタックの壊れ"),
    _Bit(19, "NOCP", "コプロセッサ（FPU）の命令を、無効のまま実行した",
         "CPACR の CP10・CP11 を有効にしていない（起動のコードで FPU を有効にする）"),
    _Bit(20, "STKOF", "スタックの下限（PSPLIM・MSPLIM）を越えた（ARMv8-M）", "スタックのあふれ"),
    _Bit(24, "UNALIGNED", "揃っていない番地へのアクセス（UNALIGN_TRP が 1）", "構造体のパック・ポインタの型の取り違え"),
    _Bit(25, "DIVBYZERO", "0 で割った（DIV_0_TRP が 1）"),
)
_HFSR_BITS = (
    _Bit(1, "VECTTBL", "例外のベクタ表の読み出しでバスエラー", "VTOR の値・ベクタ表の場所"),
    _Bit(30, "FORCED", "設定できる故障（MemManage・BusFault・UsageFault）が HardFault に格上げされた",
         "その故障が無効か、優先度が足りない。原因は CFSR を見る"),
    _Bit(31, "DEBUGEVT", "デバッグの出来事が HardFault になった", "デバッガが無いのに BKPT を実行した等"),
)
_DFSR_BITS = (
    _Bit(0, "HALTED", "止める要求・1 命令の実行で止まった"),
    _Bit(1, "BKPT", "ブレークポイント（BKPT 命令・FPB）で止まった"),
    _Bit(2, "DWTTRAP", "ウォッチポイント（DWT）で止まった"),
    _Bit(3, "VCATCH", "ベクタキャッチ（例外に入るところ）で止まった"),
    _Bit(4, "EXTERNAL", "外からの止める要求（EDBGRQ）で止まった"),
)


def _decode(value: int, bits) -> list[dict]:
    return [{"bit": b.name, "meaning": b.meaning, **({"hint": b.hint} if b.hint else {})}
            for b in bits if value >> b.position & 1]


def decode_cfsr(value: int) -> list[dict]:
    return _decode(value, _CFSR_BITS)


def decode_hfsr(value: int) -> list[dict]:
    return _decode(value, _HFSR_BITS)


def decode_dfsr(value: int) -> list[dict]:
    return _decode(value, _DFSR_BITS)


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
                          "hint": "積まれた PC の命令と、その時のレジスタから推測する"}]
    if ipsr:
        out["frame"] = _stacked_frame(target, symbols, entry_sp)
    return out


def _stacked_frame(target, symbols, entry_sp: int | None) -> dict | None:
    """例外に入ったときに積まれたフレーム（r0〜r3・r12・lr・pc・xpsr）。LR（EXC_RETURN）で MSP か PSP かを選ぶ。
    ハンドラが LR を書き換えた後で止まっていると読めない（None）。"""
    lr = target.read_core_register("lr")
    if lr >> 24 != 0xFF:
        return None
    use_psp = bool(lr & 0x4)
    if use_psp:
        sp = target.read_core_register("psp")             # ハンドラは MSP を使うので、PSP はフレームを指したまま
    else:
        sp = entry_sp if entry_sp is not None else target.read_core_register("msp")
    words = [target.read32(sp + 4 * i) for i in range(8)]
    names = ("r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr")
    regs = dict(zip(names, words))
    extended = not lr & 0x10                       # 浮動小数点の文脈も積まれている
    caller_sp = sp + (0x68 if extended else 0x20) + (4 if regs["xpsr"] >> 9 & 1 else 0)
    return {"stack": "PSP" if use_psp else "MSP", "sp": sp, "exc_return": lr, "fp_context": extended,
            "registers": regs, "caller_sp": caller_sp,
            "pc": _place(symbols, regs["pc"]), "lr": _place(symbols, regs["lr"] & ~1)}
