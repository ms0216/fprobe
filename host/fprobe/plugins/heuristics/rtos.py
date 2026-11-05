"""プラグイン heuristics: RTOS・トレースの見方（ジョブの推定・トレースフックの負荷・待ちの時間切れ・TraceX の切り替え）。"""

from collections.abc import Iterable, Mapping
from typing import Any

from .. import ApiCaller
from ._common import _tag

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


def wait_failed_as_timeout(stats: Mapping) -> dict:
    """rtos の待ちの統計（failed = 起きた後の操作が失敗）を「時間切れ」と読む（715c29a より前の本体の outcome="timeout"）。
    外れる場合: 待ちの中断（xTaskAbortDelay）・削除されたオブジェクト。"""
    return _tag("failed_is_timeout", "待ちの後の操作の失敗は、時間切れによる", timeouts=stats.get("failed", 0))


_TRACEX_ISR, _TRACEX_INIT = 0xFFFFFFFF, 0xF0F0F0F0             # tracefmt.tracex の ISR_CONTEXT・INIT_CONTEXT


def tracex_context_switches(events: Iterable[Mapping]) -> dict:
    """TraceX の出来事（トレースの events。各出来事の args.context = その時の thread_pointer）から、文脈が別の
    スレッドに変わった所を切り替え（task_in）とみなす（2026-10-24 より前の本体の tracefmt.tracex の推定）。
    割り込みの中（isr_enter と isr_exit の間）・初期化の文脈では数えない。

    経験則: 切り替えの時刻 = 文脈の変わった最初の出来事の時刻とする（TraceX の表示と同じ考え）。外れる場合: 切り替えの
    後しばらく出来事の無いスレッドは、その出来事まで前のスレッドが動いていたように見える（時刻は遅れる）。出来事を
    1 つも出さずに動いて戻ったスレッドは見えない。"""
    out: list[dict] = []
    running, depth = None, 0
    for e in events:
        ctx, kind = (e.get("args") or {}).get("context"), e.get("kind")
        if ctx is not None and ctx not in (_TRACEX_ISR, _TRACEX_INIT) and ctx != running and depth == 0:
            out.append({"ts": e["ts"], "kind": "task_in", "task": ctx})
            running = ctx
        if kind == "isr_enter":
            depth += 1
        elif kind == "isr_exit":
            depth = max(0, depth - 1)
    return _tag("tracex_context_change",
                "スレッドは文脈の変わった最初の出来事の時刻に切り替わった（間に出来事が無ければ時刻は遅れる）",
                task_in=out)
