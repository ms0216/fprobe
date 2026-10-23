"""PC サンプリングの設定値の計算: DWT（POSTPRESET・CYCTAP・PCSAMPLENA・EXCTRCENA）・ITM・TPIU（SWO の速度）と、
SWO の帯域の見積もり（ITM の溢れ）。実機には書かない: 書く番地と値の一覧（`SamplingPlan.writes`）を返し、本流が書く。

根拠: ARMv7-M Architecture Reference Manual C1.8（DWT）・C1.7（ITM）・C1.10（TPIU）、Appendix D4（パケットの大きさ）。
仕様: docs/superpowers/specs/2026-10-17-f5-profile-design.md の 2 節・7 節。
"""

import math
from dataclasses import asdict, dataclass, field

from ..sampler import plan_slots, round_estimate, swd_hz_for_rate

__all__ = ["DEMCR", "DWT_CTRL", "DWT_PCSR", "ITM_LAR", "ITM_TCR", "ITM_TER", "PCSR_INVALID", "RegisterWrite",
           "SamplingPlan", "SwoPlan", "TPIU_ACPR", "TPIU_CSPSR", "TPIU_FFCR", "TPIU_SPPR", "exception_trace_writes",
           "itm_port_writes",
           "plan_itm_sampling", "plan_pcsr_sampling", "plan_swo"]

# --- レジスタ ---------------------------------------------------------------------------
DEMCR = 0xE000EDFC
DEMCR_TRCENA = 1 << 24
ITM_TCR = 0xE0000E80
ITM_TER = 0xE0000E00
ITM_LAR = 0xE0000FB0
ITM_UNLOCK = 0xC5ACCE55
DWT_CTRL = 0xE0001000
DWT_PCSR = 0xE000101C
TPIU_CSPSR = 0xE0040004
TPIU_ACPR = 0xE0040010
TPIU_SPPR = 0xE00400F0
TPIU_FFCR = 0xE0040304

PCSR_INVALID = 0xFFFFFFFF          # DWT_PCSR: コアが止まっている・サンプルできない

# DWT_CTRL のビット
_CYCCNTENA = 1 << 0
_POSTPRESET_SHIFT = 1              # [4:1]
_POSTINIT_SHIFT = 5                # [8:5]
_CYCTAP = 1 << 9
_SYNCTAP_SHIFT = 10                # [11:10]: 1 = CYCCNT のビット 24（同期パケットは 2^24 サイクルに 1 回。16 MHz で約 1 秒）
_PCSAMPLENA = 1 << 12
_EXCTRCENA = 1 << 16
_DWT_CTRL_WRITABLE = (1 << 23) - 1  # ビット 0〜22（上位は読み出し専用の NUMCOMP 等）
_SYNCTAP = 1                       # 解読は同期パケットを待つ（推定で再開しない）ので、落ちた後の欠けを短くする

# ITM_TCR のビット
_ITMENA, _TSENA, _SYNCENA, _DWTENA = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_TRACE_BUS_ID = 1 << 16

_ACPR_MAX = 0x1FFF
_TAPS = (64, 1024)                 # CYCTAP = 0 / 1 のときに POSTCNT を 1 減らす間隔（サイクル）

# SWO の UART 方式（NRZ）: 1 バイト = スタート + 8 + ストップ
_BITS_PER_BYTE = 10
_PC_PACKET = 5                     # ヘッダ + 4 バイト（眠りのときは 2 バイトだが、多い方で見積もる）
_EXC_PACKET = 3                    # ヘッダ + 2 バイト
_EXC_PACKETS_PER_EXCEPTION = 3     # 入る・出る・戻る


@dataclass(frozen=True)
class RegisterWrite:
    """1 回の書き込み。mask が全ビットでなければ、読んで mask の外を残して書く（読み・変え・書き）。"""
    address: int
    value: int
    mask: int = 0xFFFFFFFF
    note: str = ""


