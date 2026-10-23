"""プラグイン: 本体から外した経験則（ヒューリスティック）の「見方」を、本体の API の結果（測った事実）の上に載せる。

本体（fprobe）は測定器で、測った事実だけを返す（CLAUDE.md「fprobe 本体にヒューリスティックを入れない」）。ここには
本体から外した経験則の閾値・仮定に頼る推定・判定・並べ替えを、取り外せる層として置く（利用者の承認 2026-10-22）。

使い方（仕組みは fprobe.plugins の説明を参照。本体からは import しない）:

    from fprobe.plugins import ProbeCaller
    from fprobe.plugins import heuristics as h
    ranked = h.rank_causes(client.call("recording.correlate", effect="errors changed", causes=None, window_s=0.01))
    h.decode_uart_with_estimated_baud(client, channel=5)

約束:
- 結果には必ず "heuristic"（経験則の名前）と "assumption"（仮定の短い説明）を入れる。使う人と AI が、その判断の根拠が
  測った事実ではなく経験則だと分かるように。各関数の docstring に、どの閾値・仮定か、外れる場合を書く。
- 閾値は元の本体と同じ値を既定にし、引数で変えられる。
- API を呼ぶ補助は、公開 API（host/api/fprobe_api.json）の名前と引数だけを `call(method, **params)` で呼ぶ。
  本体のライブラリ（データ型・逆アセンブラ等）は import して使うが、本体の API は変えない。
"""

import math
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import ApiCaller

__all__ = [
    "GUARD_BYTES", "PAYLOAD_CYCLES", "RECORD_CYCLES", "STANDARD_BAUDS", "TYPICAL_CORE_CLOCKS", "WORD_CYCLES",
    "decode_uart_with_estimated_baud", "diagnose_hints", "disasm_before_pc", "estimate_baud", "fault_hints",
    "heap_growing", "heap_growing_from_api", "infer_jobs", "infer_jobs_from_api", "outliers", "outliers_of_intervals",
    "rank_causes", "stack_warning", "trace_cpu_share", "typical_core_clock",
    "SWO_HEADROOM", "aliasing_suspects", "aliasing_suspects_from_api", "counter_overflow_risk", "heap_overflow_suspects", "jtag_guess_ir",
    "link_cause_hints", "sampler_falling_behind", "wait_failed_as_timeout",
]


def _tag(name: str, assumption: str, **out: Any) -> dict:
    return {"heuristic": name, "assumption": assumption, **out}


# --- 1. correlate の lift と並べ方 ------------------------------------------------------------------

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


# --- 2. 外れ値（Tukey の柵・3σ） --------------------------------------------------------------------

def outliers(durations: Sequence[float], k: float = 1.5, sigmas: float = 3.0) -> dict:
    """時間の並びの外れ値（1947d85 より前の profile/stats.py の outliers）。

    経験則:
    - iqr: Tukey の柵（第 1・第 3 四分位から四分位範囲の k 倍（既定 1.5）の外）。点数 = 柵からの距離 / 四分位範囲
      （四分位範囲が 0 なら値の単位 1 で割る）。
    - zscore: 平均から sigmas σ（既定 3）の外。点数 = |z|。正規分布を仮定した目安。
    外れる場合: 分布が二峰（2 つの道筋がある処理）・裾が重い（キャッシュ・割り込みの入る処理）と、普通の値を外れ値に
    したり本当の異常を柵の内に入れたりする。件数が少ないと四分位・σ が定まらない。
    返す: iqr / zscore それぞれ [{"index", "value", "score"}]（点数の大きい順）。"""
    from ..profile.stats import Distribution
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


# --- 3. UART のボーの推定 ------------------------------------------------------------------------

STANDARD_BAUDS = (300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 76800, 115200, 128000,
                  230400, 250000, 256000, 460800, 500000, 576000, 921600, 1_000_000, 1_152_000, 1_500_000,
                  2_000_000, 2_500_000, 3_000_000, 3_500_000, 4_000_000, 6_000_000, 8_000_000, 10_000_000, 12_000_000)


