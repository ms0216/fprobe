// 段の carry_complete = false（範囲の始めの状態・値を探し切れなかった）を、画面が描くだけで示す（総点検）。
// 画面は判断しない: API の carry_complete・carry_from と、段の最初の出来事の時刻をそのまま描く。
import { describe, expect, it } from "vitest";
import { composeFrame, unknownAt, type RowSpec } from "../src/lib/timeline/compose";
import type { Layout } from "../src/lib/timeline/layout";
import { drawOverlay } from "../src/lib/timeline/overlay";
import { fromSections, type TileHeader } from "../src/lib/timeline/tile";

const SPECS: RowSpec[] = [{ id: "st.state", type: "state", color: 0 }, { id: "st.value", type: "values", color: 1 },
                          { id: "st.mark", type: "points", color: 2 }];

/** 状態・値・印の段を持つ 1 つのレーン。times は段ごとの出来事の時刻（[state, value, mark]） */
function tile(carry: { from: number | null; complete: boolean } | null, times = [[1200], [1500], [1100]]) {
  const events = times.flatMap((ts, row) => ts.map((t) => ({ t, row }))).sort((a, b) => a.t - b.t);
  const header: TileHeader = {
    format: 2, source: "recording", t0: 1000, t1: 2000, tick_s: 1e-6, extent: [0, 2000],
    wave: { mode: "none", channels: [] }, sections: {},
    lanes: [{ stream: "st", mode: "raw", count: events.length, kinds: ["set"], labels: [],
              rows: [{ id: "st.state", rule: "a", type: "state", title: null },
                     { id: "st.value", rule: "b", type: "values", title: null },
                     { id: "st.mark", rule: "c", type: "points", title: null }],
              ...(carry ? { carry_from: carry.from, carry_complete: carry.complete } : {}) }],
  };
  const t = Float64Array.from(events.map((e) => e.t));
  return fromSections(header, {
    "lane.0.t": t, "lane.0.t_end": t.slice(), "lane.0.kind": new Uint16Array(events.length),
    "lane.0.row": Uint16Array.from(events.map((e) => e.row)),
    "lane.0.value": Float64Array.from(events.map((e) => (e.row === 1 ? 5 : NaN))),
  });
}

describe("範囲の始めの値を探し切れなかった段", () => {
  it("状態・値の段の、最初の出来事までを「不明」としてフレームに載せる（探した始め carry_from も）", () => {
    const f = composeFrame(tile({ from: 500, complete: false }), 1000, 2000, 100, SPECS);
    expect(f.unknown).toEqual([{ id: "st.state", x0: 0, x1: 20, carryFrom: 500 },
                               { id: "st.value", x0: 0, x1: 50, carryFrom: 500 }]);      // 印の段は始めの値を持たない
  });

  it("carry_complete の申告が無いタイルは探し切ったと仮定しない（不明として描く。欠けを隠さない）", () => {
    expect(composeFrame(tile(null), 1000, 2000, 100, SPECS).unknown).toEqual(
      [{ id: "st.state", x0: 0, x1: 20, carryFrom: null }, { id: "st.value", x0: 0, x1: 50, carryFrom: null }]);
  });

  it("探し切った・範囲の前の値が見つかったなら印は無い", () => {
    expect(composeFrame(tile({ from: 500, complete: true }), 1000, 2000, 100, SPECS).unknown).toEqual([]);
    const carried = tile({ from: 500, complete: false }, [[800, 1200], [700], [1100]]);
    expect(composeFrame(carried, 1000, 2000, 100, SPECS).unknown).toEqual([]);
  });

  it("出来事の無い段は見えている全体が不明。記録の始めより前は描かない", () => {
    const empty = tile({ from: 500, complete: false }, [[], [1500], [1100]]);
    const f = composeFrame(empty, -1000, 1000, 100, SPECS);
    expect(f.unknown).toEqual([{ id: "st.state", x0: 50, x1: 100, carryFrom: 500 },
                               { id: "st.value", x0: 50, x1: 100, carryFrom: 500 }]);
    expect(unknownAt(f, "st.state", 60)).toEqual(f.unknown[0]);
    expect(unknownAt(f, "st.state", 40)).toBeNull();
    expect(unknownAt(f, "st.mark", 60)).toBeNull();
  });

  it("重ねの絵: 不明の区間に印の文字を描く（無ければ描かない）", () => {
    const layout: Layout = { dpr: 1, width: 100, height: 100, ruler: 22, tops: [22, 40, 58], heights: [18, 18, 18],
                             ids: SPECS.map((s) => s.id), colors: [0, 1, 2] };
    const texts = (complete: boolean) => {
      const out: string[] = [];
      const g = new Proxy({} as Record<string, unknown>, {
        get: (o, k) => k in o ? o[k as string] : k === "measureText" ? () => ({ width: 6 })
          : k === "fillText" ? (s: string) => out.push(s) : () => undefined,
        set: (o, k, v) => { o[k as string] = v; return true; },
      });
      const canvas = { width: 100, height: 100, getContext: () => g } as unknown as HTMLCanvasElement;
      const rgba = [0, 0, 0, 1] as [number, number, number, number];
      const colors = { line: rgba, fill: rgba, bar: rgba, head: rgba, panel: rgba, text: rgba, muted: rgba, grid: rgba,
                       trigger: rgba, cursor: rgba, selection: rgba, reference: rgba, palette: Array(16).fill(rgba) };
      drawOverlay(canvas, layout, colors, {
        view: { start: 1000, end: 2000 } as never, tickS: 1e-6, origin: 0, resolution: 1e-6, trigger: null,
        cursors: { selection: null, mouse: null, reference: null }, selected: null, units: [null, null, null],
        frame: composeFrame(tile({ from: 500, complete }), 1000, 2000, 100, SPECS), unknownLabel: "不明",
      });
      return out.filter((s) => s === "不明").length;
    };
    expect(texts(false)).toBe(2);
    expect(texts(true)).toBe(0);
  });
});
