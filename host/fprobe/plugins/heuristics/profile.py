"""プラグイン heuristics: プロファイル・サンプリングの見方（信頼区間・噛み合い・クロックの誤差・典型のクロック）。"""

import math
import re
import statistics

from collections.abc import Mapping, Sequence

from .. import ApiCaller
from ._common import _tag

# 品種名（pyOCD のターゲット名）→ ファームがふつう使うコアクロック（MHz）。リセット直後の値ではない（058d574 より前の
# host/ui/src/lib/target.ts の TYPICAL）
TYPICAL_CORE_CLOCKS: tuple[tuple[str, float], ...] = (
    (r"^nrf51", 16),
    (r"^nrf52|^nrf53|^nrf91", 64),
    (r"^rp2040", 125),
    (r"^rp2350", 150),
    (r"^stm32f0", 48),
    (r"^stm32f1", 72),
    (r"^stm32f3", 72),
    (r"^stm32f401", 84),
    (r"^stm32f41[01]", 100),
    (r"^stm32f4[01][57]", 168),
    (r"^stm32f4[2-7]", 180),
    (r"^stm32f7", 216),
    (r"^stm32g0", 64),
    (r"^stm32g4", 170),
    (r"^stm32h7", 480),
    (r"^stm32l0", 32),
    (r"^stm32l4", 80),
    (r"^stm32wb", 64),
)


def typical_core_clock(part_name: str | None) -> dict | None:
    """品種名から、ファームがふつう設定するコアクロック（Hz）。表に無ければ None。

    経験則: 品種ごとに「よくある最大の設定」を 1 つの値とみなす。外れる場合: ファームが別のクロックに設定した
    （低消費電力・USB のための 48 MHz 等）、リセット直後・初期化の前（内蔵の発振器のまま）。ターゲットの
    SystemCoreClock（debug.variable.read）が読めるならそちらが事実。"""
    if not part_name:
        return None
    for pattern, mhz in TYPICAL_CORE_CLOCKS:
        if re.search(pattern, part_name, re.IGNORECASE):
            return _tag("typical_core_clock", "品種のファームがふつう設定するクロック（実際の設定ではない）",
                        part=part_name, pattern=pattern, hz=mhz * 1e6)
    return None


def aliasing_suspects(result: Mapping, min_expected: float = 4.6, top: int | None = None,
                      confidence: float = 0.95) -> dict:
    """profile.result（method=itm・by="exception"）の結果から、PC サンプルと例外トレースの負荷が「噛み合い（エイリアシング）で
    ずれている」と見る例外を挙げる（4e2138e より前の本体の判定）。本体はサンプルの回数と例外トレースの負荷だけを返す
    （区間の外かの比べ方は load_outside_interval）。

    経験則:
    - サンプル 0 回でも、期待回数 load × total が min_expected（4.6 = 0 回になる確率が約 1 %）以上なら噛み合いと見る。
    - 1 回以上なら信頼区間に余裕 2/√total × √load を足し、その外なら噛み合いと見る。余裕の幅は経験則。
    外れる場合: 例外の負荷が測る間に本当に変わった（記録の窓とサンプルの窓が違う）とき、ずれは噛み合いではない。
    rows は上位 top 件で切られる。top（求めた件数）を渡し、rows がその件数に達していれば、rows に無い例外は 0 回と
    みなさず unknown に入れる（aliasing_suspects_from_api は全件を取る）。区間は wilson_interval（信頼度 confidence。
    各サンプルが独立な二項の試行という仮定の上の推定）。"""
    if result.get("by") != "exception":
        raise ValueError('profile.result を by="exception" で取った結果を渡す（ほかの表では例外ごとの回数が無い）')
    total = result["total"]
    rows = result.get("rows", [])
    hits = {r["exception"]: r["samples"] for r in rows}
    cut = top is not None and len(rows) >= top
    suspects, unknown = [], []
    for e in result.get("exceptions", []):
        load, n = e["load"], hits.get(e["exception"], 0)
        if e["exception"] not in hits and cut:
            unknown.append(e["exception"])
            continue
        if n == 0:
            if load * total >= min_expected:
                suspects.append({"exception": e["exception"], "name": e.get("name"), "load": load, "samples": 0,
                                 "reason": "zero_samples"})
            continue
        low, high = wilson_interval(n, total, confidence)
        slack = 2 / math.sqrt(total) * math.sqrt(max(load, 1e-9))
        if load < low - slack or load > high + slack:
            suspects.append({"exception": e["exception"], "name": e.get("name"), "load": load, "samples": n,
                             "reason": "outside_interval"})
    return _tag("aliasing", "サンプルの割合と例外トレースの負荷のずれは、サンプルの間隔と割り込みの周期の噛み合いによる。"
                "負荷は例外トレースの値を使い、rate_hz を少し変えると偏りが変わる",
                suspects=suspects, unknown=unknown, min_expected=min_expected, confidence=confidence)


