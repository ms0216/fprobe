// 共通タイムラインの段（G01）: 段ごとの間引き（件数・値の範囲）が Python と同じ（固定データ rows.json は
// host/tests/ui_fixture.py が作る）。段の描き方（compose）、段の表と構成、拾う・次の出来事、表示の名前。
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { FLAG, MODE, composeFrame, valueRanges, type RowSpec } from "../src/lib/timeline/compose";
import { Pixels, countsOf, summaryEnvelope, summaryRowCounts, valueEnvelope } from "../src/lib/timeline/decimate";
import { decodeFrame } from "../src/lib/timeline/frame";
import { alignFrame, cssHeight, rowAtY, RULER } from "../src/lib/timeline/layout";
import { anomalies, pick, seek } from "../src/lib/timeline/query";
import {
  EMPTY_CONFIG, ROW_HEIGHT, RowRegistry, isShown, moveGroup, moveRow, parseConfig, setShown, toggleFold,
} from "../src/lib/timeline/rows";
import { fromSections, type Tile } from "../src/lib/timeline/tile";
import { rowTitle } from "../src/lib/timeline/titles";
import { TIMELINE_OPEN, announceOpen } from "../src/lib/timeline/events";
import { t as tr } from "../src/lib/i18n";

const dir = resolve(import.meta.dirname, "fixtures");
const fixture = JSON.parse(readFileSync(resolve(dir, "rows.json"), "utf8"));

function load(name: string): Tile {
  const bytes = readFileSync(resolve(dir, name));
  const buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
  const message = decodeFrame(buf);
  return fromSections(message.result, message.result.data);
}

const pair = (lo: Float64Array, hi: Float64Array) =>
  Array.from(lo, (x, i) => (Number.isNaN(x) ? null : [x, hi[i]]));

describe("段ごとの間引きが Python と同じ（固定データ）", () => {
  for (const c of fixture.cases) {
    it(c.name, () => {
      const tile = load(c.frame);
      for (const v of c.views) {
        const px = new Pixels(v.v0, v.v1, v.width);
        const counts = new Uint32Array(v.width), lo = new Float64Array(v.width), hi = new Float64Array(v.width);
        for (const [id, expected] of Object.entries<any[]>(v.rows)) {
          const at = tile.rowAt.get(id);
          expect(at, id).toBeDefined();
          const lane = tile.lanes[at!.lane], values = lane.rows[at!.row].type === "values";
          const where = `${id} ${v.v0}..${v.v1} / ${v.width}`;
          if (lane.mode === "raw") {
            if (values) {
              valueEnvelope(tile, lane.byRow[at!.row], px, lo, hi);
              const got = pair(lo, hi);
              expected.forEach((e, i) => {
                if (e === null) expect(got[i], `${where} px ${i}`).toBeNull();
                else expect(got[i]![0], `${where} px ${i}`).toBeCloseTo(e[0], 9);
              });
              expect(got.map((g) => g && g[1]), where).toEqual(expected.map((e) => e && expect.closeTo(e[1], 9)));
            } else {
              countsOf(tile, lane.byRow[at!.row].t, px, counts);
              expect(Array.from(counts), where).toEqual(expected);
            }
          } else if (values) {
            summaryEnvelope(lane, at!.row, px, lo, hi);
            expect(pair(lo, hi), where).toEqual(expected.map((e) => e && [expect.closeTo(e[0], 9), expect.closeTo(e[1], 9)]));
          } else {
            summaryRowCounts(lane, at!.row, px, counts);
            expect(Array.from(counts), where).toEqual(expected);
          }
        }
      }
    });
  }
});

const SPECS: RowSpec[] = [
  { id: "group:rtos", type: "group", color: 0 },
  { id: "rtos.task:0x00000100", type: "intervals", color: 0 },
  { id: "rtos.isr:0x00000036", type: "intervals", color: 1 },
  { id: "rtos.isr:0x0000000F", type: "intervals", color: 2 },
  { id: "uart.tx", type: "bus", color: 3 },
  { id: "sample.word:0x20000010", type: "values", color: 4 },
  { id: "target.state", type: "state", color: 5 },
  { id: "annotation.mark", type: "points", color: 6 },
  { id: "nothing.here", type: "points", color: 7 },
];