@dataclass
class SamplingPlan:
    method: str                                   # "itm"（SWO）/ "pcsr"（DAP サンプラで DWT_PCSR を読む）
    rate_hz: float                                # 実際のサンプル率
    writes: list[RegisterWrite]
    warnings: list[str] = field(default_factory=list)
    # itm
    interval_cycles: int | None = None
    cyctap: int | None = None
    postpreset: int | None = None
    acpr: int | None = None
    swo_baud: float | None = None                 # 実際の SWO の速度
    baud_error: float | None = None               # 求めた速度との差の割合
    bytes_per_s: float | None = None              # SWO に流れる見積もり
    utilization: float | None = None              # SWO の帯域に対する割合
    fits: bool = True                             # 帯域に収まる
    fitted: bool = False                          # 収めるためにサンプルの間隔を延ばした
    # pcsr
    sampler_ranges: list[tuple[int, int]] | None = None
    period_s: float | None = None
    requested_rate_hz: float | None = None        # 求めたサンプル率（rate_hz は SWD の速さで出せる見込み）
    swd: dict | None = None                       # 1 巡回の見積もり（fprobe.sampler.round_estimate と swd_hz_for_rate）

    def as_dict(self) -> dict:
        d = asdict(self)
        d["writes"] = [asdict(w) for w in self.writes]
        return d


def _intervals() -> list[tuple[int, int, int]]:
    """(間隔のサイクル, CYCTAP, POSTPRESET) を短い順に（同じ間隔なら先のもの）。"""
    seen: dict[int, tuple[int, int, int]] = {}
    for cyctap, tap in enumerate(_TAPS):
        for post in range(16):
            seen.setdefault((post + 1) * tap, ((post + 1) * tap, cyctap, post))
    return sorted(seen.values())


def _timestamp_bytes(delta: int) -> int:
    """ローカルのタイムスタンプのパケットの大きさ（差分 delta クロック）。"""
    if delta <= 6:
        return 1
    return 1 + min(4, math.ceil(delta.bit_length() / 7))


def _bandwidth(rate_hz: float, interval: int, exception_rate_hz: float, timestamps: bool) -> float:
    total = _PC_PACKET * rate_hz + _EXC_PACKET * _EXC_PACKETS_PER_EXCEPTION * exception_rate_hz
    if timestamps:
        total += _timestamp_bytes(interval) * (rate_hz + _EXC_PACKETS_PER_EXCEPTION * exception_rate_hz)
    return total


def plan_itm_sampling(core_clock_hz: float, rate_hz: float, swo_baud: float, *, traceclk_hz: float | None = None,
                      exception_trace: bool = False, exception_rate_hz: float = 0, timestamps: bool = False,
                      headroom: float = 1.0) -> SamplingPlan:
    """SWO で PC サンプルを受けるための設定。サンプル率は目標より速くしない。見積もった使用率が headroom（既定 1.0 =
    帯域の上限。余裕は使う側が明示する）を超えるなら間隔を延ばす。
    exception_rate_hz は例外トレースの見積もりに使う 1 秒あたりの例外の回数。"""
    if core_clock_hz <= 0 or rate_hz <= 0 or swo_baud <= 0:
        raise ValueError("クロック・サンプル率・SWO の速度は正")
    warnings: list[str] = []
    candidates = _intervals()
    want = core_clock_hz / rate_hz
    index = next((k for k, (cycles, _, _) in enumerate(candidates) if cycles >= want), None)
    if index is None:
        index = len(candidates) - 1
        warnings.append(f"サンプル率 {rate_hz:g} Hz は遅すぎる: 最も長い間隔 {candidates[-1][0]} サイクルにした")
    elif index == 0 and want < candidates[0][0]:
        warnings.append(f"サンプル率 {rate_hz:g} Hz は速すぎる: 最も短い間隔 {candidates[0][0]} サイクルにした")

    swo = plan_swo(traceclk_hz or core_clock_hz, swo_baud)
    warnings += swo.warnings
    acpr, baud, error = swo.acpr, swo.baud, swo.baud_error

    exc_rate = exception_rate_hz if exception_trace else 0

    def usage(k: int) -> tuple[float, float]:
        cycles = candidates[k][0]
        per_s = _bandwidth(core_clock_hz / cycles, cycles, exc_rate, timestamps)
        return per_s, per_s * _BITS_PER_BYTE / baud

    fitted = False
    while usage(index)[1] > headroom and index + 1 < len(candidates):
        index += 1
        fitted = True
    bytes_per_s, utilization = usage(index)
    fits = utilization <= headroom
    cycles, cyctap, post = candidates[index]
    if fitted:
        warnings.append(f"SWO の帯域に収めるため、サンプルの間隔を {cycles} サイクルに延ばした")
    if not fits:
        warnings.append(f"SWO の帯域が足りない（使用率 {utilization:.0%}）: 溢れてサンプルが欠ける。SWO を速くするか、"
                        "例外トレース・タイムスタンプを切る")

    ctrl = (_CYCCNTENA | post << _POSTPRESET_SHIFT | post << _POSTINIT_SHIFT | (_CYCTAP if cyctap else 0)
            | _SYNCTAP << _SYNCTAP_SHIFT | (_EXCTRCENA if exception_trace else 0))
    tcr = _ITMENA | _SYNCENA | _DWTENA | _TRACE_BUS_ID | (_TSENA if timestamps else 0)
    writes = [
        RegisterWrite(DEMCR, DEMCR_TRCENA, DEMCR_TRCENA, "DEMCR.TRCENA: DWT・ITM を使う"),
        RegisterWrite(ITM_LAR, ITM_UNLOCK, note="ITM の鍵を外す"),
        *swo.writes,
        RegisterWrite(ITM_TCR, tcr, note="ITM: 有効・同期・DWT のパケットを流す" + ("・タイムスタンプ" if timestamps else "")),
        RegisterWrite(DWT_CTRL, ctrl, _DWT_CTRL_WRITABLE, "DWT: CYCCNT・POSTPRESET・CYCTAP・SYNCTAP（PCSAMPLENA はまだ）"),
        RegisterWrite(DWT_CTRL, ctrl | _PCSAMPLENA, _DWT_CTRL_WRITABLE, "DWT: PC サンプリングを始める"),
    ]
    return SamplingPlan("itm", core_clock_hz / cycles, writes, warnings, interval_cycles=cycles, cyctap=cyctap,
                        postpreset=post, acpr=acpr, swo_baud=baud, baud_error=error, bytes_per_s=bytes_per_s,
                        utilization=utilization, fits=fits, fitted=fitted)


