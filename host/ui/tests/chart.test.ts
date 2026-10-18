// ビューのグラフの計算（lib/chart.ts）と、時間軸の知らせの受け取り（events.ts）
import { describe, expect, it } from "vitest";
import {
  extentOf, nearestIndex, nearestPoint, niceDomain, niceStep, niceTicks, shortNumber, stackTops, toCsv,
} from "../src/lib/chart";
import { nearestAtOrBefore } from "../src/lib/timeline/events";

describe("グラフの計算", () => {
  it("範囲は NaN を除き、切りのよい目盛りに広げる", () => {
    expect(extentOf([[3, NaN, 1], [7]])).toEqual([1, 7]);
    expect(extentOf([[NaN]])).toBeNull();
    expect(niceStep(0.3)).toBe(0.5);
    expect(niceStep(7)).toBe(10);
    expect(niceDomain([1, 7])).toEqual([0, 8]);
    expect(niceDomain([5, 5])).toEqual([4.5, 5.5]);
    expect(niceDomain([3, 9], true)[0]).toBe(0);
    expect(niceDomain(null)).toEqual([0, 1]);
    expect(niceTicks(0, 10, 5)).toEqual([0, 2, 4, 6, 8, 10]);
    expect(niceTicks(0.1, 0.35, 3)).toEqual([0.1, 0.2, 0.3]);
  });

  it("いちばん近い点・以下のいちばん後の点", () => {
    expect(nearestIndex([0, 10, 20], 14)).toBe(1);
    expect(nearestIndex([0, 10, 20], 16)).toBe(2);
    expect(nearestIndex([], 1)).toBe(-1);
    expect(nearestAtOrBefore([0, 10, 20], 15)).toBe(1);
    expect(nearestAtOrBefore([0, 10, 20], -1)).toBe(-1);
    expect(nearestAtOrBefore([0, 10, 20], 20)).toBe(2);
  });

  it("積み上げの上端（値の無い系列は飛ばして積む、すべて無ければ切る）と散布図のいちばん近い点", () => {
    const tops = stackTops([{ name: "a", x: [0, 1, 2], y: [1, NaN, NaN] }, { name: "b", x: [0, 1, 2], y: [2, 3, NaN] }]);
    expect(Array.from(tops[0])).toEqual([1, NaN, NaN]);
    expect(Array.from(tops[1])).toEqual([3, 3, NaN]);
    const id = (v: number) => v;
    expect(nearestPoint([0, 10, 20], [0, 10, 20], 11, 9, id, id)).toBe(1);
    expect(nearestPoint([0, 10, 20], [0, 10, 20], 50, 50, id, id)).toBe(-1);
  });

  it("数の短い表し方と CSV（x のそろわない系列は空の欄）", () => {
    expect(shortNumber(3.14159)).toBe("3.142");
    expect(shortNumber(1.5e9)).toBe("1.50e+9");
    expect(shortNumber(NaN)).toBe("—");
    const csv = toCsv([{ name: "a", x: [0, 1], y: [5, 6] }, { name: "b,c", x: [1, 2], y: [7, NaN] }], "t_us");
    expect(csv).toBe('t_us,a,"b,c"\n0,5,\n1,6,7\n2,,\n');
  });
});
