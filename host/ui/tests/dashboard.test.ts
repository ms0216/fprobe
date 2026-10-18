// Dashboard: 配置の検証と直し・編集・表示の計算・読み書きの API
import { describe, expect, it } from "vitest";
import {
  addWidget, exampleLayout, expressionsOf, formatNumber, fraction, gaugeArc, layoutJson, moveWidget, newWidget,
  parseLayout, readValues, removeWidget, SharedDashboard, sliderStep, truthOf, updateWidget, validateLayout, writeCall,
  writeValue, MAX_WIDGETS,
} from "../src/lib/dashboard";
import { FakeConn } from "./fake-conn";

describe("配置の検証", () => {
  it("例の配置は書き出して読み戻すと同じで、問題が無い", () => {
    const l = exampleLayout();
    const back = parseLayout(layoutJson(l));
    expect(back.problems).toEqual([]);
    expect(back.layout).toEqual(l);
  });

  it("壊れた JSON・配置でない値", () => {
    expect(parseLayout("{").problems.map((p) => p.code)).toEqual(["invalidJson"]);
    expect(validateLayout(42).problems.map((p) => p.code)).toEqual(["notLayout"]);
  });

  it("知らない部品は捨て、範囲・桁・大きさを収め、重複した id を振り直す", () => {
    const { layout, problems } = validateLayout({ widgets: [
      { kind: "gauge", expr: " a ", id: "x", min: 5, max: 5, digits: 9, w: 7, h: 0 },
      { kind: "knob", expr: "b" },
      "nope",
      { kind: "number", id: "x", expr: "" },
      { kind: "button", expr: "c", value: 3 },
    ] });
    expect(layout.widgets.map((w) => w.kind)).toEqual(["gauge", "number", "button"]);
    const g = layout.widgets[0];
    expect([g.expr, g.min, g.max > g.min, g.digits, g.w, g.h]).toEqual(["a", 5, true, 6, 4, 1]);
    expect(layout.widgets[1].id).not.toBe("x");
    expect(layout.widgets[2].value).toBe("3");
    expect(problems.map((p) => [p.index, p.code])).toEqual([
      [1, "range"], [1, "clamped"], [1, "clamped"], [1, "clamped"], [2, "unknownKind"], [3, "notObject"],
      [4, "noExpression"], [4, "duplicateId"],
    ]);
    expect(problems.find((p) => p.code === "unknownKind")?.detail).toBe("knob");
  });

  it("部品の数の上限（配列そのものも配置として読む）", () => {
    const many = Array.from({ length: MAX_WIDGETS + 2 }, () => ({ kind: "lamp", expr: "x" }));
    const { layout, problems } = validateLayout(many);
    expect(layout.widgets.length).toBe(MAX_WIDGETS);
    expect(problems.at(-1)?.code).toBe("tooMany");
  });
});

describe("編集", () => {
  it("足す・消す・並べ替え・大きさ（収める）", () => {
    let l = addWidget({ version: 1, widgets: [] }, "gauge", "a");
    l = addWidget(l, "lamp", "b");
    const [a, b] = l.widgets;
    expect(moveWidget(l, b.id, -1).widgets.map((w) => w.expr)).toEqual(["b", "a"]);
    expect(moveWidget(l, a.id, -1)).toBe(l);
    l = updateWidget(l, a.id, { w: 9, h: 2.4, digits: -1, label: "A" });
    expect([l.widgets[0].w, l.widgets[0].h, l.widgets[0].digits, l.widgets[0].label]).toEqual([4, 2, 0, "A"]);
    expect(removeWidget(l, a.id).widgets.map((w) => w.expr)).toEqual(["b"]);
    expect(expressionsOf({ version: 1, widgets: [newWidget("gauge", "a"), newWidget("bar", "a"), newWidget("lamp", "")] }))
      .toEqual(["a"]);
  });
});

describe("表示と書く値", () => {
  const w = { ...newWidget("gauge"), min: 0, max: 200, digits: 1, unit: "V" };

  it("範囲の位置・数・真偽", () => {
    expect([fraction(50, w), fraction(-5, w), fraction(500, w)]).toEqual([0.25, 0, 1]);
    expect(fraction(NaN, w)).toBeNaN();
    expect(formatNumber(3.14159, w)).toBe("3.1 V");
    expect(formatNumber(NaN, w)).toBe("—");
    expect([truthOf(0), truthOf(2), truthOf(NaN)]).toEqual([false, true, null]);
  });

  it("ボタン（数・名前・真偽）・スライダ・切り替え（反対の値）", () => {
    const btn = (value: string) => ({ ...newWidget("button"), value });
    expect([writeValue(btn("0"), 0), writeValue(btn("MODE_RUN"), 0), writeValue(btn("true"), 0), writeValue(btn(" "), 0)])
      .toEqual([0, "MODE_RUN", true, null]);
    expect(writeValue(newWidget("slider"), 0, 42)).toBe(42);
    expect(writeValue(newWidget("slider"), 0)).toBeNull();
    expect([writeValue(newWidget("toggle"), 1), writeValue(newWidget("toggle"), 0)]).toEqual([0, 1]);
    expect(writeValue(newWidget("lamp"), 1)).toBeNull();
  });

  it("スライダの刻みとゲージの弧", () => {
    expect(sliderStep({ min: 0, max: 200, digits: 0 })).toBe(2);
    expect(sliderStep({ min: 0, max: 1, digits: 0 })).toBe(1);
    expect(sliderStep({ min: 0, max: 1, digits: 2 })).toBeCloseTo(0.01);
    expect(gaugeArc(0)).toMatch(/^M /);
    expect(gaugeArc(1)).toContain(" 1 1 ");                                 // 240° は大きい弧
  });
});

