// Power: 電圧の窓の統計・記録の電圧・低下の検出
import { describe, expect, it } from "vitest";
import { findDips, formatVolts, measuredVolts, minIndex, recordingVolts, VoltageWindow } from "../src/lib/power";

const ok = (volts: number) => ({ volts, over_range: false, live: true });

describe("いまの電圧の表示（補助の Pico が無い・送っていないときは 0 V と出さない）", () => {
  it("測れているときだけ値、ほかは null（画面は「データなし」）", () => {
    expect(measuredVolts(ok(3.3))).toBe(3.3);
    expect(measuredVolts({ volts: 0, over_range: false, live: false })).toBeNull();
    expect(measuredVolts({ volts: null, over_range: true, live: true })).toBeNull();
    expect(measuredVolts(null)).toBeNull();
  });
});

describe("電圧の窓", () => {
  it("最小・最大・平均（測れない読みは数えず、別に数える）", () => {
    const w = new VoltageWindow(60);
    w.add(0, ok(3.3));
    w.add(1000, ok(3.1));
    w.add(2000, { volts: null, over_range: true, live: true });
    w.add(3000, { volts: 3.2, over_range: false, live: false });
    const s = w.stats();
    expect([s.count, s.min, s.max, s.overRange, s.notLive]).toEqual([2, 3.1, 3.3, 1, 1]);
    expect(s.mean).toBeCloseTo(3.2);
    const series = w.series("V", 0);
    expect(series.x).toEqual([0, 1, 2, 3]);
    expect(series.y[2]).toBeNaN();
    expect(w.csv().split("\n")[0]).toBe("time,volts,over_range,live");
    expect(w.csv().split("\n")[3]).toContain(",,true,true");
  });

  it("窓の長さより古い読みは捨てる（短くすると即座に）", () => {
    const w = new VoltageWindow(10);
    for (let t = 0; t <= 20_000; t += 1000) w.add(t, ok(3.3));
    expect(w.length).toBe(11);
    w.setSpan(5);
    expect(w.length).toBe(6);
    w.clear();
    expect(w.stats().count).toBe(0);
    expect(w.stats().min).toBeNaN();
  });
});

describe("記録の電圧と低下", () => {
  it("aux の mV を V の列に（時刻の順）", () => {
    expect(recordingVolts([{ t: 20, b: 3290, kind: "mv" }, { t: 10, b: 3300, kind: "mv" }, { t: 5, b: 1, kind: "x" }]))
      .toEqual({ x: [10, 20], y: [3.3, 3.29] });
  });

  it("中央値より drop 以上下がった区間と、いちばん低い点", () => {
    const xs = [0, 1, 2, 3, 4, 5, 6, 7];
    const ys = [3.3, 3.3, 3.0, 2.9, 3.3, 3.3, 3.1, 3.3];
    // 使う人が指定した電圧以下の区間（中央値を正常の水準とみなす基準は使わない）。under = 指定より下の深さ
    expect(findDips(xs, ys, 3.2)).toEqual([
      { t: 2, tEnd: 4, at: 3, min: 2.9, under: expect.closeTo(0.3) },
      { t: 6, tEnd: 7, at: 6, min: 3.1, under: expect.closeTo(0.1) },
    ]);
    expect(findDips(xs, ys, 2.5)).toEqual([]);
    expect(findDips([], [], 3)).toEqual([]);
    expect(minIndex(ys)).toBe(3);
    expect(minIndex([NaN])).toBe(-1);
    expect(formatVolts(3.2954)).toBe("3.295 V");
  });
});