@dataclass(frozen=True)
class SwoPlan:
    """TPIU を SWO（UART 方式・幅 1・フォーマッタ無し）にする設定。baud は実際の速度（受信側もこれにする）。"""
    acpr: int
    baud: float
    baud_error: float                             # 求めた速度との差の割合
    writes: tuple[RegisterWrite, ...]
    warnings: tuple[str, ...] = ()


def plan_swo(traceclk_hz: float, swo_baud: float) -> SwoPlan:
    """TPIU の SWO の設定（ACPR = round(TRACECLKIN / 速度) - 1）。チップ固有のピンの設定（STM32 の DBGMCU_CR の
    TRACE_IOEN 等）は含まない。"""
    if traceclk_hz <= 0 or swo_baud <= 0:
        raise ValueError("TRACECLKIN と SWO の速度は正")
    warnings: list[str] = []
    acpr = round(traceclk_hz / swo_baud) - 1
    if not 0 <= acpr <= _ACPR_MAX:
        acpr = min(max(acpr, 0), _ACPR_MAX)
        warnings.append(f"SWO の速度 {swo_baud:g} は TRACECLKIN {traceclk_hz:g} Hz から作れない（ACPR を {acpr} にした）")
    baud = traceclk_hz / (acpr + 1)
    error = abs(baud - swo_baud) / swo_baud
    if error > 0:                       # 閾値で隠さない: 違えば実際の速度を書く
        warnings.append(f"SWO の速度は {baud:g}（求めた {swo_baud:g} と {error:.2%} 違う）。受信側も {baud:g} にする")
    writes = (
        RegisterWrite(TPIU_CSPSR, 1, note="TPIU: ポートの幅 1"),
        RegisterWrite(TPIU_ACPR, acpr, note=f"TPIU: SWO の分周（{baud:g} baud）"),
        RegisterWrite(TPIU_SPPR, 2, note="TPIU: SWO は NRZ（UART 方式）"),
        RegisterWrite(TPIU_FFCR, 0x100, note="TPIU: フォーマッタを切る"),
    )
    return SwoPlan(acpr, baud, error, writes, tuple(warnings))


def itm_port_writes(port: int) -> list[RegisterWrite]:
    """ファームがスティミュラス・ポート port に書いた語を SWO に出す設定（ITM・TPIU 以外は変えない）。
    TCR は ITMENA・SYNCENA だけ、TER は port のビットだけを立てる（読み・変え・書き）。"""
    if not 0 <= port < 32:
        raise ValueError(f"ITM のポートは 0〜31: {port}")
    return [RegisterWrite(DEMCR, DEMCR_TRCENA, DEMCR_TRCENA, "DEMCR.TRCENA: ITM を使う"),
            RegisterWrite(ITM_LAR, ITM_UNLOCK, note="ITM の鍵を外す"),
            RegisterWrite(ITM_TCR, _ITMENA | _SYNCENA, _ITMENA | _SYNCENA, "ITM: 有効・同期"),
            _sync_packets(),
            RegisterWrite(ITM_TER, 1 << port, 1 << port, f"ITM: ポート {port} を有効に")]   # TER は最後（呼び出し側が分ける）


