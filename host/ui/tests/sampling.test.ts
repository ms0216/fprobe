// Data Sampling の計算: 式の一覧・値の積み上げと統計・XY・配列の波形・取得の状態・条件での取得の引数
import { describe, expect, it } from "vitest";
import {
  addExpr, arrayWave, captureSeries, chartSeries, displayValue, health, numberOf, parseExprList,
  RATES, rateOptions, removeExpr, SampleStore, SharedSampling, toggleCursor, triggerParams, windowDomain, xyPoints,
} from "../src/lib/sampling";
import { missing } from "../src/lib/watchshare";
import { FakeConn } from "./fake-conn";
import { ints, varray, vbool, vbytes, venum, vfloat, vint, vna, vstruct } from "./value-kinds";

describe("一覧と周期をデーモンの画面の状態で分け合う（別の窓・別の PC・AI が同じものを見る）", () => {
  const tick = () => new Promise((r) => setTimeout(r, 0));
  const LIST = [{ expr: "counter", plot: true, axis: "left" as const }];

  it("デーモンに一覧があればそれを使い、無ければ手元の一覧を上げる", async () => {
    let state: any = { sampling: { expressions: LIST, rate_hz: 100 } };
    const conn = new FakeConn({ "ui.state.get": () => state, "ui.state.set": (p: any) => { state = { sampling: p.sampling }; return state; } });
    const got: any[] = [];
    const shared = new SharedSampling(conn as any, (list, rate) => got.push([list, rate]));
    await shared.load([], 1000);
    expect(got).toEqual([[LIST, 100]]);
    state = { sampling: null };
    const other = new SharedSampling(conn as any, () => {});
    await other.load([{ expr: "adc", plot: true, axis: "right" }], 10);
    expect(conn.calls.at(-1)).toEqual(["ui.state.set", { sampling: { expressions: [{ expr: "adc", plot: true, axis: "right" }], rate_hz: 10 },
                                                          origin: other.origin }]);
    expect(conn.quiet).toContain("ui.state.set");
  });

  it("ほかの窓・AI の変更を受け取り、自分の変更の通知は読み飛ばす", async () => {
    const conn = new FakeConn({ "ui.state.get": { sampling: null }, "ui.state.set": {} });
    const got: any[] = [];
    const shared = new SharedSampling(conn as any, (list, rate) => got.push([list, rate]));
    await shared.load(LIST, 1000);
    conn.emit("ui.state", { origin: "w-other", changed: ["sampling"], state: { sampling: { expressions: [], rate_hz: 10 } } });
    conn.emit("ui.state", { origin: shared.origin, changed: ["sampling"], state: { sampling: { expressions: LIST, rate_hz: 1 } } });
    conn.emit("ui.state", { origin: "w-other", changed: ["view"], state: { sampling: { expressions: LIST, rate_hz: 2 } } });
    await tick();
    expect(got).toEqual([[[], 10]]);
    shared.dispose();
  });

  it("ui.state の無い古いデーモンでは手元の一覧のまま（誤りにしない）", async () => {
    const conn = new FakeConn({ "ui.state.get": () => { throw new Error("no method"); } });
    const got: any[] = [];
    await new SharedSampling(conn as any, (l, r) => got.push([l, r])).load(LIST, 1000);
    expect(got).toEqual([]);
  });

  it("外された誤った式は、ほかの利用者に外されたと数えない", () => {
    expect(missing(["a"], ["a", "bad", "gone"], ["bad"])).toEqual(["gone"]);
  });
});

describe("式の一覧", () => {
  it("足す・消す（空白を除き、重複・空は足さない）", () => {
    let l = addExpr([], " counter ");
    l = addExpr(l, "counter");
    l = addExpr(l, "");
    l = addExpr(l, "sensors[0].value");
    expect(l).toEqual([{ expr: "counter", plot: true, axis: "left" }, { expr: "sensors[0].value", plot: true, axis: "left" }]);
    expect(removeExpr(l, "counter").map((e) => e.expr)).toEqual(["sensors[0].value"]);
  });

  it("覚えた一覧を読む（壊れていれば空、古い形 = 文字の配列も）", () => {
    expect(parseExprList("{")).toEqual([]);
    expect(parseExprList('"x"')).toEqual([]);
    expect(parseExprList('["a", "a", " b "]').map((e) => e.expr)).toEqual(["a", "b"]);
    expect(parseExprList(JSON.stringify([{ expr: "v", plot: false, axis: "right" }, { expr: 3 }])))
      .toEqual([{ expr: "v", plot: false, axis: "right" }]);
  });
});