def estimate_baud(transitions, channel: int, *, snap: float | None = 0.03, noise: float = 0.01,
                  tolerance: float = 0.2, max_bits: int = 10,
                  standard: Sequence[float] = STANDARD_BAUDS) -> dict:
    """同じ値が続いた長さ（fprobe.la.Transitions の 1 チャンネル）から 1 ビットの長さを推定する（73d5717 より前の
    decode/uart.py の estimate_baud）。

    経験則:
    - 短い順に並べた区間のうち、下位 noise（既定 1 %。少なくとも 2 個目）より短いものは雑音とみなす。
    - その長さの 0.5〜1.5 倍の区間を 1 ビットとみなして平均し、各区間を整数ビット（1〜max_bits）に丸めて ±tolerance
      （既定 0.2 ビット）に入るものだけをビット数の合計で割る（停止ビット 1.5・フレームの間の休止を除く）。
    - snap（既定 3 %）以内に標準のボーレートがあれば、その値に寄せる（None で寄せない）。
    外れる場合: 1 ビットの並びが 1 つも無いデータ（例: 2 ビットずつしか変わらない）では倍の長さを 1 ビットと見る。
    雑音が 1 % より多い・グリッチがある・別のボーの通信が混ざると外れる。標準値から 3 % 以内の非標準のボーは標準値に
    寄ってしまう。
    返す: baud（寄せた後。推定できなければ None）・raw_baud（寄せる前）・snapped。"""
    from ..decode.core import ChannelLevels
    edges = ChannelLevels(transitions, channel).edges
    runs = sorted(edges[i + 1] - edges[i] for i in range(len(edges) - 1))   # 両端の切れた区間は使わない
    assumption = (f"下位 {noise:.0%} の区間は雑音、最短の 0.5〜1.5 倍が 1 ビット、整数ビットから ±{tolerance} の区間"
                  f"だけを使う" + ("" if snap is None else f"、標準値から {snap:.0%} 以内なら標準値に寄せる"))
    none = _tag("uart_baud_estimate", assumption, channel=channel, baud=None, raw_baud=None, snapped=False)
    if len(runs) < 2:
        return none
    k = max(2, math.ceil(noise * len(runs)))
    if len(runs) < k:
        return none
    rough = runs[k - 1]
    ones = [r for r in runs if 0.5 * rough <= r <= 1.5 * rough]
    unit = sum(ones) / len(ones)
    total, bits = 0, 0
    for r in runs:
        n = round(r / unit)
        if 1 <= n <= max_bits and abs(r / unit - n) <= tolerance:
            total += r
            bits += n
    raw = bits / (total * transitions.sample_period_s)
    baud, snapped = raw, False
    if snap is not None:
        nearest = min(standard, key=lambda b: abs(b - raw))
        if abs(nearest - raw) <= snap * nearest:
            baud, snapped = nearest, True
    return {**none, "baud": baud, "raw_baud": raw, "snapped": snapped}


def decode_uart_with_estimated_baud(caller: ApiCaller, channel: int, *, snap: float | None = 0.03,
                                    work_dir: str | Path | None = None, **settings: Any) -> dict:
    """最後の捕捉（la.capture 等）の channel からボーを estimate_baud で推定し、その baud で la.decode（uart）を呼ぶ。

    最後の捕捉の波形は la.export（format sr）でファイルに書いて fprobe.sigrok.read_sr で読む（デーモンと同じ計算機の
    ファイルを読む。work_dir を省くと一時ディレクトリ）。settings は la.decode の uart の設定（data_bits・parity・
    stop_bits・invert）と la.decode の引数（start・end・kinds・errors_only・offset・limit）。
    経験則・外れる場合は estimate_baud と同じ。推定できなければ la.decode を呼ばず decoding = None。"""
    from ..sigrok import read_sr
    decode_keys = {"start", "end", "kinds", "errors_only", "offset", "limit", "capture_id"}
    uart = {k: v for k, v in settings.items() if k not in decode_keys}
    extra = {k: v for k, v in settings.items() if k in decode_keys}
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        path = Path(tmp) / "capture.sr"
        caller.call("la.export", path=str(path), format="sr")
        transitions, _names = read_sr(path)
    estimate = estimate_baud(transitions, channel, snap=snap)
    out = dict(estimate)
    out["decoding"] = None
    if estimate["baud"] is not None:
        out["decoding"] = caller.call("la.decode", protocol="uart",
                                      settings={"channel": channel, "baud": estimate["baud"], **uart}, **extra)
    return out


# --- 4. 典型のコアクロック --------------------------------------------------------------------

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


# --- 5. RTOS のジョブの推定 ---------------------------------------------------------------------

