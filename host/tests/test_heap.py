"""fprobe.heap: ヒープの走査（FreeRTOS heap_4 / heap_5・newlib-nano・newlib の dlmalloc）と壊れの検出、
malloc / free の記録からのリークの候補。"""

import pytest

from fprobe.heap import AllocationTracker, HeapError, detect, scan
from heap_fakes import ALLOCATED, RAM, DlHeap, FreeRTOSHeap, NanoHeap


def kinds(result):
    return sorted({i["kind"] for i in result.issues})


# --- FreeRTOS heap_4 / heap_5 -------------------------------------------------------------------

def test_heap_4_blocks_usage_and_fragmentation():
    h = FreeRTOSHeap([(64, True), (128, False), (32, True), (256, False), (48, True)])
    r = scan(h.mem.read, h.syms, "freertos")
    assert r.allocator == "freertos" and r.scheme == "heap_4" and not r.issues
    used = [b for b in r.blocks if b.used]
    assert [b.size for b in used] == [64, 32, 48] and used[0].pointer == h.start + 8
    d = r.to_dict()
    free_total = 128 + 256 + (h.end - h.start - 64 - 128 - 32 - 256 - 48)
    assert d["free"] == free_total and d["used"] == 144 and d["used_blocks"] == 3
    assert d["largest_free"] == max(256, h.end - h.start - 528)
    assert d["fragmentation"] == pytest.approx(1 - d["largest_free"] / free_total)
    assert d["counters"]["free_bytes"] == free_total and d["regions"] == [[h.start, h.end]]


def test_heap_4_with_the_heap_protector():
    h = FreeRTOSHeap([(64, True), (128, False), (32, True)], canary=0x5A5A_1234)
    r = scan(h.mem.read, h.syms, "freertos")
    assert not r.issues and len([b for b in r.blocks if not b.used]) == 2


def test_heap_4_broken_header_reports_the_previous_used_block_as_a_fact():
    # 壊した者を「直前の使用中のブロックからのあふれの疑い」と推定しない（間に空きを挟むこともある）。
    # 直前の使用中のブロック（previous_used）を事実として添える
    h = FreeRTOSHeap([(64, True), (128, True), (32, False)])
    h.mem.u32(h.addresses[1] + 4, 0x41414141)                   # 1 つ目の本体があふれて 2 つ目の頭を上書き
    r = scan(h.mem.read, h.syms, "freertos")
    bad = [i for i in r.issues if i["kind"] == "bad_size"]
    assert bad and bad[0]["address"] == h.addresses[1] and bad[0]["previous_used"] == h.start + 8
    assert "suspect" not in bad[0] and not any("疑い" in i["detail"] for i in r.issues)


def test_heap_4_detects_list_and_counter_inconsistencies():
    h = FreeRTOSHeap([(64, False), (64, False), (64, True)])     # 隣り合う空き（heap_4 は必ず繋ぐ）
    h.mem.u32(h.free_var, 12)
    h.mem.u32(h.addresses[2], 0x2000_1234)                       # 使用中なのに次の空きを指す
    r = scan(h.mem.read, h.syms, "freertos")
    assert {"unmerged_free", "counter_mismatch", "bad_link"} <= set(kinds(r))


def test_heap_4_free_block_missing_from_the_list():
    h = FreeRTOSHeap([(64, True), (64, False), (64, True), (64, False), (64, True)])
    h.mem.u32(h.start_var, h.addresses[3])                       # 1 つ目の空きをリストから外す
    r = scan(h.mem.read, h.syms, "freertos")
    assert "free_not_listed" in kinds(r)


def test_heap_4_free_list_loop_is_stopped():
    h = FreeRTOSHeap([(64, False), (64, True), (64, False), (64, True)])
    h.mem.u32(h.addresses[2], h.addresses[0])                    # 後ろの空きが前を指す
    r = scan(h.mem.read, h.syms, "freertos")
    assert {"free_list_unsorted"} <= set(kinds(r)) or {"free_list_loop"} <= set(kinds(r))