describe("値", () => {
  it("数に（int・float・enum の value、真偽は 1 / 0）。kind で決め、構造体・NaN・kind の無いものは NaN", () => {
    expect([numberOf(vint(3)), numberOf(vfloat(1.5)), numberOf(vbool(true)), numberOf(venum("MODE_RUN", 1))])
      .toEqual([3, 1.5, 1, 1]);
    expect(numberOf(vstruct({ enum: venum("A", 1), value: vint(1) }))).toBeNaN();     // メンバー名で推さない
    expect(numberOf(vfloat("nan"))).toBeNaN();
    expect(numberOf(3)).toBeNaN();                                                     // kind の無い古い形
    expect(numberOf(null)).toBeNaN();
  });

  it("表示（列挙の名前・真偽・読めない理由・長い構造体は短く）", () => {
    expect(displayValue(venum("MODE_RUN", 1))).toBe("MODE_RUN");
    expect(displayValue(venum(null, 4))).toBe("4");
    expect(displayValue(vbool(false))).toBe("false");
    expect(displayValue(vfloat(1.23456789))).toBe("1.23457");
    expect(displayValue(vfloat(2.5), 2)).toBe("2.50");
    expect(displayValue(vint(7))).toBe("7");
    expect(displayValue(vfloat("inf"))).toBe("inf");
    expect(displayValue(null)).toBe("—");
    expect(displayValue(vna("NULL"))).toBe("<NULL>");
    expect(displayValue(varray(ints(104, 105, 0), 3, { string: "hi" }))).toBe('"hi"');
    const long = vstruct(Object.fromEntries([..."abcdefghijklmnop"].map((k) => [k, vbytes("ffff")])));
    expect(displayValue(long).endsWith("…")).toBe(true);
    expect(displayValue(long).length).toBe(60);
    expect(displayValue({ enum: "MODE_RUN", value: 1 })).toBe('{"enum":"MODE_RUN","value":1}');   // kind の無い古い形は JSON
  });
});

describe("値の積み上げ（SampleStore）", () => {
  const s = (time_us: number, value: number | object) => ({ time_us, value: typeof value === "number" ? vint(value) : value });

  it("同じ列を何度読んでも、前回の最後より後だけを足す", () => {
    const st = new SampleStore();
    expect(st.merge("a", [s(1, 2), s(2, 4)])).toBe(2);
    expect(st.merge("a", [s(1, 2), s(2, 4), s(3, 9), s(4, vstruct({ x: vint(1) }))])).toBe(2);
    expect(st.latest("a")).toEqual(s(4, vstruct({ x: vint(1) })));
    expect(st.points("a").y.slice(0, 3)).toEqual([2, 4, 9]);
    expect(st.end()).toBe(4);
  });

  it("上限を超えたら古い点を捨てる", () => {
    const st = new SampleStore(3);
    st.merge("a", [1, 2, 3, 4, 5].map((t) => s(t, t)));
    expect(st.points("a").x).toEqual([3, 4, 5]);
    expect(st.discarded("a")).toBe(2);                               // 捨てた数を数える（黙って捨てない）
    expect(st.limit).toBe(3);
    expect(st.discarded("b")).toBe(0);
  });

  it("読みが追いつかず読まなかった区間（API の from_us より前で、前回の最後より後）を数える（黙って抜かさない）", () => {
    const st = new SampleStore();
    st.merge("a", [1, 2].map((t) => s(t, t)), null);
    st.merge("a", [2, 3, 4].map((t) => s(t, t)), 2);                // 読んだ範囲の始め 2 が前回の最後 2 以前: 続いている
    expect(st.gaps("a")).toEqual([]);
    st.merge("a", [10, 11].map((t) => s(t, t)), 10);                // 前回の最後 4 と 10 の間を読んでいない
    expect(st.gaps("a")).toEqual([[4, 10]]);
    st.merge("b", [50].map((t) => s(t, t)), 50);                    // 初めての読み: 前が無いので抜けではない
    expect(st.gaps("b")).toEqual([]);
  });

  it("グラフの系列（描く式だけ、右の目盛り、階段）と窓", () => {
    const st = new SampleStore();
    st.merge("a", [s(1, 1)]);
    st.merge("b", [s(2_000_000, 5)]);
    const series = chartSeries(st, [{ expr: "a", plot: true, axis: "left" }, { expr: "b", plot: true, axis: "right" },
                                     { expr: "c", plot: false, axis: "left" }]);
    expect(series.map((x) => [x.name, x.axis, x.step])).toEqual([["a", "left", true], ["b", "right", true]]);
    expect(windowDomain(st.end(), 1)).toEqual([1_000_000, 2_000_000]);
    expect(windowDomain(st.end(), 0)).toBeNull();
    expect(windowDomain(null, 5)).toBeNull();
  });
});

