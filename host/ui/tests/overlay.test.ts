// 時間軸の印: 見えている印を選ぶ（区間の印は始まりがどれだけ前でも、終わりが見えていれば描く）
import { describe, expect, it } from "vitest";
import { visibleMarks } from "../src/lib/timeline/overlay";

describe("見えている印", () => {
  it("始まりが視点より前の区間も、終わりが見えていれば入れる（前の 64 件までに限らない）", () => {
    const long = { t: 0, tEnd: 10_000 };
    const marks = [long, ...Array.from({ length: 200 }, (_, i) => ({ t: 100 + i }))];
    const shown = visibleMarks(marks, { start: 5000, end: 6000 });
    expect(shown).toContain(long);
    expect(shown).toHaveLength(1);
    expect(visibleMarks(marks, { start: 150, end: 160 }).map((m) => m.t)).toEqual([0, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159]);
    expect(visibleMarks(marks, { start: 20_000, end: 30_000 })).toEqual([]);
  });
});
