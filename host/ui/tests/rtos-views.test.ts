// RTOS・Code Profile・Interrupts・Call Graph のビューの計算（lib/rtos.ts・profile.ts・interrupts.ts・callgraph.ts）
import { describe, expect, it } from "vitest";
import { stackTops } from "../src/lib/chart";
import { deepestKeys, rootsOf, treeRows, type StackFunction } from "../src/lib/callgraph";
import { isrAt, longestIn, profileHz, recordedIsrs, statsFromProfile } from "../src/lib/interrupts";
import { SymbolAddresses, baseName, heatOf, selectedRange, sourceHeat } from "../src/lib/profile";
import {
  loadFromTimeline, loadStack, nextRun, percent, recordedLoad,
  tableFromTimeline, windowAt,
} from "../src/lib/rtos";
import { FakeConn } from "./fake-conn";

describe("RTOS: 表と負荷の推移", () => {
  it("取った時間軸の要約を rtos.timeline.get の順のまま表に（画面で使用率の順に並べない。b16）", () => {
    const t = tableFromTimeline({ timebase_hz: 1e7, t0: 0, t1: 100, lower_bound: true,
      stats: { covered: 90, tasks: { main: { handle: 1, cpu: 0.3, runs: 3, total: 27, max_run: 10, min_run: 8, mean_run: 9 },
                                     IDLE: { handle: 5, cpu: 0.7, runs: 2, total: 63, max_run: 40, min_run: 23, mean_run: 31.5 } } } });
    expect(t.tasks.map((x) => x.task)).toEqual(["main", "IDLE"]);
    expect(t.tasks[0]).toMatchObject({ handle: 1, runs: 3, max: 10, min: 8, mean: 9 });
    expect(t).toMatchObject({ hz: 1e7, covered: 90, lowerBound: true });
    expect(loadFromTimeline({ window: 10, windows: [], peak: null, mean_load: 0.2 }).meanLoad).toBe(0.2);
  });

  it("時刻を含む窓と割合の文字", () => {
    const s = loadFromTimeline({ window: 100, mean_load: 0.5, peak: null, idle_tasks: [], windows: [
      { t0: 0, t1: 100, covered: 100, load: 0.5, isr: 0, other: 0, tasks: {} },
      { t0: 100, t1: 200, covered: 100, load: 0.5, isr: 0, other: 0, tasks: {} }] });
    expect(windowAt(s, 150)?.t0).toBe(100);
    expect(windowAt(s, 250)).toBeNull();
    expect(percent(0.1234)).toBe("12.3%");
    expect(percent(null)).toBe("—");
  });

  it("CPU 負荷の積み上げ: タスク（表の並び = デーモンの順）・割り込み・その他（デーモンの other）で、上端が負荷", () => {
    const s = loadFromTimeline({ window: 100, mean_load: 0.5, peak: null, idle_tasks: ["IDLE"], windows: [
      { t0: 0, t1: 100, covered: 100, load: 0.6, isr: 0.1, other: 0.1, tasks: { main: 0.3, net: 0.1, IDLE: 0.4 } },
      { t0: 100, t1: 200, covered: 0, load: null, isr: null, other: null, tasks: {} },
      { t0: 200, t1: 300, covered: 100, load: 0.5, isr: 0, other: 0, tasks: { main: 0.5, IDLE: 0.5 } },
    ] });
    // 並びは渡した順（画面で多い順に並べ替えない）。アイドルは積まない
    const stack = loadStack(s, ["net", "IDLE", "main"], (t) => t / 100, { isr: "ISR", other: "Other" }, null);
    expect(stack.map((x) => x.name)).toEqual(["net", "main", "ISR", "Other"]);
    expect(stack[0].x).toEqual([0, 1, 2]);
    const tops = stackTops(stack);
    expect(tops[3][0]).toBeCloseTo(60);                    // 上端 = 負荷
    expect(tops[3][2]).toBeCloseTo(50);
    expect(Number.isNaN(tops[3][1])).toBe(true);
    expect(loadStack(s, ["main", "net"], (t) => t, { isr: "ISR", other: "Other" }, "main").map((x) => !!x.dim))
      .toEqual([false, true, true, true]);
    // その他はデーモンが数えた値そのもの（0 なら出さない。画面で引き算して丸めの閾値を持たない）
    const none = loadFromTimeline({ window: 100, mean_load: 0.5, peak: null, idle_tasks: ["IDLE"], windows: [
      { t0: 0, t1: 100, covered: 100, load: 0.5, isr: 0, other: 0, tasks: { main: 0.5, IDLE: 0.5 } }] });
    expect(loadStack(none, ["main"], (t) => t, { isr: "ISR", other: "Other" }).map((x) => x.name)).toEqual(["main"]);
  });

  it("記録から: 表と推移はデーモン（recording.cpu_load）の数えたまま。窓を切ったら全体の数と上限", async () => {
    const conn = new FakeConn({
      "recording.cpu_load": { timebase_hz: 1e7, t0: 0, t1: 200, lower_bound: false,
        stats: { covered: 140, tasks: { main: { handle: 1, cpu: 40 / 140, runs: 2, total: 40, max_run: 30, min_run: 10, mean_run: 20 },
                                        IDLE: { handle: 5, cpu: 100 / 140, runs: 1, total: 100, max_run: 100, min_run: 100, mean_run: 100 } } },
        load: { window: 100, windows: [{ t0: 0, t1: 100, covered: 100, load: 0.3, isr: 0, other: 0, tasks: {} }], windows_total: 2,
                limit: 1, peak: null, mean_load: 0.3, idle_tasks: ["IDLE"] } },
      "recording.find": { count: 2, events: [{ t: 300, t_end: 340 }, { t: 120, t_end: 150 }] },
    });
    const r = (await recordedLoad(conn as any, 10))!;
    expect(r.table.tasks.map((x) => x.task)).toEqual(["main", "IDLE"]);   // デーモンの順のまま
    expect(r.table).toMatchObject({ t0: 0, t1: 200, covered: 140, hz: 1e7 });
    expect(r.series?.idle).toEqual(["IDLE"]);
    expect(r.windowsCut).toEqual({ shown: 1, total: 2, limit: 1 });
    expect(conn.calls[0]).toEqual(["recording.cpu_load", { window_us: 10 }]);
    expect(await nextRun(conn as any, 1, 100)).toEqual({ t: 120, tEnd: 150 });
    expect(conn.calls.find(([m]) => m === "recording.find")![1]).toEqual({ query: { stream: "rtos", kind: "run", a: 1, t0: 101 } });
    expect(conn.quiet.length).toBe(conn.calls.length);       // 画面の自動の読みは quiet
    const noRuns = new FakeConn({ "recording.cpu_load": { timebase_hz: 1e7, stats: null } });
    expect(await recordedLoad(noRuns as any, null)).toBeNull();
    expect(noRuns.calls[0]).toEqual(["recording.cpu_load", {}]);         // 窓の幅が無ければ推移は頼まない
  });
});