describe("段の描き方（compose）", () => {
  const tile = load("rows-raw.bin");
  const W = 500;

  it("生の段は範囲の前の値を探した始めと、探し切ったかを持つ（黙って打ち切らない）", () => {
    for (const lane of tile.lanes) {
      if (lane.mode !== "raw") continue;
      expect(typeof lane.carryComplete).toBe("boolean");
      expect(lane.carryFrom === null || typeof lane.carryFrom === "number").toBe(true);
    }
  });

  it("段の型ごとの描き方。知らない段は描かない", () => {
    const f = composeFrame(tile, 0, 20000, W, SPECS);
    expect(Array.from(f.modes)).toEqual([MODE.header, MODE.intervals, MODE.intervals, MODE.intervals, MODE.bus,
                                         MODE.values, MODE.state, MODE.points, MODE.none]);
    const scale = f.scales[5]!;
    expect(scale[0]).toBeLessThan(scale[1]);
    expect(f.ids).toEqual(SPECS.map((s) => s.id));
  });

  it("区間: 色・始めの印・入れ子の深さと外側の色、幅の広い区間にラベル", () => {
    const f = composeFrame(tile, 150, 250, W, SPECS);                // USART2 [150, 210) の中に SysTick [160, 180)?
    const px = (row: number, p: number) => f.data.subarray((row * W + p) * 4, (row * W + p) * 4 + 4);
    const usart = px(2, 10);
    expect(usart[0]).toBe(2);                                        // 色 1 + 1
    expect(px(2, 0)[1] & FLAG.start).toBeTruthy();
    const systick = tile.rowAt.get("rtos.isr:0x0000000F");
    if (systick) {
      const lane = tile.lanes[systick.lane];
      if (lane.mode === "raw" && lane.byRow[systick.row].t[0] === 160) {
        const inner = px(3, 75);                                     // 172 の SysTick は深さ 1
        expect(inner[2]).toBe(1);
      }
    }
    expect(f.labels.some((l) => l.text === "USART2")).toBe(true);
  });

  it("状態: 視点の前に変わった状態も視点の始めから描く（次の変化まで）", () => {
    const f = composeFrame(tile, 5000, 6000, W, SPECS);              // 4600 から RUNNING
    const row = 6;
    expect(f.data[(row * W + 0) * 4]).toBeGreaterThan(0);
    expect(f.data[(row * W + W - 1) * 4]).toBeGreaterThan(0);
    expect(f.labels.find((l) => l.row === row)?.text).toBe("RUNNING");
  });

  it("印: 画素に印、間が空いていればラベル。多すぎる段は件数（密度）で", () => {
    const f = composeFrame(tile, 0, 20000, W, SPECS);
    const marks = Array.from({ length: W }, (_, p) => f.data[(7 * W + p) * 4 + 1] & FLAG.mark).filter(Boolean).length;
    expect(marks).toBe(3);
    expect(f.labels.filter((l) => l.row === 7).map((l) => l.text)).toEqual(["mark 900", "mark 7777", "mark 18000"]);
    const narrow = composeFrame(tile, 0, 20000, 5, SPECS);            // 5 画素に数十の区間
    expect(narrow.modes[1]).toBe(MODE.density);
    expect(narrow.max[1]).toBeGreaterThan(1);
  });

  it("値の段の目盛りは見たタイルの値の範囲", () => {
    const ranges = valueRanges(tile);
    expect(ranges.get("sample.word:0x20000010")![0]).toBeGreaterThanOrEqual(0);
    expect(ranges.get("aux.voltage")).toEqual([3.11, 3.3]);
    const f = composeFrame(tile, 0, 20000, W, SPECS, 0, new Map([["sample.word:0x20000010", [0, 10000]]]));
    expect(f.scales[5]).toEqual([0, 10000]);
  });

  it("前のフレームを新しい段の並びに合わせる", () => {
    const f = composeFrame(tile, 0, 20000, W, SPECS);
    const g = alignFrame(f, ["annotation.mark", "new.row"]);
    expect(g.rows).toBe(2);
    expect(Array.from(g.modes)).toEqual([MODE.points, MODE.none]);
    expect(Array.from(g.data.subarray(0, W * 4))).toEqual(Array.from(f.data.subarray(7 * W * 4, 8 * W * 4)));
    expect(alignFrame(f, f.ids)).toBe(f);
  });
});