def test_reaching_the_free_list_limit_is_not_reported_as_corruption(monkeypatch):
    # 辿る数の上限に着いたのを「輪」「外を指す」（壊れ）と言わない（点検 2 回目）: complete = false と上限の事実
    import fprobe.heap as heap
    monkeypatch.setattr(heap, "MAX_FREE_BLOCKS", 1)
    h = FreeRTOSHeap([(64, False), (64, True), (64, False), (64, True)])
    r = scan(h.mem.read, h.syms, "freertos")
    assert not {"free_list_loop", "free_list_outside"} & set(kinds(r)) and not r.complete
    assert any("1 個" in n for n in r.notes)
    n = NanoHeap([(24, True, 4), (40, False, 0), (16, True, 4), (64, False, 0)])
    rn = scan(n.mem.read, n.syms, "newlib_nano", end=n.top)
    assert not {"free_list_loop", "free_list_outside"} & set(kinds(rn)) and not rn.complete
    assert any("1 個" in note for note in rn.notes)


def test_heap_4_before_the_first_allocation():
    h = FreeRTOSHeap([])
    h.mem.u32(h.end_var, 0)
    r = scan(h.mem.read, h.syms, "freertos")
    assert not r.blocks and any("初期化" in n for n in r.notes)


def test_heap_5_regions_are_walked_from_the_application_table():
    h = FreeRTOSHeap([])
    s, mem = h.syms, h.mem
    del s.table["ucHeap"]
    regions = [(RAM + 0x1000, 0x400), (RAM + 0x3000, 0x200)]
    table = s.add("xHeapRegions", RAM + 0x180, 24)
    ends, starts = [], []
    for i, (start, size) in enumerate(regions):
        mem.u32(table + 8 * i, start)
        mem.u32(table + 8 * i + 4, size)
        end = (start + size - 8) & ~7
        starts.append(start)
        ends.append(end)
        mem.u32(start + 4, 0x40 | ALLOCATED)                     # 先頭 64 バイトを使用中に
        mem.u32(start, 0)
        mem.u32(start + 0x40 + 4, end - start - 0x40)
        mem.u32(end + 4, 0)
    mem.u32(table + 16, 0)
    mem.u32(table + 20, 0)
    # 空きのリスト: xStart → 領域 1 の空き → 領域 1 の終わり（大きさ 0）→ 領域 2 の空き → pxEnd
    mem.u32(h.start_var, starts[0] + 0x40)
    mem.u32(starts[0] + 0x40, ends[0])
    mem.u32(ends[0], starts[1] + 0x40)
    mem.u32(starts[1] + 0x40, ends[1])
    mem.u32(ends[1], 0)
    mem.u32(h.end_var, ends[1])
    free = (ends[0] - starts[0] - 0x40) + (ends[1] - starts[1] - 0x40)
    mem.u32(h.free_var, free)
    mem.u32(h.allocs_var, 2)
    mem.u32(h.frees_var, 0)
    # アプリの表の変数の名前（xHeapRegions 等）は慣習なので探さない（点検 2 回目 H5）: 使う側が regions_symbol で渡す
    unnamed = scan(mem.read, s, "freertos")
    assert unnamed.regions == [] and not unnamed.complete and any("regions_symbol" in n for n in unnamed.notes)
    r = scan(mem.read, s, "freertos", regions_symbol="xHeapRegions")
    assert r.scheme == "heap_5" and r.regions == [(starts[0], ends[0]), (starts[1], ends[1])]
    assert not r.issues and r.to_dict()["used_blocks"] == 2 and r.to_dict()["free"] == free
    with pytest.raises(ValueError, match="xHeapRegion"):
        scan(mem.read, s, "freertos", regions_symbol="xHeapRegion")


def test_heap_5_without_a_region_table_reads_only_the_free_list():
    h = FreeRTOSHeap([(64, True), (128, False)])
    del h.syms.table["ucHeap"]
    r = scan(h.mem.read, h.syms, "freertos")
    assert r.scheme == "heap_5" and not any(b.used for b in r.blocks)
    assert any("regions" in n for n in r.notes)
    r2 = scan(h.mem.read, h.syms, "freertos", regions=[(h.heap, 0x2000)])
    assert r2.to_dict()["used_blocks"] == 1


# --- newlib-nano --------------------------------------------------------------------------------

def test_nano_chunks_and_free_list():
    h = NanoHeap([(24, True, 4), (40, False, 0), (16, True, 4), (64, False, 0), (32, True, 4)])
    r = scan(h.mem.read, h.syms, "newlib_nano")
    assert r.allocator == "newlib_nano" and not r.issues
    assert [b.pointer for b in r.blocks if b.used] == h.pointers
    d = r.to_dict()
    assert d["free"] == 104 and d["used"] == 72 and d["largest_free"] == 64