describe("Code Profile", () => {
  it("選んだ区間は基準と選択のカーソルの間（記録・方法 pcsr だけ）", () => {
    const s = { source: "recording" as const, selection: 500, reference: 200 };
    expect(selectedRange(s, "pcsr")).toEqual({ t0: 200, t1: 500 });
    expect(selectedRange(s, "itm")).toEqual({ reason: "itm" });
    expect(selectedRange({ ...s, reference: null }, "pcsr")).toEqual({ reason: "cursors" });
    expect(selectedRange({ ...s, source: "capture" }, "pcsr")).toEqual({ reason: "cursors" });
  });

  it("ソースの余白: ファイル名ごとの行の当たりと、当たらなかった行", () => {
    const heat = heatOf(
      [{ function: "tick", file: "C:/src/sram_demo.c", line: 17, samples: 30, fraction: 0.3 },
       { function: "tick", file: "sram_demo.c", line: 18, samples: 10, fraction: 0.1 }],
      [{ function: "tick", lines: 4, lines_hit: 2, line_fraction: 0.5, hit_lines: [["sram_demo.c", 17], ["sram_demo.c", 18]],
         missed_lines: [["sram_demo.c", 19], ["sram_demo.c", 21]] }]);
    expect(heat.lines.get("sram_demo.c")!.get(17)).toEqual({ fraction: 0.3, samples: 30, max: 30 });
    expect([...heat.missed.get("sram_demo.c")!]).toEqual([19, 21]);
    expect(baseName("a\\b\\c.c")).toBe("c.c");
    let seen = 0;
    const stop = sourceHeat.subscribe((s) => (seen = s.lines.size));
    sourceHeat.set(heat);
    expect(seen).toBe(1);
    sourceHeat.clear();
    expect(seen).toBe(0);
    stop();
  });

  it("関数名の番地は debug.symbol.info で 1 度だけ引く", async () => {
    const conn = new FakeConn({ "debug.symbol.info": (p: any) => (p.location === "tick" ? { address: 0x100 } : (() => { throw new Error("no"); })()) });
    const s = new SymbolAddresses(conn as any);
    expect(await s.of("tick+4")).toBe(0x100);
    expect(await s.of("tick")).toBe(0x100);
    expect(await s.of("nothing")).toBeNull();
    expect(conn.calls.length).toBe(2);
  });
});