describe("拾う・次の出来事・異常の印", () => {
  const tile = load("rows-raw.bin");

  it("区間はその時刻を含むもの（入れ子は内側）、印は近いもの、値と状態は保たれているもの", () => {
    const task = pick(tile, "rtos.task:0x00000100", 5, 0)!;
    expect(task).toMatchObject({ type: "intervals", label: "main", t: 0, stream: "rtos", kind: "run" });
    const mark = pick(tile, "annotation.mark", 905, 10)!;
    expect(mark).toMatchObject({ t: 900, label: "mark 900", tone: 4 });   // 色の意味 = 事実の種類 mark
    expect(pick(tile, "annotation.mark", 950, 10)).toBeNull();
    const state = pick(tile, "target.state", 4300, 0)!;
    expect(state).toMatchObject({ label: "HALTED", t: 4000, tEnd: 4600 });
    const v = pick(tile, "aux.voltage", 250, 0)!;
    expect(v).toMatchObject({ t: 100, value: 3.3, unit: "V" });
    const w = pick(tile, "sample.word:0x20000010", 1, 0)!;
    expect(w.address).toBe(0x20000010);
    expect(w.jump).toBe("memory");
  });

  it("次 / 前の出来事", () => {
    expect(seek(tile, ["annotation.mark"], 900, 1)).toEqual({ t: 7777, row: "annotation.mark" });
    expect(seek(tile, ["annotation.mark"], 900, -1)).toBeNull();
    expect(seek(tile, ["annotation.mark", "target.state"], 4100, 1)).toEqual({ t: 4600, row: "target.state" });
    expect(seek(tile, ["annotation.mark", "target.state"], 4100, -1)).toEqual({ t: 4000, row: "target.state" });
  });

  it("縮図の異常の印は誤り・注意の出来事", () => {
    const found = anomalies(tile, 2000);
    expect(found.map((a) => a.t)).toEqual(expect.arrayContaining([900, 4000, 7777, 15000, 18000]));
    expect(found.every((a) => a.tone === 1 || a.tone === 3)).toBe(true);
  });

  it("縮図の異常の印を減らすときは時間の箱ごとに残し、離れた 1 つ・注意の色を落とさない（等間隔に間引かない）", () => {
    const n = 3000;
    const t = new Float64Array([...Array.from({ length: n }, (_, i) => i), 999_000, 999_500]);
    const tone = new Uint8Array([...Array.from({ length: n }, () => 1), 1, 3]);
    const fake = { t0: 0, t1: 1_000_000, lanes: [{ mode: "raw", t, tone }] } as any;
    const found = anomalies(fake, 100);
    expect(found.length).toBeLessThanOrEqual(2 * 100);
    expect(found.some((a) => a.t === 999_000 && a.tone === 1)).toBe(true);    // 離れた誤り
    expect(found.some((a) => a.t === 999_500 && a.tone === 3)).toBe(true);    // 同じ箱の注意も
    expect(found.some((a) => a.t === 0)).toBe(true);
  });
});

describe("他のパネルへの知らせ", () => {
  it("飛び先のある出来事だけ開く", () => {
    const tile = load("rows-raw.bin");
    const opened: unknown[] = [];
    const target = new EventTarget();
    (globalThis as any).window = target;
    target.addEventListener(TIMELINE_OPEN, (e) => opened.push((e as CustomEvent).detail));
    try {
      expect(announceOpen(pick(tile, "annotation.mark", 900, 0), 900)).toBe(false);
      expect(announceOpen(pick(tile, "sample.word:0x20000010", 1, 0), 1)).toBe(true);
      expect(opened).toMatchObject([{ kind: "memory", address: 0x20000010, t: 1 }]);
    } finally {
      delete (globalThis as any).window;
    }
  });
});