def infer_jobs(runs: Iterable[Mapping], priorities: Mapping[Any, int], order: int = 1, *,
               idle: Iterable[Any] = (), gaps: Iterable[Mapping] = ()) -> dict:
    """タスクの区間をジョブ（起きてから待つまでの 1 回の仕事）にまとめる。区間の次に動いたタスクの優先度で区間の終わりの
    理由を決める（03f8c6e より前の rtos/analysis.py の find_jobs の、出来事の無いタスクの分岐）。

    runs: 全タスクの区間（rtos.timeline.get の runs: task・handle・t0・t1。exec（割り込みを除いた自分の時間）が
    あれば使い、無ければ t1 − t0）。gaps: 抜け（rtos.timeline.get の gaps: t0・t1）。priorities: handle かタスク名 →
    元の優先度。order: 優先度の向き（1 = 大きい方が高い（FreeRTOS）、−1 = 小さい方が高い）。idle: 除くタスク
    （handle か名前）。
    経験則（固定優先度・優先度の継承なしの仮定）: 次に動いたタスクの優先度が高い → 横取りされた（仕事は続く）、
    低い → 待った（blocked。ジョブの終わり）、同じ・分からない → uncertain（時分割か譲りか分からない）。抜け・観測の
    端で終わったら truncated。外れる場合: 優先度の継承・動的な変更があった間、高い優先度のタスクへ自分から譲った
    （待ったのに横取りに見える）、割り込みの中から起こされたタスクとの区別。応答時間は分からない（経過時間は動き
    始めからの下限）。"""
    idle = set(idle)
    gaps_ = sorted((g["t0"], g["t1"]) for g in gaps)
    rows = sorted(runs, key=lambda r: r["t0"])

    def urgency(r: Mapping) -> int | None:
        p = priorities.get(r.get("handle"), priorities.get(r.get("task")))
        return None if p is None else p * order

    open_jobs: dict[Any, dict] = {}
    jobs: list[dict] = []
    for i, r in enumerate(rows):
        if r.get("handle") in idle or r.get("task") in idle:
            continue
        key = r.get("handle", r.get("task"))
        own = r.get("exec", r["t1"] - r["t0"])
        job = open_jobs.get(key)
        if job is None:
            job = open_jobs[key] = {"task": r.get("task"), "handle": r.get("handle"), "start": r["t0"],
                                    "end": r["t1"], "exec": 0, "preemptions": 0, "end_reason": "truncated"}
        else:
            job["preemptions"] += 1
        job["exec"] += own
        job["end"] = r["t1"]
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if nxt is None or any(r["t1"] <= g0 <= nxt["t0"] or g0 <= r["t1"] < g1 for g0, g1 in gaps_):
            reason = "truncated"
        elif nxt.get("handle", nxt.get("task")) == key:
            continue                                                 # 同じタスクが続く
        else:
            mine, theirs = urgency(r), urgency(nxt)
            if mine is None or theirs is None or theirs == mine:
                reason = "uncertain"
            elif theirs > mine:
                continue                                             # 横取りされた: 仕事は続く
            else:
                reason = "blocked"
        job["end_reason"] = reason
        jobs.append(open_jobs.pop(key))
    jobs += open_jobs.values()
    jobs.sort(key=lambda j: (j["start"], str(j["task"])))
    for j in jobs:
        j["wall"] = j["end"] - j["start"]
    return _tag("rtos_jobs_from_priority",
                "固定優先度・継承なし: 次に高い優先度が動けば横取り、低ければ待った、同じなら uncertain",
                order=order, jobs=jobs)


def infer_jobs_from_api(caller: ApiCaller, *, order: int | None = None, live: bool = True,
                        idle: Iterable[Any] = (), page: int = 10000) -> dict:
    """rtos.timeline.get（区間と抜け。全部をページで集める）と rtos.tasks（各タスクの元の優先度 = base_priority が
    あればそれ、無ければ priority）から infer_jobs を呼ぶ。order を省くと rtos.objects の priority_order から。
    rtos.tasks・rtos.objects は live（走行中に読む）を渡す。優先度は読んだ時点の値（時間軸の間に変わっていれば外れる）。
    経験則・外れる場合は infer_jobs と同じ。区間の exec は無い（割り込みの時間を含む）。"""
    runs: list[dict] = []
    gaps: list[dict] = []
    offset = 0
    while True:
        got = caller.call("rtos.timeline.get", offset=offset, limit=page)
        part = got.get("runs", [])
        runs += part
        gaps = got.get("gaps", gaps)
        offset += len(part)
        if not part or offset >= got.get("runs_total", offset):
            break
    tasks = caller.call("rtos.tasks", live=live).get("tasks", [])
    priorities = {t["handle"]: t.get("base_priority", t["priority"]) for t in tasks if "handle" in t}
    if order is None:
        kind = caller.call("rtos.objects", live=live).get("priority_order")
        order = -1 if kind == "lower_is_more_urgent" else 1
    out = infer_jobs(runs, priorities, order, idle=idle, gaps=gaps)
    out["priorities"] = {t["name"]: t.get("base_priority", t["priority"]) for t in tasks if "name" in t}
    return out


# --- 6. トレースフックの CPU の負荷の見積もり ------------------------------------------------------

