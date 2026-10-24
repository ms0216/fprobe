// 読みの打ち切りを画面に出す材料（切ったこと・使った上限）
import { describe, expect, it } from "vitest";
import { queryCut, topCut } from "../src/lib/cut";

describe("打ち切り", () => {
  it("recording.query: limit で切った・長期の記録の新しい分割だけを読んだ、を返す（切っていなければ null）", () => {
    expect(queryCut({ count: 10, truncated: false }, 100)).toBeNull();
    expect(queryCut({ count: 100, truncated: true }, 100)).toEqual({ shown: 100, limit: 100, truncated: true, segments: null });
    expect(queryCut({ count: 5, truncated: false, partial: true, segments: [3, 4] }, 100))
      .toEqual({ shown: 5, limit: 100, truncated: false, segments: 2 });
  });
  it("上位 top の表: 切る前の数が並びより多ければ（shown / total / limit）、無ければ null", () => {
    expect(topCut(3, 3, 500)).toBeNull();
    expect(topCut(500, 812, 500)).toEqual({ shown: 500, total: 812, limit: 500 });
    expect(topCut(2, undefined, 500)).toBeNull();
  });
});