describe("段の表と構成", () => {
  const tile = load("rows-raw.bin");

  it("タイルから段を知り、まとまりの見出しと段を並べる。色はまとまりの中で順に", () => {
    const reg = new RowRegistry();
    expect(reg.merge(tile.header)).toBe(true);
    expect(reg.merge(tile.header)).toBe(false);
    const visible = reg.visible(EMPTY_CONFIG);
    expect(visible[0]).toMatchObject({ header: true, def: { id: "group:annotation" } });
    const rtos = visible.filter((r) => r.def.group === "rtos" && !r.header);
    expect(rtos.map((r) => r.def.color)).toEqual(rtos.map((_, i) => i));
    expect(cssHeight(visible)).toBe(RULER + visible.reduce((a, r) => a + ROW_HEIGHT[r.def.type], 0) + 4);
    expect(rowAtY(visible, RULER + 1)).toBe(0);
    expect(rowAtY(visible, 1)).toBe(-1);
  });

  it("折りたたみ・隠す・並べ替え・覚えた構成の読み直し", () => {
    const reg = new RowRegistry();
    reg.merge(tile.header);
    let c = toggleFold(EMPTY_CONFIG, "rtos");
    expect(reg.visible(c).filter((r) => r.def.group === "rtos").map((r) => r.header)).toEqual([true]);
    c = toggleFold(c, "rtos");
    const groups = reg.ordered(c);
    const rtos = groups.find((g) => g.id === "rtos")!.rows;
    c = setShown(c, rtos[0], false);
    expect(isShown(rtos[0], c)).toBe(false);
    expect(reg.visible(c).some((r) => r.def.id === rtos[0].id)).toBe(false);
    c = moveRow(c, groups, rtos[2].id, rtos[1].id);
    expect(reg.ordered(c).find((g) => g.id === "rtos")!.rows.map((r) => r.id).slice(1, 3))
      .toEqual([rtos[2].id, rtos[1].id]);
    c = moveGroup(c, groups, "target", "annotation");
    expect(reg.ordered(c)[0].id).toBe("target");
    expect(parseConfig(JSON.stringify(c))).toEqual(c);
    expect(parseConfig("{broken")).toEqual(EMPTY_CONFIG);
    expect(parseConfig(JSON.stringify({ hidden: [1, "x"], rows: { a: "no" } }))).toMatchObject({ hidden: ["x"], rows: { a: [] } });
  });

  it("既定で隠す段は、見せると覚える", () => {
    const reg = new RowRegistry();
    reg.merge({ ...tile.header, lanes: [{ stream: "rtos", mode: "raw", count: 0,
                                          rows: [{ id: "rtos.switch", rule: "switch", type: "points", title: null, hidden: true }] }] });
    const def = reg.ordered(EMPTY_CONFIG).find((g) => g.id === "rtos")!.rows[0];
    expect(isShown(def, EMPTY_CONFIG)).toBe(false);
    expect(isShown(def, setShown(EMPTY_CONFIG, def, true))).toBe(true);
  });

  it("表示の名前: データの名前、規則の名前、操作の段は利用者の種類", () => {
    const base = { group: "rtos", type: "intervals" as const, color: 0 };
    expect(rowTitle({ ...base, id: "rtos.task:0x1", rule: "task", title: "main" }, tr)).toBe("main");
    expect(rowTitle({ ...base, id: "rtos.blocked", rule: "blocked", title: null }, tr)).toBe("Blocked");
    expect(rowTitle({ ...base, group: "host", id: "host.call:ai", rule: "call", title: null }, tr)).toBe("AI");
    expect(rowTitle({ ...base, group: "custom", id: "custom.kind:alpha", rule: "kind", title: null }, tr)).toBe("kind alpha");
    expect(rowTitle({ ...base, type: "group", group: "sample", id: "group:sample", title: null }, tr)).toBe("Data Sampling");
  });
});

describe("値の段の値なしの点", () => {
  it("値なし（NaN）の点の後は、次の点まで空白（前の値を伸ばさない。MCU のメモリの読みの空白・読めなかった語）", () => {
    // 点: 0 → 5、10 → 6、20 → 値なし（読みの空白の始め）、80 → 7。画素 10 個 × 10
    const ev = { t: new Float64Array([0, 10, 20, 80]), value: new Float64Array([5, 6, NaN, 7]) };
    const tile = { t0: 0, t1: 100 } as unknown as Tile;
    const px = new Pixels(0, 100, 10);
    const lo = new Float64Array(10), hi = new Float64Array(10);
    valueEnvelope(tile, ev as never, px, lo, hi);
    const got = Array.from(lo, (v, i) => (Number.isNaN(v) ? null : [v, hi[i]]));
    expect(got).toEqual([[5, 5], [5, 6], [6, 6], null, null, null, null, null, [7, 7], [7, 7]]);  // 画素 1・2 は始めに保たれている値も含む
  });
});