# フックの負荷の見積もり（Cortex-M4、FIFO が空いているとき。fda6552 より前の rtos/kernel_trace.py）: freertos_demo.elf の
# 逆アセンブル（fprobe_trace_record の中身 2 語の道筋が約 45 命令）と、ポート 31 の 1 語のインライン（約 12 命令）から。
WORD_CYCLES = 14                       # ポート 31 の 1 語
RECORD_CYCLES = 36                     # 記録の固定分（呼び出し・確認・割り込みの禁止と戻し）
PAYLOAD_CYCLES = 7                     # 記録の 1 語ごと（FIFO の確認とストア）


def trace_cpu_share(overhead: Mapping, core_clock_hz: float, baud: float | None = None, *,
                    word_cycles: float = WORD_CYCLES, record_cycles: float = RECORD_CYCLES,
                    payload_cycles: float = PAYLOAD_CYCLES, swo_limit: float = 0.8) -> dict:
    """rtos.timeline.overhead の結果（words_by_port: task・kernel・payload の語の数、seconds、swo_share）から、
    トレースフックが使う CPU の割合を命令数で見積もる（fda6552 より前の rtos/flow.py の overhead）。

    式: 命令 = task 語 × word_cycles + kernel 語（記録）× record_cycles + payload 語 × payload_cycles。
    FIFO の待ち（baud があるとき）= payload 語 × max(0, 50 / baud × core_clock_hz − payload_cycles)（記録の中身の語は
    見出しのすぐ後に書くので、見出しが SWO に出る（1 語 = 5 バイト × 10 ビット）まで待つ）。
    cpu_share = (命令 + 待ち) / core_clock_hz / seconds。baud を省くと swo_share と語の数から逆算する。
    swo_share が swo_limit（既定 0.8）を超えたら「ITM の FIFO が待ち・あふれる」の注記を足す。

    経験則: 1 語あたりのサイクル数は Cortex-M4 の 1 つのファーム（freertos_demo.elf）の逆アセンブルからの見込みで、
    周辺のレジスタの待ち・キャッシュ・フラッシュの待ちを数えない。語どうしが離れている分（ポート 31 の語・記録の
    見出し）の FIFO の待ちも数えない。外れる場合: **2026-10-18 の実機（4 Mbaud・16 MHz）で、見積もり 1.5 % に対し
    実測 6.2 % だった**（待ちを数える前の式。4 倍外れた）。別のコア・コンパイラ・最適化でも変わる。負荷は実測する:
    gateware/tests/hil/test_trace_hooks_hw.py（トレースの有無でアイドルの回る数を比べる）。"""
    w = overhead["words_by_port"]
    seconds = overhead.get("seconds") or 0
    if baud is None and overhead.get("swo_share") and seconds:
        words = w["task"] + w["kernel"] + w["payload"]
        baud = 5 * words * 10 / overhead["swo_share"] / seconds
    instructions = w["task"] * word_cycles + w["kernel"] * record_cycles + w["payload"] * payload_cycles
    wait = None
    if baud and core_clock_hz:
        wait = w["payload"] * max(0.0, 50 / baud * core_clock_hz - payload_cycles)
    cycles = instructions + (wait or 0)
    notes = []
    swo = overhead.get("swo_share")
    if swo is not None and swo > swo_limit:
        notes.append(f"SWO の帯域の {swo_limit:.0%} を超えている: ITM の FIFO が待ち・あふれる見込み。SWO を速くするか"
                     "出来事を減らす")
    return _tag("trace_hook_cycles_model",
                f"1 語 {word_cycles}・記録 {record_cycles}・中身の語 {payload_cycles} サイクル（Cortex-M4 の 1 つの"
                "ファームの見込み）+ 見出しの後の FIFO の待ち。実機で 1.5 % の見積もりが実測 6.2 % だった",
                cycles=cycles, instruction_cycles=instructions, fifo_wait_cycles=wait, baud=baud,
                core_clock_hz=core_clock_hz,
                cpu_share=cycles / core_clock_hz / seconds if core_clock_hz and seconds else None,
                cycles_model={"task_word": word_cycles, "record": record_cycles, "payload_word": payload_cycles},
                notes=notes)


# --- 7. ヒープのリークの候補・スタックの警告 ------------------------------------------------------

def heap_growing(report: Mapping) -> dict:
    """heap.trace.report の live_by_caller に growing（リークの強い候補）を足し、「growing を先、バイトの多い順」に
    並べる（be967ac より前の heap.py の _growing と leak_candidates の並び）。

    経験則: live_counts（記録の最初と最後の間を 4 つに分けた 5 時点の、生きている確保の数）のうち、後ろの 4 時点で
    減らずに増え（単調非減少で、最後 > 4 時点の最初）たら growing（リーク）とみなす。外れる場合: 起動直後に
    キャッシュ・プールを満たす（いずれ止まる増加）、記録が短く 4 時点の間に増え方が見えない、一度に解放する設計
    （ガベージを溜めてまとめて解放）。"""
    callers = []
    for c in report.get("live_by_caller", []):
        c = dict(c)
        values = [n for _, n in c.get("live_counts", [])][1:]
        c["growing"] = (len(values) >= 2 and all(b >= a for a, b in zip(values, values[1:]))
                        and values[-1] > values[0])
        callers.append(c)
    callers.sort(key=lambda c: (not c["growing"], -c["bytes"]))
    return _tag("heap_growing_live_count",
                "呼び出し元の生きている確保の数が 4 時点で減らずに増えたらリークの候補",
                leak_candidates=callers)