describe("Interrupts", () => {
  it("SWO の例外トレースの結果も同じ表に（秒）", () => {
    const rows = statsFromProfile({ clock_hz: 100, exceptions: [{ exception: 15, name: "SysTick", count: 3, total_inclusive: 30,
      total_self: 24, inclusive: { min: 5, max: 15, mean: 10 }, load: 0.1, max_at: 700, max_end: 715 }] });
    expect(rows[0]).toMatchObject({ number: 15, total: 0.3, self: 0.24, min: 0.05, max: 0.15, mean: 0.1, maxAt: 700 });
  });

  const dist = { number: 15, bins: 3, longest_limit: 2,
    histogram: [{ lo: 10, hi: 20, count: 1, longest: { t: 0, t_end: 10, inclusive: 10 } },
                { lo: 20, hi: 30, count: 0, longest: null },
                { lo: 30, hi: 40, count: 2, longest: { t: 300, t_end: 340, inclusive: 40 } }],
    longest: [{ t: 300, t_end: 340, inclusive: 40, bin: 2 }, { t: 200, t_end: 230, inclusive: 30, bin: 2 }] };

  it("記録の割り込みはデーモン（recording.interrupts）が数えたまま。分布は API の箱・上位・箱ごとの最長", async () => {
    const conn = new FakeConn({ "recording.interrupts": (p: any) => ({ timebase_hz: 10, exceptions: [{ exception: 15,
      name: "SysTick", count: 1, total_inclusive: 10, total_self: 10, inclusive: { min: 10, max: 10, mean: 10 }, load: 0.01,
      max_at: 10, max_end: 20 }], ...(p.number !== undefined ? { distribution: dist } : {}), ...(p.at !== undefined
      ? { at_numbers: [16, 15] } : {}) }) });
    const r = await recordedIsrs(conn as any, null);
    expect(r.hz).toBe(10);
    expect(r.stats[0]).toMatchObject({ name: "SysTick", count: 1, total: 1, load: 0.01 });
    expect(r.histogram).toBeNull();
    const h = (await recordedIsrs(conn as any, { number: 15, bins: 3, longest: 2 })).histogram!;
    expect(conn.calls.at(-1)).toEqual(["recording.interrupts", { number: 15, bins: 3, longest: 2 }]);
    expect(h.bars.map((b) => [b.x0, b.y, b.mark])).toEqual([[10, 1, false], [20, 0, false], [30, 2, true]]);
    expect(h.longest.map((a) => a.t)).toEqual([300, 200]);
    expect(longestIn(h, 2)?.t).toBe(300);
    expect(longestIn(h, 1)).toBeNull();
    expect(longestIn(h, null)).toBeNull();
    expect(await isrAt(conn as any, 205)).toBe(15);                      // いちばん内側 = 実行中
  });

  it("時間の単位が分からなければ秒にしない（1 で割って秒として出さない）", () => {
    expect(statsFromProfile({ exceptions: [{ exception: 15, name: "S", count: 1, total_inclusive: 10, total_self: 10,
      inclusive: { min: 10, max: 10, mean: 10 } }] })[0]).toMatchObject({ total: 10 });
    expect(profileHz({ clock_hz: 16e6 })).toBe(16e6);
    expect(profileHz({ timebase_hz: 1e6 })).toBe(1e6);
    expect(profileHz({})).toBeNull();
  });
});