def test_nano_unmerged_neighbours_and_bad_padding():
    h = NanoHeap([(24, True, 4), (40, False, 0), (40, False, 0), (16, True, 4)])
    h.mem.u32(h.base + 4, 0x1234)                                # 詰め物の印（ptr − 4 の負の位置）が壊れた
    r = scan(h.mem.read, h.syms, "newlib_nano")
    assert {"unmerged_free", "bad_padding"} <= set(kinds(r))


def test_nano_without_the_end_walks_until_the_shape_breaks_and_says_it_is_incomplete():
    # sbrk の実装の変数の名前（__sbrk_heap_end・heap_end）は慣習なので探さない（点検 2 回目 H4）。終わりを渡されなければ
    # 形が崩れた所で止まり、そこが終わりか壊れかは決めない: complete = false と止まった所の事実
    h = NanoHeap([(24, True, 4), (40, False, 0)])
    r = scan(h.mem.read, h.syms, "newlib_nano")
    assert len(r.blocks) == 2 and not r.complete and "break" not in r.counters
    assert any("break_symbol" in n and f"0x{h.top:08X}" in n for n in r.notes)
    named = scan(h.mem.read, h.syms, "newlib_nano", break_symbol="__sbrk_heap_end")
    assert named.complete and named.counters["break"] == h.top and len(named.blocks) == 2
    r2 = scan(h.mem.read, h.syms, "newlib_nano", end=h.top)
    assert r2.complete and not any("break_symbol" in n for n in r2.notes)
    with pytest.raises(ValueError, match="heap_end"):
        scan(h.mem.read, h.syms, "newlib_nano", break_symbol="heap_end")


# --- newlib dlmalloc ----------------------------------------------------------------------------

def test_dlmalloc_chunks_top_and_links():
    h = DlHeap([(32, True), (48, False), (24, True), (64, False), (16, True)])
    r = scan(h.mem.read, h.syms, "newlib")
    assert r.allocator == "newlib" and not r.issues, r.issues
    used = [b for b in r.blocks if b.used]
    assert [b.pointer for b in used] == [h.addresses[i] + 8 for i in (0, 2, 4)]
    d = r.to_dict()
    assert d["free"] == 48 + 64 + 0x200 and d["largest_free"] == 0x200


def test_dlmalloc_broken_links_and_footer():
    h = DlHeap([(32, True), (48, False), (32, True)])
    h.mem.u32(h.addresses[1] + 8, RAM + 0x5000)                  # fd が壊れた
    h.mem.u32(h.addresses[1] + 48, 99)                           # 足が合わない
    r = scan(h.mem.read, h.syms, "newlib")
    assert {"bad_links", "bad_footer"} <= set(kinds(r))


# --- 種類の判定 --------------------------------------------------------------------------------

def test_detect():
    assert detect(FreeRTOSHeap([]).syms) == ["freertos"]
    assert detect(NanoHeap([(24, True, 4)]).syms) == ["newlib_nano"]
    assert detect(DlHeap([]).syms) == ["newlib"]
    with pytest.raises(HeapError):
        scan(FreeRTOSHeap([]).mem.read, NanoHeap([(24, True, 4)]).syms, "freertos")


# --- malloc / free の記録 ---------------------------------------------------------------------

def test_tracker_live_peak_and_issues():
    t = AllocationTracker()
    t.alloc(1.0, 0x100, 32, caller="a")
    t.alloc(2.0, 0x200, 64, caller="b")
    t.free(3.0, 0x100, caller="x")
    t.free(4.0, 0x100, caller="x")                               # 二重の解放
    t.free(5.0, 0x900, caller="x")                               # 記録を始める前の確保（誤りではない）
    t.alloc(6.0, 0, 16, caller="c")                              # 失敗
    t.alloc(7.0, 0x200, 8, caller="d")                           # 解放されていない番地をまた返した
    r = t.report(now=10.0)
    assert r["live"]["count"] == 1 and r["live"]["bytes"] == 8
    assert r["peak_bytes"] == 96 and r["allocations"] == 4 and r["frees"] == 3
    kinds_ = {i["kind"] for i in r["issues"]}
    assert {"double_free", "failed_allocation", "reused_live_pointer"} <= kinds_
    assert r["untracked_frees"] == 1


