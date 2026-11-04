// 既定の配置の窓の大きさ（lib/dock.ts の share）: 画面の割合で、上限は大きな画面での大きさ
import { describe, expect, it } from "vitest";
import { share } from "../src/lib/dock";

describe("share", () => {
  it("狭い画面では割合、広い画面では上限（中央の時間軸・ソースが脇の列に押しつぶされない）", () => {
    expect(share(1024, 0.42, 600)).toBe(430);
    expect(share(1920, 0.42, 600)).toBe(600);
    expect(share(0, 0.42, 600)).toBe(600);                 // 大きさがまだ分からない
  });
});