describe("Call Graph", () => {
  const fns: StackFunction[] = [
    { function: "reset", frame: 8, frame_source: "disasm", max: 40, bounded: true, reasons: [], unknown_from: [],
      path: ["reset", "a", "b"], calls: ["a", "c"] },
    { function: "a", frame: 16, frame_source: "su", max: 32, bounded: true, reasons: [], unknown_from: [], path: ["a", "b"],
      calls: ["b"] },
    { function: "b", frame: 16, frame_source: "su", max: 16, bounded: true, reasons: [], unknown_from: [], path: ["b"],
      calls: [] },
    { function: "c", frame: 8, frame_source: "su", max: 8, bounded: false, reasons: ["再帰: c → c"], unknown_from: [],
      path: ["c"], calls: ["c"] },
  ];
  const roots = rootsOf({ roots: [{ root: "reset", kind: "reset", max: 40, bounded: true, path: ["reset", "a", "b"] },
                                   { root: "h", kind: "exception", max: 4, bounded: true, path: ["h"] }] });

  it("根は API の並びのまま（画面で種類・大きさの順に並べ替えない）。重複は除く", () => {
    const api = [{ root: "u", kind: "uncalled", max: 99, bounded: true, path: ["u"] },
                 { root: "h", kind: "exception", max: 4, bounded: true, path: ["h"] },
                 { root: "h", kind: "exception", max: 4, bounded: true, path: ["h"] }];
    expect(rootsOf({ roots: api }).map((r) => r.root)).toEqual(["u", "h"]);
  });

  it("根は API の並び、木は開いた所だけ、再帰はそれ以上開かない", () => {
    expect(roots.map((r) => r.root)).toEqual(["reset", "h"]);
    expect(treeRows(fns, roots, new Set()).map((r) => r.name)).toEqual(["reset", "h"]);
    const rows = treeRows(fns, roots, new Set(["reset", "reset/a", "reset/c", "reset/c/c"]));
    expect(rows.map((r) => `${r.depth}${r.name}`)).toEqual(["0reset", "1a", "2b", "1c", "2c", "0h"]);
    expect(rows.find((r) => r.key === "reset/c/c")).toMatchObject({ recursive: true, hasChildren: false });
    expect(rows.filter((r) => r.deepest).map((r) => r.key)).toEqual(["reset/a", "reset/a/b"]);    // 根の下の経路
    expect(rows.find((r) => r.name === "h")!.fn).toBeNull();
    expect(deepestKeys(roots[0])).toEqual(["reset", "reset/a", "reset/a/b"]);
  });
});

describe("RTOS Objects の種類のタブ", () => {
  it("タブは API の kind そのもの（名前の接尾辞で種類をまとめない。知らない RTOS の型も自分のタブ）", async () => {
    const { objectKinds, objectsOfKind } = await import("../src/lib/rtos");
    const objects = [{ kind: "k_msgq" }, { kind: "queue" }, { kind: "my_rtos_mailbox" }, { kind: "queue" }, { kind: "k_sem" }];
    expect(objectKinds(objects)).toEqual([
      { kind: "all", count: 5 }, { kind: "k_msgq", count: 1 }, { kind: "k_sem", count: 1 },
      { kind: "my_rtos_mailbox", count: 1 }, { kind: "queue", count: 2 }]);
    expect(objectsOfKind(objects, "queue")).toEqual([{ kind: "queue" }, { kind: "queue" }]);
    expect(objectsOfKind(objects, "all")).toHaveLength(5);
    expect(objectsOfKind(objects, "mutex")).toEqual([]);
  });
});