def heap_growing_from_api(caller: ApiCaller, min_age_s: float = 1.0, top: int = 20) -> dict:
    """heap.trace.report を呼んで heap_growing を当てる。min_age_s の既定 1 秒は元の本体の既定（それより若い確保は
    まだ解放されていないだけかもしれない、という経験則）。"""
    out = heap_growing(caller.call("heap.trace.report", min_age_s=min_age_s, top=top))
    out["min_age_s"] = min_age_s
    return out


GUARD_BYTES = 16                       # 底のこのバイト数が塗りのままかを見る（FreeRTOS の方法 2 と同じ幅）


def stack_warning(usage: Mapping, threshold: float = 0.9, guard: int = GUARD_BYTES) -> dict:
    """target.stack.usage の結果（size・used・untouched・fraction・overflow 等）に、使用率 threshold（既定 90 %）
    以上の警告と、底から guard（既定 16）バイト未満しか塗りが残っていなければ「あふれの疑い」を足す（d933ff7 より前の
    stackusage.py の measure_stack）。

    経験則: 90 % は余裕の目安。底の 16 バイトまで使ったなら、下端より下まで書いた（あふれた）かもしれないとみなす
    （FreeRTOS の方法 2 と同じ幅）。外れる場合: 塗りの値と同じ値を書いた所は使っていないと数える（使用量は過小）。
    16 バイト未満でもちょうど下端で止まっていればあふれていない。逆に塗りが残っていても、塗りを飛び越えて
    下の領域だけを書いた（大きな配列を確保して一部だけ書く）ならあふれていても見えない。"""
    untouched = usage["untouched"]
    suspected = untouched < guard or bool(usage.get("overflow"))
    fraction = usage.get("fraction", 0.0)
    notes = []
    if suspected:
        notes.append(f"底（下端）から {guard} バイト未満まで使った: あふれの疑い（下端より下の変数・ヒープが壊れて"
                     "いるかもしれない）")
    return _tag("stack_threshold_and_guard",
                f"使用率 {threshold:.0%} 以上は警告、底の {guard} バイトまで使ったらあふれの疑い",
                threshold=threshold, guard=guard, fraction=fraction, untouched=untouched,
                overflow_suspected=suspected, warning=fraction >= threshold or suspected, notes=notes)


# --- 8. 故障・接続の典型の原因 ---------------------------------------------------------------------

# 9c7eccf より前の fault.py の hint（経験的な原因の候補を含む）
_FAULT_TYPICAL = {
    "IACCVIOL": "実行禁止（XN）の領域・MPU で許していない番地へ分岐した",
    "DACCVIOL": "MMFAR の番地（MMARVALID のとき）。NULL ポインタの参照が多い",
    "MUNSTKERR": "スタックの壊れ",
    "MSTKERR": "スタックのあふれ（スタックの下限を越えた）",
    "MLSPERR": "スタックのあふれ",
    "IBUSERR": "存在しない番地・電源や時計の止まった領域へ分岐した",
    "PRECISERR": "BFAR の番地（BFARVALID のとき）。積まれた PC が故障した命令",
    "IMPRECISERR": "書き込みバッファのため、積まれた PC は故障の後の命令。時計の入っていない周辺への書き込みが多い",
    "UNSTKERR": "スタックの壊れ",
    "STKERR": "スタックのあふれ（SRAM の外へ）",
    "LSPERR": "スタックのあふれ",
    "UNDEFINSTR": "壊れたコード・データ領域への分岐・別の CPU 向けのビルド",
    "INVSTATE": "番地の bit0 が 0 の関数ポインタへ分岐した（関数ポインタの壊れ・ベクタ表の bit0）",
    "INVPC": "LR の壊れ・スタックの壊れ",
    "NOCP": "CPACR の CP10・CP11 を有効にしていない（起動のコードで FPU を有効にする）",
    "STKOF": "スタックのあふれ",
    "UNALIGNED": "構造体のパック・ポインタの型の取り違え",
    "VECTTBL": "VTOR の値・ベクタ表の場所",
    "FORCED": "その故障が無効か、優先度が足りない。原因は CFSR を見る",
    "DEBUGEVT": "デバッガが無いのに BKPT を実行した等",
    "HardFault": "積まれた PC の命令と、その時のレジスタから推測する",
}