def test_tracker_live_by_caller_is_facts_without_a_growing_verdict():
    # 「4 時点で減らずに増えた → growing（強い候補）を先頭」と判定しない。呼び出し元ごとの生きている確保と、
    # 時点ごとの生きている数（live_counts）を事実として返し、バイトの多い順に並べる
    t = AllocationTracker()
    now = 0.0
    for i in range(40):                                          # leaky は確保だけ、steady は確保と解放
        now += 1
        t.alloc(now, 0x1000 + 16 * i, 16, caller="leaky")
        t.alloc(now + 0.1, 0x8000, 32, caller="steady")
        t.free(now + 0.5, 0x8000, caller="steady")
    t.alloc(now, 0x9000, 100, caller="once")                     # 最後に 1 回（若い）
    r = t.report(now=now + 1, min_age_s=5)
    leaks = r["live_by_caller"]
    assert "leak_candidates" not in r and not any("growing" in c for c in leaks)
    assert leaks[0]["caller"] == "leaky" and leaks[0]["count"] >= 35
    ns = [n for _, n in leaks[0]["live_counts"]]
    assert len(ns) == 5 and ns[0] <= 1 and ns[-1] == 40 and ns == sorted(ns)
    assert [c["bytes"] for c in leaks] == sorted((c["bytes"] for c in leaks), reverse=True)
    assert all(c["caller"] != "steady" for c in leaks)
    assert all(c["caller"] != "once" for c in leaks)
    assert r["series"][-1][1] == r["live"]["bytes"]
    assert r["live_points"] == 5                                            # 点の数を結果に出す（?24）
    finer = t.report(now=now + 1, min_age_s=5, live_points=9)["live_by_caller"][0]["live_counts"]
    assert len(finer) == 9


def test_tracker_says_when_it_cut_the_issues_and_the_series():
    # 黙って切らない（点検 2 回目）: 誤りは issues_limit 件まで持ち、総数・切ったか・上限を返す。推移は series_points
    # 点まで間引き、元の点の数・間引いたか・使った点の数を返す
    t = AllocationTracker(issues_limit=3)
    for i in range(5):
        t.alloc(float(i), 0, 16, caller="c")                    # 失敗 5 回
    for i in range(10):
        t.alloc(10.0 + i, 0x100 + 16 * i, 16, caller="c")
    r = t.report(now=30.0, series_points=4)
    assert len(r["issues"]) == 3 and r["issues_total"] == 5 and r["issues_truncated"] and r["issues_limit"] == 3
    assert len(r["series"]) == 4 and r["series_total"] == 10 and r["series_downsampled"] and r["series_points"] == 4
    full = t.report(now=30.0, series_points=100)
    assert len(full["series"]) == 10 and not full["series_downsampled"]


# --- 実際の ELF（freertos_demo、heap_4）: static の変数を ELF のシンボルで引き、U08 の要約と一致する ----------------

def test_freertos_demo_heap_4_through_the_elf_symbols():
    from rtos_kernel import FreeRTOSBuilder, shared

    from fprobe.heap import SymbolTable
    from fprobe.rtos.freertos import FreeRTOS

    k = FreeRTOSBuilder()
    heap = k.var("ucHeap")
    start, end = (heap + 7) & ~7, (heap + 20 * 1024 - 8) & ~7
    layout = [(64, True), (96, False), (128, True)]
    a, free = start, []
    for size, used in layout:
        k.u32(a, 0)
        k.u32(a + 4, size | (ALLOCATED if used else 0))
        if not used:
            free.append(a)
        a += size
    k.u32(a + 4, end - a)
    free.append(a)
    k.u32(end, 0)
    k.u32(end + 4, 0)
    previous = k.var("xStart")
    for f in free:
        k.u32(previous, f)
        previous = f
    k.u32(previous, end)
    k.u32(k.var("pxEnd"), end)
    total_free = 96 + end - a
    k.heap_state(free=total_free, minimum=total_free - 100, allocations=2, frees=0)
    k.u32(k.var("pxEnd"), end)                                     # heap_state は blocks 無しでは pxEnd に触れない
    info, symbols = shared()
    syms = SymbolTable(symbols)
    assert detect(syms) == ["freertos"]
    r = scan(k.memory.read_memory, syms, "freertos")
    assert r.scheme == "heap_4" and not r.issues, r.issues
    d = r.to_dict()
    summary = FreeRTOS(info, k.memory, symbols).heap()
    assert d["free_blocks"] == summary.free_blocks and d["largest_free"] == summary.largest_free_block
    assert d["used_blocks"] == 2 and d["counters"]["free_bytes"] == summary.free