describe("XY と配列", () => {
  it("XY: watch.xy の組を描く点に（数でない値の組は除く。組み方は変えない）", () => {
    const r = { points: [{ x: vint(1), y: vint(100) }, { x: venum("A", 2), y: vint(200) },
                         { x: vstruct({ a: vint(3) }), y: vint(300) }] };
    expect(xyPoints(r)).toEqual({ x: [1, 2], y: [100, 200] });
    expect(xyPoints(null)).toEqual({ x: [], y: [] });
  });

  it("配列の値を添字 → 数に（数の配列・切った配列は読んだ分・文字の配列・構造体の欄）", () => {
    expect(arrayWave(varray(ints(1, 2, 3)))).toEqual({ values: [1, 2, 3], length: 3 });
    expect(arrayWave(varray(ints(7, 14), 100))).toEqual({ values: [7, 14], length: 100 });
    expect(arrayWave(varray(ints(97, 98), 2, { string: "ab" }))).toEqual({ values: [97, 98], length: 2 });
    expect(arrayWave(varray([vfloat(1.5), vfloat(2.5)]))).toEqual({ values: [1.5, 2.5], length: 2 });
    expect(arrayWave(varray([vstruct({ value: vfloat(1.5) }), vstruct({ value: vfloat(2.5) })]), "value"))
      .toEqual({ values: [1.5, 2.5], length: 2 });
    expect(arrayWave(varray([vstruct({ a: vint(1) })]))).toBeNull();
    expect(arrayWave(vint(42))).toBeNull();
    expect(arrayWave([1, 2, 3])).toBeNull();                                         // kind の無い古い形
  });
});

describe("取得の状態と条件", () => {
  it("watch.status から周期・方式・取りこぼしの割合（割合はデーモンの loss_ratio）", () => {
    const h = health({ watches: [{ name: "a", method: "sampler" }, { name: "p->x", method: "host" }], rate_hz: 1000,
                       rounds: 900, overruns: 100, errors: 2, loss_ratio: 0.1, chase_errors: { "p->x": "NULL" } });
    expect([h.rate, h.sampled, h.host, h.lossRatio, h.errors, h.running]).toEqual([1000, ["a"], ["p->x"], 0.1, 2, true]);
    expect(h.chaseErrors).toEqual({ "p->x": "NULL" });
    expect(health({ watches: [] }).running).toBe(false);
    expect(health(null).lossRatio).toBeNull();
  });

  it("watch.set で外した誤った式（rejected）と、開始からの秒・実際の周期", () => {
    const h = health({ watches: [{ name: "a", method: "sampler" }], rate_hz: 100, rounds: 500, overruns: 0,
                       since_start_s: 5, measured_hz: 100, rejected: [{ expression: "nope", error: "変数 'nope' が無い" }] });
    expect(h.rejected).toEqual({ nope: "変数 'nope' が無い" });
    expect(h.measuredHz).toBeCloseTo(100);
    expect(health({ watches: [] }).rejected).toEqual({});
  });

  it("周期の欄の選択肢は、実際の周期が一覧に無ければ足す", () => {
    expect(rateOptions(250)).toContain(250);
    expect(rateOptions(100)).toEqual(RATES);
  });

  it("条件の引数（変数が無ければいまから、post 0 は止まりまで）と誤り", () => {
    const form = { variable: "counter", op: ">" as const, value: "100", edge: true, preMs: "10", postMs: "500" };
    expect(triggerParams(form)).toEqual({ start: { variable: "counter", op: ">", value: 100, edge: true }, pre_s: 0.01, post_s: 0.5 });
    expect(triggerParams({ ...form, variable: "", postMs: "0" })).toEqual({ start: null, pre_s: 0.01, post_s: null });
    expect(triggerParams({ ...form, value: "x" })).toBe("value");
    expect(triggerParams({ ...form, value: " " })).toBe("value");
    expect(triggerParams({ ...form, preMs: "-1" })).toBe("time");
    // 前後の時間は使う人が入れる（空を 0 とみなさない）
    expect(triggerParams({ ...form, preMs: "" })).toBe("time");
    expect(triggerParams({ ...form, postMs: " " })).toBe("time");
  });

  it("取った値を系列に（値の無い式は除く）", () => {
    const out = captureSeries({ a: [{ time_us: 1, value: vint(2) }], b: [] }, ["a", "b"]);
    expect(out).toEqual([{ name: "a", x: [1], y: [2], step: true }]);
    expect(captureSeries(undefined, ["a"])).toEqual([]);
  });
});

describe("グラフの 2 つのカーソル（A・B。測定は watch.measure）", () => {
  it("クリックで A、Shift + クリックで B。A の無いうちの Shift + クリックは A", () => {
    expect(toggleCursor({ a: null, b: null }, 5, false)).toEqual({ a: 5, b: null });
    expect(toggleCursor({ a: null, b: null }, 5, true)).toEqual({ a: 5, b: null });
    expect(toggleCursor({ a: 5, b: null }, 9, true)).toEqual({ a: 5, b: 9 });
    expect(toggleCursor({ a: 5, b: 9 }, 7, false)).toEqual({ a: 7, b: 9 });
  });
});