_SYNC_BITS = _CYCCNTENA | _SYNCTAP << _SYNCTAP_SHIFT       # CYCCNT を回し、SYNCTAP でビット 24 ごとに同期パケット
_SYNC_MASK = _CYCCNTENA | 3 << _SYNCTAP_SHIFT


def _sync_packets() -> RegisterWrite:
    """DWT の同期パケット。ITM の解読は既定で同期パケットを待つ（推定で再開しない）ので、これが無いと 1 バイト化けた
    後に二度と解けない。DWT_CTRL のほかのビットは変えない。"""
    return RegisterWrite(DWT_CTRL, _SYNC_BITS, _SYNC_MASK, "DWT: CYCCNT・同期パケット（SYNCTAP）")


def exception_trace_writes() -> list[RegisterWrite]:
    """例外トレース（例外に入る・出る・戻る）を SWO に流す設定。PC サンプリング等ほかの DWT の機能・ITM のポートは
    変えない（読み・変え・書き）。止めるときは DWT_CTRL を元に戻す（write_registers が返す前の値）。"""
    return [RegisterWrite(DEMCR, DEMCR_TRCENA, DEMCR_TRCENA, "DEMCR.TRCENA: DWT・ITM を使う"),
            RegisterWrite(ITM_LAR, ITM_UNLOCK, note="ITM の鍵を外す"),
            RegisterWrite(ITM_TCR, _ITMENA | _SYNCENA | _DWTENA, _ITMENA | _SYNCENA | _DWTENA,
                          "ITM: 有効・同期・DWT のパケットを流す"),
            RegisterWrite(DWT_CTRL, _EXCTRCENA | _SYNC_BITS, _EXCTRCENA | _SYNC_MASK,
                          "DWT: 例外トレース（EXCTRCENA）・CYCCNT・同期パケット（SYNCTAP）")]


def plan_pcsr_sampling(rate_hz: float, sys_clock_hz: int | None = None, swd_hz: float | None = None) -> SamplingPlan:
    """DAP サンプラで DWT_PCSR を周期で読む設定（SWO を使わない。ITM の無い Cortex-M0+ でも使える）。
    サンプラのか所と周期は fprobe.sampler.Sampler.configure にそのまま渡す。
    sys_clock_hz（FPGA）と swd_hz（つないだ SWD の速さの要求）があれば、1 巡回の時間を見積もり（swd）、rate_hz を
    出せる見込みの速さにし、足りなければ要る SWD の速さを警告に書く（B9。B17 の後は SWD 4 MHz で 1 巡回 3 転送 約 37 µs）。"""
    if rate_hz <= 0:
        raise ValueError("サンプル率は正")
    writes = [RegisterWrite(DEMCR, DEMCR_TRCENA, DEMCR_TRCENA, "DEMCR.TRCENA: DWT を使う")]
    plan = SamplingPlan("pcsr", rate_hz, writes, sampler_ranges=[(DWT_PCSR, 4)], period_s=1 / rate_hz,
                        requested_rate_hz=rate_hz)
    if sys_clock_hz and swd_hz:
        slots = plan_slots(plan.sampler_ranges)
        estimate = round_estimate(slots, sys_clock_hz, swd_hz)
        need = swd_hz_for_rate(slots, rate_hz, sys_clock_hz)
        plan.swd = {**estimate, "swd_hz_for_rate": need}
        if estimate["max_rate_hz"] < rate_hz:
            plan.rate_hz = estimate["max_rate_hz"]
            fix = (f"SWD を {need / 1e6:.2f} MHz 以上にする（target_connect の frequency_hz）か rate_hz を下げる"
                   if need is not None else "SWD の最も速い設定でも届かない: rate_hz を下げる")
            plan.warnings.append(
                f"サンプル率 {rate_hz:g} Hz は SWD {estimate['swclk_hz'] / 1e6:.3f} MHz では出ない（1 巡回 約 "
                f"{estimate['round_us']:.0f} µs = 約 {estimate['max_rate_hz']:.0f} Hz。実機は調停の分さらに遅い）: {fix}")
    return plan
