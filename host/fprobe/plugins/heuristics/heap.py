"""プラグイン heuristics: ヒープの見方（リークの候補・確保の包みの名前・あふれの疑い）。"""

from collections.abc import Mapping, Sequence

from .. import ApiCaller
from ._common import _tag

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


# 標準ライブラリ・FreeRTOS の確保の関数とその包みの名前（総点検 Q3 で本体の probe_heap から外した名前の表）
ALLOCATOR_WRAPPERS = ("malloc", "free", "calloc", "realloc", "_calloc_r", "_realloc_r", "_malloc_r", "_free_r",
                      "reallocf", "_reallocf_r", "memalign", "_memalign_r", "strdup", "_strdup_r", "strndup",
                      "_strndup_r", "pvPortMalloc", "vPortFree", "pvPortCalloc", "__wrap_malloc", "__wrap_free")


def heap_trace_start_skipping_wrappers(caller: ApiCaller, allocators: list[str] | None = None,
                                       wrappers: Sequence[str] = ALLOCATOR_WRAPPERS) -> dict:
    """heap.trace.start を、確保の関数の包み（calloc・strdup 等）を呼び出し元から飛ばす形で呼ぶ（skip_functions）。

    経験則: 関数の名前が ALLOCATOR_WRAPPERS にあれば、それは確保の包みで、本当の呼び出し元はその外とみなす。外れる場合:
    利用者が同じ名前の関数を別の役目で持つ・包みを別の名前で作った（__wrap_ 以外の --wrap 等）・名前がリンクで変わった。
    飛ばしたフレームは本体の出来事の skipped に出る。"""
    out = dict(caller.call("heap.trace.start", allocators=allocators, skip_functions=list(wrappers)))
    out.update(_tag("allocator_wrapper_names", "名前が標準の確保の関数・包みなら、呼び出し元はその外"))
    return out


def heap_overflow_suspects(scan: Mapping) -> dict:
    """heap.scan の壊れ（bad_size / bad_link / bad_end）ごとに、直前の使用中のブロック（previous_used）を「あふれさせた
    疑い」とする（1744653 より前の本体の表示）。外れる場合: 解放後の書き込み・別の領域からの迷った書き込み。"""
    suspects = [{"kind": i["kind"], "address": i["address"], "suspect": i["previous_used"]}
                for i in scan.get("issues", []) if i.get("previous_used") is not None]
    return _tag("heap_overflow_from_previous", "ヘッダの壊れは、すぐ前の使用中のブロックの書き込みのあふれによる",
                suspects=suspects)
