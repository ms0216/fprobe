// RTOS・Code Profile・Interrupts・Call Graph のビューの計算（lib/rtos.ts・profile.ts・interrupts.ts・callgraph.ts）
import { describe, expect, it } from "vitest";
import { stackTops } from "../src/lib/chart";
import { deepestKeys, rootsOf, treeRows, type StackFunction } from "../src/lib/callgraph";
import { activations, histogram, longestIn, recordedIsrs, statsFromProfile, statsOf } from "../src/lib/interrupts";
import { SymbolAddresses, baseName, heatOf, selectedRange, sourceHeat } from "../src/lib/profile";
import {
  busyTasks, isIdle, subtract, loadFromTimeline, loadStack, loadWindows, nextRun, overlap, percent, recordedRuns, recordedTable, runningAt,
  tableFromTimeline, union, windowAt,
} from "../src/lib/rtos";
import { FakeConn } from "./fake-conn";

describe("RTOS: 表と負荷の推移", () => {
  it("取った時間軸の要約を使用率の順の表に", () => {
    const t = tableFromTimeline({ timebase_hz: 1e7, t0: 0, t1: 100, lower_bound: true,
      stats: { covered: 90, tasks: { IDLE: { handle: 5, cpu: 0.7, runs: 2, total: 63, max_run: 40, min_run: 23, mean_run: 31.5 },
                                     main: { handle: 1, cpu: 0.3, runs: 3, total: 27, max_run: 10, min_run: 8, mean_run: 9 } } } });
    expect(t.tasks.map((x) => x.task)).toEqual(["IDLE", "main"]);
    expect(t).toMatchObject({ hz: 1e7, covered: 90, lowerBound: true });
    expect(t.tasks[1]).toMatchObject({ handle: 1, runs: 3, max: 10, min: 8, mean: 9 });
    expect(loadFromTimeline({ window: 10, windows: [], peak: null, mean_load: 0.2 }).meanLoad).toBe(0.2);
  });

  it("区間の和集合と重なり", () => {
    expect(union([[5, 8], [0, 2], [1, 3], [8, 9], [4, 4]])).toEqual([[0, 3], [5, 9]]);
    expect(overlap([[0, 3], [5, 9]], 2, 6)).toBe(2);
    expect(overlap([[0, 3], [5, 9]], 10, 20)).toBe(0);
    expect(subtract([[0, 10], [20, 30]], [[2, 4], [8, 22]])).toEqual([[0, 2], [4, 8], [22, 30]]);
  });

  it("窓ごとの負荷はデーモンと同じ定義（割り込みはタスクから除き負荷に入る、抜けは分母から除く）", () => {
    const runs: [number, number, number, string][] = [[0, 30, 1, "main"], [30, 100, 5, "IDLE"], [100, 160, 1, "main"],
                                                       [160, 200, 5, "IDLE"]];
    const s = loadWindows({ runs, isrs: [[10, 20, 15, "SysTick"], [12, 14, 54, "USART2"]], gaps: [[180, 200]] }, 100);
    expect(s.windows).toHaveLength(2);
    const [a, b] = s.windows;
    expect(a).toMatchObject({ t0: 0, t1: 100, covered: 100 });
    expect(a.tasks.main).toBeCloseTo(0.2);                 // 30 − 割り込み 10
    expect(a.isr).toBeCloseTo(0.1);
    expect(a.load).toBeCloseTo(0.3);                       // 1 − アイドル 70 / 100
    expect(b.covered).toBe(80);
    expect(b.load).toBeCloseTo(1 - 20 / 80);              // 抜けの中のアイドルは数えない
    expect(s.peak).toEqual({ t0: 100, t1: 200, load: 0.75 });
    expect(s.meanLoad).toBeCloseTo((0.3 * 100 + 0.75 * 80) / 180);
    expect(windowAt(s, 150)?.t0).toBe(100);
    expect(windowAt(s, 250)).toBeNull();
    expect(busyTasks(s)).toEqual(["main"]);
    expect(loadWindows({ runs: [], isrs: [], gaps: [] }, 10).windows).toEqual([]);
    // 実行の区間の無い間（止まっていた）は情報が無い: 負荷は null（100% にしない）
    const halted = loadWindows({ runs: [[0, 50, 5, "IDLE"], [150, 200, 5, "IDLE"]], isrs: [], gaps: [] }, 100);
    expect(halted.windows.map((w) => w.covered)).toEqual([50, 50]);
    expect(halted.windows.map((w) => w.load)).toEqual([0, 0]);
    expect(loadWindows({ runs: [[0, 50, 5, "IDLE"], [250, 300, 1, "main"]], isrs: [], gaps: [] }, 100).windows[1].load)
      .toBeNull();
    expect(isIdle("IDLE") && isIdle("idle 00") && !isIdle("idler")).toBe(true);
    expect(percent(0.1234)).toBe("12.3%");
    expect(percent(null)).toBe("—");
  });

  it("CPU 負荷の積み上げ: タスク（多い順）・割り込み・その他で、上端が負荷。情報の無い窓は切る", () => {
    const s = loadFromTimeline({ window: 100, mean_load: 0.5, peak: null, windows: [
      { t0: 0, t1: 100, covered: 100, load: 0.6, isr: 0.1, tasks: { main: 0.3, net: 0.1, IDLE: 0.4 } },
      { t0: 100, t1: 200, covered: 0, load: null, isr: null, tasks: {} },
      { t0: 200, t1: 300, covered: 100, load: 0.5, isr: 0, tasks: { main: 0.5, IDLE: 0.5 } },
    ] });
    const stack = loadStack(s, (t) => t / 100, { isr: "ISR", other: "Other" }, null, 1);
    expect(stack.map((x) => x.name)).toEqual(["main", "ISR", "Other"]);
    expect(stack[0].x).toEqual([0, 1, 2]);
    const tops = stackTops(stack);
    expect(tops[2][0]).toBeCloseTo(60);                    // 上端 = 負荷
    expect(tops[2][2]).toBeCloseTo(50);
    expect(Number.isNaN(tops[2][1])).toBe(true);
    expect(loadStack(s, (t) => t, { isr: "ISR", other: "Other" }, "main").map((x) => !!x.dim))
      .toEqual([false, true, true, true]);
  });

  it("記録から: 表は SQL の集計、区間は SQL の行、選んだ時刻のタスク・次の実行は find", async () => {
    const conn = new FakeConn({
      "recording.query": (p: any) => {
        if (p.sql.includes("GROUP BY a")) return { rows: [[1, "main", 2, 40, 30, 10, 0, 200, 1e7], [5, "IDLE", 1, 100, 100, 100, 50, 150, 1e7]] };
        if (p.sql.includes(":t")) return { rows: p.params.t < 50 ? [[1, "main", 0, 30]] : [] };
        return { rows: [], truncated: false };
      },
      "recording.find": { count: 2, events: [{ t: 300, t_end: 340 }, { t: 120, t_end: 150 }] },
    });
    const t = (await recordedTable(conn as any))!;
    expect(t.tasks[0]).toMatchObject({ task: "IDLE", cpu: 100 / 140 });     // 分母は実行の区間の合計
    expect(t.tasks[1]).toMatchObject({ task: "main", mean: 20, max: 30, min: 10 });
    expect(t).toMatchObject({ t0: 0, t1: 200, covered: 140, hz: 1e7 });
    expect(await runningAt(conn as any, 10)).toEqual({ handle: 1, task: "main" });
    expect(await runningAt(conn as any, 90)).toBeNull();
    expect(await nextRun(conn as any, 1, 100)).toEqual({ t: 120, tEnd: 150 });
    expect(conn.calls.find(([m]) => m === "recording.find")![1]).toEqual({ query: { stream: "rtos", kind: "run", a: 1, t0: 101 } });
    expect((await recordedRuns(conn as any)).truncated).toBe(false);
    expect(conn.quiet.length).toBe(conn.calls.length);       // 画面の自動の読みは quiet
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
      [{ function: "tick", file: "C:/src/sram_demo.c", line: 17, samples: 30, fraction: 0.3, ci: [0.2, 0.4] },
       { function: "tick", file: "sram_demo.c", line: 18, samples: 10, fraction: 0.1, ci: [0.05, 0.2] }],
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
  it("入れ子の回の自分の時間と統計（最長の回の時刻）", () => {
    const groups = activations([[0, 100, 54, "USART2"], [20, 30, 15, "SysTick"], [200, 205, 15, "SysTick"],
                                [300, 340, 54, "USART2"]]);
    expect(groups.get(54)!.acts[0]).toEqual({ t: 0, tEnd: 100, inclusive: 100, self: 90 });
    const stats = statsOf(groups, 10, 1000);
    expect(stats.map((s) => s.name)).toEqual(["USART2", "SysTick"]);
    expect(stats[0]).toMatchObject({ count: 2, total: 14, self: 13, min: 4, max: 10, mean: 7, load: 130 / 1000, maxAt: 0,
                                     maxEnd: 100 });
    expect(stats[1]).toMatchObject({ count: 2, max: 1, maxAt: 20 });
  });

  it("SWO の例外トレースの結果も同じ表に（秒）", () => {
    const rows = statsFromProfile({ clock_hz: 100, exceptions: [{ exception: 15, name: "SysTick", count: 3, total_inclusive: 30,
      total_self: 24, inclusive: { min: 5, max: 15, mean: 10 }, load: 0.1, max_at: 700, max_end: 715 }] });
    expect(rows[0]).toMatchObject({ number: 15, total: 0.3, self: 0.24, min: 0.05, max: 0.15, mean: 0.1, maxAt: 700 });
  });

  it("分布図: 外れ値の棒に印、棒の中の最長の回", () => {
    const acts = [...Array.from({ length: 20 }, (_, i) => ({ t: i * 10, tEnd: i * 10 + 5, inclusive: 5 + (i % 3), self: 5 })),
                  { t: 999, tEnd: 1049, inclusive: 50, self: 50 }];
    const h = histogram(acts, 10);
    expect(h.bars).toHaveLength(10);
    expect(h.bars[9].mark).toBe(true);
    expect(h.bars[0].mark).toBe(false);
    expect(h.outliers.map((a) => a.t)).toEqual([999]);
    expect(longestIn(h, 9)?.t).toBe(999);
    expect(longestIn(h, null)).toBeNull();
    expect(histogram([]).bars).toEqual([]);
    const few = histogram([3, 5, 5, 4].map((d, i) => ({ t: i, tEnd: i + d, inclusive: d, self: d })), 24);
    expect(few.bars.map((b) => [b.x0, b.y])).toEqual([[2.5, 1], [3.5, 1], [4.5, 2]]);     // 1 つの値に 1 本
  });

  it("記録の割り込みは SQL で読む", async () => {
    const conn = new FakeConn({ "recording.query": (p: any) => (p.sql.includes("MIN(t)") ? { rows: [[0, 1000, 10]] }
      : { rows: [[10, 20, 15, "SysTick"]], truncated: false }) });
    const r = await recordedIsrs(conn as any);
    expect(r.hz).toBe(10);
    expect(r.stats[0]).toMatchObject({ name: "SysTick", count: 1, total: 1, load: 10 / 1000 });
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
  const roots = rootsOf({ roots: [{ root: "h", kind: "exception", max: 4, bounded: true, path: ["h"] },
                                   { root: "reset", kind: "reset", max: 40, bounded: true, path: ["reset", "a", "b"] }] });

  it("根はリセット → 例外の順、木は開いた所だけ、再帰はそれ以上開かない", () => {
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