def fault_hints(report: Mapping) -> dict:
    """target.fault の結果の causes の各ビットに、典型の原因（typical）を足す（9c7eccf より前の fault.py の hint）。
    本体の hint（アーキテクチャの定義から言えること）はそのまま残す。

    経験則: 「NULL 参照が多い」「スタックのあふれ」等は、よくある原因の見込みで、そのビットの定義ではない。外れる
    場合: 同じビットは別の原因でも立つ（例 STKERR はスタックのあふれのほか、SP を壊した・MSP の初期値の誤り）。
    原因は積まれたフレーム（frame）・故障の番地で確かめる。"""
    causes = []
    for c in report.get("causes", []):
        c = dict(c)
        typical = _FAULT_TYPICAL.get(c.get("bit"))
        if typical:
            c["typical"] = typical
        causes.append(c)
    return _tag("fault_typical_causes", "故障のビットごとの、よくある原因の見込み（そのビットの定義ではない）",
                causes=causes, exception=report.get("exception"), faulted=report.get("faulted"))


def diagnose_hints(result: Mapping, min_volts: float = 1.0, low_volts: float = 1.65,
                   high_volts: float = 3.6) -> dict:
    """target.diagnose の結果の steps に、電圧の判定と原因の候補（typical）を足す（9331a9b・bb73f1c より前の
    diagnose.py の文）。

    経験則:
    - 電圧: min_volts（1.0 V）未満は電源が入っていない、low_volts（1.65 V）未満はレベル変換が要る（fprobe の入出力は
      3.3 V）、high_volts（3.6 V）超は fprobe を壊す。ただしターゲットが SWD・JTAG に応答したのに 1.0 V 未満なら、
      電源ではなく電圧の計測の配線（分圧・補助マイコン）を疑う（2026-10-18 の実機で、補助の Pico の計測の線が外れて
      いたのに「電源が入っていない」と誤って結論した）。
    - 原因の候補: SWD の無応答 → 配線・電源・GND・SWD が無効、パリティ誤り → 信号品質、WAIT → 低消費電力・クロック
      停止、低い速さでだけ応答 → 配線長・GND・信号品質、デバッグ電源の無応答 → 深い低消費電力・電源ドメイン・保護、
      ピンが Low → プルアップ・短絡・リセットの押さえ。
    外れる場合: どれもよくある原因の見込みで、測った事実ではない（例: 計測の線が外れていても 0 V になる）。"""
    steps = [dict(s) for s in result.get("steps", [])]
    responded = any(s["name"] in ("swd", "jtag") and s["status"] in ("ok", "warn") for s in steps)
    for s in steps:
        typical: list[str] = []
        data = s.get("data", {})
        msg = s.get("message", "")
        if s["name"] == "voltage":
            volts = data.get("volts")
            if data.get("over_range"):
                typical.append("分圧の配線を確かめる")
            elif s["status"] == "ok" and volts is not None:
                if volts < min_volts:
                    typical.append("電圧の計測は低いが、ターゲットは応答している: 電圧の計測の配線（分圧・補助マイコン）"
                                   "を確かめる" if responded else
                                   f"ターゲットの電源が入っていない（{min_volts} V 未満）")
                elif volts < low_volts:
                    typical.append(f"ターゲットの電圧が低い（{low_volts} V 未満）: fprobe の入出力は 3.3 V。レベル変換が要る")
                elif volts > high_volts:
                    typical.append(f"ターゲットの電圧が高い（{high_volts} V 超）: 3.3 V を超える入力は fprobe を壊す")
        elif s["name"] == "pins":
            if data.get("nreset") == 0:
                typical.append("nRESET が Low: リセットが掛かったまま。外部で押さえていないか・プルアップ")
            if data.get("swdio") == 0:
                typical.append("SWDIO が Low: ターゲットのプルアップが無い・短絡・電源が無い")
        elif s["name"] == "swd":
            if msg.startswith("SWD に応答が無い"):
                typical.append("配線・電源・GND・SWD が無効のターゲット")
            elif "パリティ誤り" in msg:
                typical.append("信号品質: 配線長・GND・速度")
            elif "WAIT を返し続ける" in msg:
                typical.append("ターゲットが低消費電力・クロック停止")
            elif "では応答せず" in msg:
                typical.append("配線長・GND・信号品質を疑う")
        elif s["name"] == "power" and s["status"] == "fail" and "CDBGPWRUPACK" in msg:
            typical.append("ターゲットが深い低消費電力・電源ドメインが落ちている・保護")
        if typical:
            s["typical"] = typical
    return _tag("diagnose_typical_causes",
                f"電圧 {min_volts} V 未満は電源なし・{low_volts} V 未満は低い・{high_volts} V 超は高い。"
                "各段の失敗によくある原因の見込み",
                min_volts=min_volts, low_volts=low_volts, high_volts=high_volts, verdict=result.get("verdict"),
                ok=result.get("ok"), steps=steps)