def aliasing_suspects_from_api(caller: ApiCaller, min_expected: float = 4.6, top: int = 1000) -> dict:
    """profile.result(by="exception", top=top) を取り、aliasing_suspects に渡す。"""
    return aliasing_suspects(caller.call("profile.result", by="exception", top=top), min_expected, top)


def sampler_falling_behind(result: Mapping, ratio: float = 0.9) -> dict:
    """profile（method=pcsr）の sampling で、実際の速さが求めた速さの ratio 未満なら「サンプラが追いつかない」と見る
    （7f98b11 より前の本体の閾値 9 割）。本体は求めたより遅いという事実を常に書く。"""
    s = result["sampling"]
    achieved = s.get("achieved_hz")
    behind = None if achieved is None else achieved < ratio * s["requested_hz"]
    return _tag("sampler_ratio", f"求めた速さの {ratio:.0%} 未満は追いつかない、それ以上は時刻のばらつきの内",
                falling_behind=behind, ratio=ratio, achieved_hz=achieved, requested_hz=s["requested_hz"])


SWO_HEADROOM = 0.75                    # 7f98b11 より前の plan_itm_sampling の既定（profile の headroom に渡す）


def counter_overflow_risk(counters: int, core_clock_hz: float, swo_baud: float, share: float = 0.5) -> dict:
    """DWT のイベントカウンタの溢れのパケットが最悪で SWO の帯域の share を超えるなら「忙しい区間で溢れて数え落とす」と
    見る（7f98b11 より前の本体の閾値 5 割）。最悪 = 個数 × クロック / 256 × 2 バイト、帯域 = baud / 10。"""
    worst = counters * core_clock_hz / 256 * 2
    used = worst / (swo_baud / 10)
    return _tag("counter_overflow_share", f"最悪の量が帯域の {share:.0%} を超えると、ほかのパケットと重なる区間で溢れる",
                worst_bytes_per_s=worst, share=used, risky=used > share)


_ASSUME_BINOMIAL = ("各サンプルが独立な二項の試行（ランダムな時刻の抽出）。PC サンプリングは周期の抽出なので、周期と"
                    "同期した処理（割り込み等）ではこの仮定が外れ、区間は偏りを含まない")


def _z(confidence: float) -> float:
    if not 0 < confidence < 1:
        raise ValueError(f"信頼度は 0 と 1 の間: {confidence}")
    return statistics.NormalDist().inv_cdf(0.5 + confidence / 2)


def wilson_interval(hits: int, total: int, confidence: float = 0.95) -> tuple[float, float]:
    """二項の割合 hits / total の Wilson の区間（2026-10-24 より前の本体の profile.stats.wilson_interval）。
    サンプルが少なくても、割合が 0 や 1 でも [0, 1] の外へ出ない。total が 0 なら何も分からない: (0, 1)。
    仮定: 各サンプルが独立な二項の試行（_ASSUME_BINOMIAL）。信頼度 0.95 は元の本体の既定。"""
    if not 0 <= hits <= total:
        raise ValueError(f"数が合わない: {hits} / {total}")
    if total == 0:
        return 0.0, 1.0
    z = _z(confidence)
    p = hits / total
    z2n = z * z / total
    center = (p + z2n / 2) / (1 + z2n)
    half = z / (1 + z2n) * math.sqrt(p * (1 - p) / total + z2n / (4 * total))
    low = 0.0 if hits == 0 else max(0.0, center - half)                # 端は丸めの誤差を残さない
    high = 1.0 if hits == total else min(1.0, center + half)
    return low, high


def _with_ci(row: Mapping, total: int, confidence: float) -> dict:
    n, fraction = row["samples"], row.get("fraction")
    denominator = round(n / fraction) if n and fraction else total    # 例外の表の分母は例外の分かったサンプル
    return {**row, "ci": list(wilson_interval(n, denominator, confidence))}