describe("読み書きの API", () => {
  it("走行中は watch.values（組に無い式は sampled = false）", async () => {
    const c = new FakeConn({ "watch.values": { values: [{ name: "a", time_us: 5, value: 1 }] } });
    const r = await readValues(c.call.bind(c) as any, ["a", "b"], true);
    expect(r.get("a")).toEqual({ value: 1, time_us: 5, error: null, sampled: true });
    expect(r.get("b")?.sampled).toBe(false);
    expect(c.quiet).toEqual(["watch.values"]);
  });

  it("止まっているときは debug.evaluate（誤りは式ごと）", async () => {
    const c = new FakeConn({ "debug.evaluate": ({ expression }: any) => {
      if (expression === "bad") throw new Error("no symbol");
      return { value: 7 };
    } });
    const r = await readValues(c.call.bind(c) as any, ["a", "bad"], false);
    expect(r.get("a")?.value).toBe(7);
    expect(r.get("bad")?.error).toBe("no symbol");
  });

  it("書く: 走行中は watch.write、止まっているときは debug.write_expression（真偽は数に）", () => {
    expect(writeCall(true, "x", true)).toEqual({ method: "watch.write", params: { expression: "x", value: true } });
    expect(writeCall(false, "x", true)).toEqual({ method: "debug.write_expression", params: { expression: "x", value: 1 } });
  });
});

describe("配置をデーモンの画面の状態で分け合う（別の窓・遠隔の画面・AI が同じ操作パネルを見る）", () => {
  const tick = () => new Promise((r) => setTimeout(r, 0));

  it("デーモンに配置があればそれ（検証して直したもの）、無ければ手元の配置を上げる", async () => {
    const remote = { version: 1, widgets: [{ id: "a", kind: "gauge", expr: "counter", w: 9 }] };
    let state: any = { dashboard: remote };
    const conn = new FakeConn({ "ui.state.get": () => state, "ui.state.set": (p: any) => (state = { dashboard: p.dashboard }) });
    const got: any[] = [];
    const shared = new SharedDashboard(conn as any, (c) => got.push(c));
    await shared.load(exampleLayout());
    expect(got.length).toBe(1);
    expect(got[0].layout.widgets[0]).toMatchObject({ id: "a", kind: "gauge", expr: "counter", w: 4 });   // 幅は直す
    expect(got[0].problems.length).toBeGreaterThan(0);
    state = { dashboard: null };
    const other = new SharedDashboard(conn as any, () => {});
    const local = exampleLayout();
    await other.load(local);
    expect(conn.calls.at(-1)).toEqual(["ui.state.set", { dashboard: local, origin: other.origin }]);
    expect(conn.quiet).toContain("ui.state.set");
  });

  it("同じ配置は上げ直さない（受け取った配置を送り返さない）", async () => {
    const conn = new FakeConn({ "ui.state.get": { dashboard: null }, "ui.state.set": {} });
    const shared = new SharedDashboard(conn as any, () => {});
    const l = exampleLayout();
    await shared.load(l);
    const sets = () => conn.calls.filter(([m]) => m === "ui.state.set").length;
    expect(sets()).toBe(1);
    await shared.save(structuredClone(l));
    expect(sets()).toBe(1);
    conn.emit("ui.state", { origin: "w-other", changed: ["dashboard"], state: { dashboard: { version: 1, widgets: [] } } });
    await tick();
    await shared.save({ version: 1, widgets: [] });
    expect(sets()).toBe(1);
    await shared.save(addWidget({ version: 1, widgets: [] }, "lamp", "x"));
    expect(sets()).toBe(2);
  });

  it("ほかの窓・AI の変更を受け取り、自分の変更の通知と消された配置は読み飛ばす", async () => {
    const conn = new FakeConn({ "ui.state.get": { dashboard: null }, "ui.state.set": {} });
    const got: any[] = [];
    const shared = new SharedDashboard(conn as any, (c) => got.push(c.layout));
    await shared.load({ version: 1, widgets: [] });
    conn.emit("ui.state", { origin: shared.origin, changed: ["dashboard"], state: { dashboard: exampleLayout() } });
    conn.emit("ui.state", { origin: "w2", changed: ["view"], state: { dashboard: exampleLayout() } });
    conn.emit("ui.state", { origin: "w2", changed: ["dashboard"], state: { dashboard: null } });
    conn.emit("ui.state", { origin: null, changed: ["dashboard"], state: { dashboard: { version: 1, widgets: [] } } });
    await tick();
    expect(got).toEqual([{ version: 1, widgets: [] }]);
    shared.dispose();
  });

  it("ui.state の無い古いデーモンでは手元の配置のまま", async () => {
    const conn = new FakeConn({ "ui.state.get": () => { throw new Error("no method"); }, "ui.state.set": {} });
    const shared = new SharedDashboard(conn as any, () => {});
    await shared.load(exampleLayout());
    await shared.save(exampleLayout());
    expect(conn.calls.map(([m]) => m)).toEqual(["ui.state.get"]);
  });
});