# --- 9. disasm の境界の推定 -------------------------------------------------------------------

def disasm_before_pc(caller: ApiCaller, pc: int, before: int = 8, after: int = 8) -> dict:
    """PC の前の命令の境界を「Thumb は数命令で揃う」と見込んで推定し、PC の前 before 命令・PC・後ろ after 命令を
    並べる（f2cfa3c より前の disasm.py の _sync_start の heuristic）。

    メモリは target.memory.read で読み、本体の逆アセンブラ（fprobe.disasm.Disassembler）をシンボル無しで使う。
    遠い候補（PC − 2 × (2 × before + 8)）から近い方へ、そこから読んで解読できないバイト無しで PC にちょうど着地する
    最初の点を境界とみなす。どこも着地しなければ PC から読む（sync = "start"）。
    経験則: Thumb の命令の境界は、違う所から読み始めても数命令で正しい境界に揃う。外れる場合: リテラルプール・表
    （データ）の直後、32 ビット命令の後半から読むともっともらしい誤った命令の列になることがある。関数の先頭・$t が
    分かるなら本体の debug.disasm.around（sync = function / mapping）が事実。"""
    from ..disasm import Disassembler
    from ..memview import BytesMemory
    reach = 2 * (2 * max(before, 1) + 8)
    tail = 4 * (after + 1) + 2
    base = max(0, pc - reach)
    data = bytes.fromhex(caller.call("target.memory.read", address=base, length=pc - base + tail + 2))
    dis = Disassembler(BytesMemory(base, data))
    start = pc
    for k in range((pc - base) // 2, 0, -1):
        try:
            head = dis.range(pc - 2 * k, pc).instructions
        except Exception:
            continue
        if head and head[-1].address + head[-1].size == pc and not any(i.kind == "data" for i in head):
            start = pc - 2 * k
            break
    instructions = list(dis.range(start, pc + tail - 2).instructions)
    at = next((n for n, i in enumerate(instructions) if i.address == pc), None)
    sync = "heuristic" if start < pc else "start"
    if at is None:                                       # PC が推定した列の命令の途中: PC から読む
        instructions, at, sync = list(dis.range(pc, pc + tail - 2).instructions), 0, "start"
    prior = instructions[:at][-before:] if before > 0 else []
    listing = prior + instructions[at:at + after + 1]
    return _tag("thumb_self_sync", "Thumb の命令の境界は、違う所から読んでも数命令で揃う",
                sync=sync, pc=pc, pc_index=len(prior), instructions=[i.as_dict() for i in listing])


# --- 2 回目に外した判断（2026-10-22 以降。台帳 .superpowers/sdd/heuristics-ledger.md） ----------------------

def aliasing_suspects(result: Mapping, min_expected: float = 4.6, top: int | None = None) -> dict:
    """profile.result（method=itm・by="exception"）の結果から、PC サンプルと例外トレースの負荷が「噛み合い（エイリアシング）で
    ずれている」と見る例外を挙げる（4e2138e より前の本体の判定）。本体は信頼区間の外という事実だけを notes に書く。

    経験則:
    - サンプル 0 回でも、期待回数 load × total が min_expected（4.6 = 0 回になる確率が約 1 %）以上なら噛み合いと見る。
    - 1 回以上なら信頼区間に余裕 2/√total × √load を足し、その外なら噛み合いと見る。余裕の幅は経験則。
    外れる場合: 例外の負荷が測る間に本当に変わった（記録の窓とサンプルの窓が違う）とき、ずれは噛み合いではない。
    rows は上位 top 件で切られる。top（求めた件数）を渡し、rows がその件数に達していれば、rows に無い例外は 0 回と
    みなさず unknown に入れる（aliasing_suspects_from_api は全件を取る）。"""
    from fprobe.profile.stats import wilson_interval
    if result.get("by") != "exception":
        raise ValueError('profile.result を by="exception" で取った結果を渡す（ほかの表では例外ごとの回数が無い）')
    total, confidence = result["total"], result.get("confidence", 0.95)
    rows = result.get("rows", [])
    hits = {r["exception"]: r["samples"] for r in rows}
    ci = {r["exception"]: r["ci"] for r in rows if r.get("ci")}
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
        low, high = ci.get(e["exception"]) or wilson_interval(n, total, confidence)
        slack = 2 / math.sqrt(total) * math.sqrt(max(load, 1e-9))
        if load < low - slack or load > high + slack:
            suspects.append({"exception": e["exception"], "name": e.get("name"), "load": load, "samples": n,
                             "reason": "outside_interval"})
    return _tag("aliasing", "サンプルの割合と例外トレースの負荷のずれは、サンプルの間隔と割り込みの周期の噛み合いによる。"
                "負荷は例外トレースの値を使い、rate_hz を少し変えると偏りが変わる",
                suspects=suspects, unknown=unknown, min_expected=min_expected)


def aliasing_suspects_from_api(caller: ApiCaller, min_expected: float = 4.6, top: int = 1000) -> dict:
    """profile.result(by="exception", top=top) を取り、aliasing_suspects に渡す。"""
    return aliasing_suspects(caller.call("profile.result", by="exception", top=top), min_expected, top)


def heap_overflow_suspects(scan: Mapping) -> dict:
    """heap.scan の壊れ（bad_size / bad_link / bad_end）ごとに、直前の使用中のブロック（previous_used）を「あふれさせた
    疑い」とする（1744653 より前の本体の表示）。外れる場合: 解放後の書き込み・別の領域からの迷った書き込み。"""
    suspects = [{"kind": i["kind"], "address": i["address"], "suspect": i["previous_used"]}
                for i in scan.get("issues", []) if i.get("previous_used") is not None]
    return _tag("heap_overflow_from_previous", "ヘッダの壊れは、すぐ前の使用中のブロックの書き込みのあふれによる",
                suspects=suspects)


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


_ACK_CAUSES = {"WAIT": ["低消費電力", "クロック停止", "バスが塞がっている"],
               "FAULT": ["前の誤りの印が残っている", "アクセスが拒まれた"],
               "NO_ACK": ["配線", "電源", "速度", "線のプロトコルの選択"],
               "PROTOCOL": ["信号品質: 速度を下げる・GND・配線長"]}
_TDO_CAUSES = {"stuck_low": ["TDO の線", "ターゲットの電源", "TRST"],
               "stuck_high": ["TDO が浮いてプルアップ", "TCK / TMS の配線", "JTAG が無効のターゲット"]}


def link_cause_hints(ack: str | None = None, tdo: str | None = None) -> dict:
    """SWD/JTAG の ACK（WAIT / FAULT / NO_ACK / PROTOCOL）や jtag.scan の tdo（stuck_low / stuck_high）に、よくある原因を
    並べる（27cdb7e より前の本体の文）。順に確かめる候補で、どれかである保証は無い。"""
    causes = _ACK_CAUSES.get(ack or "", []) + _TDO_CAUSES.get(tdo or "", [])
    return _tag("link_causes", "よくある原因の一覧。ほかの原因もありうる", ack=ack, tdo=tdo, causes=causes)


def jtag_guess_ir(scan: Mapping) -> dict:
    """jtag.scan の Capture-IR（ir_capture・total_ir_length）を、境目が一つに決まらない装置では最短の候補を取って
    装置に分け直し、jtag_chain を作る（27cdb7e より前の本体の推測）。表の IR 長（ir_length_source="table"）は使う。
    guessed = 最短の候補を取った装置。外れる場合: IR の Capture 値に境目と同じ形（…01）が並ぶ装置。データシートの値が
    あればそちらを jtag_chain に与える。"""
    total, capture = scan.get("total_ir_length"), scan.get("ir_capture")
    chain, guessed = [], []
    if total is not None and capture is not None:
        starts = sorted(k for k in range(total) if (capture >> k) & 1 and not (capture >> (k + 1)) & 1)
        devices = scan["devices"]
        known = [d["ir_length"] if d.get("ir_length_source") == "table" else None for d in devices]
        pos = 0
        for i, n in enumerate(known):
            rest = known[i + 1:]
            if n is None:
                if not rest or None not in rest:
                    n = total - pos - sum(rest)
                else:
                    candidates = [k for k in starts if k >= pos + 2]
                    if not candidates:
                        break
                    n = candidates[0] - pos
                    if len(candidates) > len(rest):
                        guessed.append(devices[i]["index"])
            chain.append(n)
            pos += n
        if len(chain) != len(devices) or pos != total or min(chain, default=2) < 2:
            chain = None
    else:
        chain = None
    return _tag("ir_shortest", "Capture-IR の境目の候補のうち、最短が本当の IR 長",
                jtag_chain=chain, guessed=guessed)


def wait_failed_as_timeout(stats: Mapping) -> dict:
    """rtos の待ちの統計（failed = 起きた後の操作が失敗）を「時間切れ」と読む（715c29a より前の本体の outcome="timeout"）。
    外れる場合: 待ちの中断（xTaskAbortDelay）・削除されたオブジェクト。"""
    return _tag("failed_is_timeout", "待ちの後の操作の失敗は、時間切れによる", timeouts=stats.get("failed", 0))