def sampling_intervals(result: Mapping, confidence: float = 0.95) -> dict:
    """profile.result（または profile.group）の各行と categories に、割合の Wilson の信頼区間 ci = [下, 上] を付ける
    （2026-10-24 より前の本体が付けていた ci。本体は回数 samples と割合 fraction だけを返す）。

    仮定: 各サンプルが独立な二項の試行。外れる場合: PC サンプリングは周期の抽出で、サンプルの周期と同期した処理
    （SysTick 等）の割合は区間の外に大きく偏りうる（2026-10-18 の実機で SysTick の負荷 0.1 % が 0 % や 0.8 % に）。
    区間はサンプル数による誤差の目安でしかない。元の result は変えない。"""
    total = result["total"]
    out = {k: v for k, v in result.items() if k not in ("rows", "categories")}
    out["rows"] = [_with_ci(r, total, confidence) for r in result.get("rows", [])]
    if "categories" in result:
        out["categories"] = {k: _with_ci(v, total, confidence) for k, v in result["categories"].items()}
    return _tag("wilson_binomial", _ASSUME_BINOMIAL, confidence=confidence, **out)


def load_outside_interval(result: Mapping, confidence: float = 0.95) -> dict:
    """profile.result（method=itm・by="exception"）の結果で、例外トレースで測った負荷 load が、PC サンプルの割合
    （その例外のサンプル数 / サンプルの総数 total）の Wilson の区間の外にある例外を挙げる（2026-10-24 より前の本体の
    profile.aliasing.aliasing_notes の判定）。0 回のときも同じ区間で比べる。

    仮定: 区間は各サンプルが独立な二項の試行とした推定（wilson_interval）。外れる場合: 周期の抽出の偏り（噛み合い）・
    記録の窓とサンプルの窓の違いで外に出る。区間の外にある原因はここでも断定しない（aliasing_suspects を参照）。
    rows が top で切られていると、rows に無い例外を 0 回として比べてしまう（全件を取って渡す）。"""
    if result.get("by") != "exception":
        raise ValueError('profile.result を by="exception" で取った結果を渡す（ほかの表では例外ごとの回数が無い）')
    total = result["total"]
    hits = {r["exception"]: r["samples"] for r in result.get("rows", [])}
    outside = []
    for e in result.get("exceptions", []):
        n = hits.get(e["exception"], 0)
        low, high = wilson_interval(n, total, confidence)
        if not low <= e["load"] <= high:
            outside.append({"exception": e["exception"], "name": e.get("name"), "load": e["load"], "samples": n,
                            "total": total, "ci": [low, high]})
    return _tag("wilson_binomial", _ASSUME_BINOMIAL, confidence=confidence, outside=outside)


def clock_stderr(samples: Sequence[tuple[int, int]], timebase_hz: float) -> dict:
    """(タイムベースの時刻, CYCCNT) の列の最小二乗の傾き（CPU クロック）と、その標準誤差・ppm（2026-10-24 より前の
    本体の profile.cycles.estimate_clock が返した stderr_hz・ppm）。本体の cycles.clock は傾きと残差の事実だけを返す。

    仮定: 直線からの残差が独立・等分散（読みの時刻の揺れが互いに無関係）。外れる場合: サンプラの読みが USB・DAP の
    周期と同期して揺れる、温度で水晶がゆっくり動く（残差が相関する）と、標準誤差は実際の誤差より小さく出る。"""
    from fprobe.profile.cycles import estimate_clock, unwrap
    est = estimate_clock(list(samples), timebase_hz)
    ordered = sorted(samples)
    xs = [(t - ordered[0][0]) / timebase_hz for t, _ in ordered]
    ys = unwrap([c for _, c in ordered])
    mx = statistics.fmean(xs)
    sxx = sum((x - mx) ** 2 for x in xs)
    intercept = statistics.fmean(ys) - est.clock_hz * mx
    sse = sum((y - intercept - est.clock_hz * x) ** 2 for x, y in zip(xs, ys))
    n = len(xs)
    stderr = (sse / (n - 2) / sxx) ** 0.5
    return _tag("least_squares_iid", "直線からの残差が独立・等分散（読みの時刻の揺れが互いに無関係）",
                clock_hz=est.clock_hz, stderr_hz=stderr, ppm=stderr / est.clock_hz * 1e6 if est.clock_hz else None,
                samples=n)
