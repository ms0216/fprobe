"""プラグイン heuristics: 記録の時間の並びの見方（correlate の lift・外れ値）。"""

from collections.abc import Mapping, Sequence

from ._common import _tag

def rank_causes(result: Mapping, min_preceded: float = 0.5) -> dict:
    """recording.correlate の結果の各候補に lift = preceded / coverage を足し、「先行率 min_preceded 以上を先に、
    その中で lift の大きい順」に並べ直す（b9f950c より前の本体の並べ方）。

    経験則:
    - coverage（記録のうち候補の後 window_s 以内に当たる時間の割合）を「結果がランダムな時刻に起きたとき、偶然に
      候補が先行する見込み」とみなし、lift = 先行率 ÷ その見込み を「偶然より何倍よく先行するか」と読む。
      外れる場合: 結果が周期的・候補と同じ源から起きる（ランダムな時刻でない）とき、lift は偶然比にならない。
    - 先行率 0.5（結果の大半）未満の候補は後ろに回す。まれにしか先行しない候補は coverage が小さく lift が大きく
      見えやすい（実機: 先行率 0.01・lift 2.9 が、先行率 1.0・lift 2.6 の本当の原因より上に来た）。0.5 は経験則で、
      結果の原因が複数あって各々が半分未満しか先行しないときは本当の原因も後ろに回る。
    元の result は変えない（候補の dict を写して lift を足す）。"""
    causes = [dict(c) for c in result.get("causes", [])]
    for c in causes:
        p, cov = c.get("preceded"), c.get("coverage")
        c["lift"] = p / cov if p is not None and cov else None
    ranked = sorted(causes, key=lambda c: ((c["preceded"] or 0) >= min_preceded,
                                           -1 if c["lift"] is None else c["lift"]), reverse=True)
    return _tag("correlate_lift",
                f"結果はランダムな時刻に起きる（coverage = 偶然の見込み）。先行率 {min_preceded} 以上を先に、lift の順",
                min_preceded=min_preceded, effect=result.get("effect"), window_s=result.get("window_s"),
                causes=ranked)


def outliers(durations: Sequence[float], k: float = 1.5, sigmas: float = 3.0) -> dict:
    """時間の並びの外れ値（1947d85 より前の profile/stats.py の outliers）。

    経験則:
    - iqr: Tukey の柵（第 1・第 3 四分位から四分位範囲の k 倍（既定 1.5）の外）。点数 = 柵からの距離 / 四分位範囲
      （四分位範囲が 0 なら値の単位 1 で割る）。
    - zscore: 平均から sigmas σ（既定 3）の外。点数 = |z|。正規分布を仮定した目安。
    外れる場合: 分布が二峰（2 つの道筋がある処理）・裾が重い（キャッシュ・割り込みの入る処理）と、普通の値を外れ値に
    したり本当の異常を柵の内に入れたりする。件数が少ないと四分位・σ が定まらない。
    返す: iqr / zscore それぞれ [{"index", "value", "score"}]（点数の大きい順）。"""
    from ...profile.stats import Distribution
    d = Distribution(list(durations))
    out_iqr: list[tuple[int, float]] = []
    out_z: list[tuple[int, float]] = []
    if d.count >= 2:
        q1, q3 = d.quantile(0.25), d.quantile(0.75)
        iqr = q3 - q1
        low, high = q1 - k * iqr, q3 + k * iqr
        scale = iqr if iqr > 0 else 1.0
        for i, v in enumerate(d.values):
            if v > high:
                out_iqr.append((i, (v - high) / scale))
            elif v < low:
                out_iqr.append((i, (low - v) / scale))
        mean, sd = d.mean, d.stdev
        if sd != 0:
            for i, v in enumerate(d.values):
                z = abs(v - mean) / sd
                if z > sigmas:
                    out_z.append((i, z))

    def rows(found):
        return [{"index": i, "value": d.values[i], "score": s} for i, s in sorted(found, key=lambda x: -x[1])]

    return _tag("tukey_iqr_and_zscore",
                f"Tukey の柵（Q1 − {k} IQR・Q3 + {k} IQR の外）と、平均から {sigmas} σ の外を外れ値とみなす",
                k=k, sigmas=sigmas, count=d.count, iqr=rows(out_iqr), zscore=rows(out_z))


def outliers_of_intervals(result: Mapping, k: float = 1.5, sigmas: float = 3.0) -> dict:
    """recording.intervals の結果（intervals = [(始めの時刻, 時間)]。タイムベース）に outliers を当て、外れ値に
    始めの時刻（t）を添える。経験則・外れる場合は outliers と同じ。recording.intervals は先頭 1000 件しか返さない
    ので、それより多いときは先頭 1000 件の中での外れ値。"""
    pairs = [tuple(p) for p in result.get("intervals", [])]
    out = outliers([d for _, d in pairs], k, sigmas)
    for key in ("iqr", "zscore"):
        for row in out[key]:
            row["t"] = pairs[row["index"]][0]
    out["truncated"] = result.get("count", len(pairs)) > len(pairs)
    return out
