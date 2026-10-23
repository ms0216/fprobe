// 2 点の間のサイクル数（lib/cycles.ts）: cycles.measure の引数と、要約の並べ方
import { describe, expect, it } from "vitest";
import { lineSpec, measureParams } from "../src/lib/cycles";

describe("measureParams", () => {
  it("始点・終点（関数名・ファイル:行・0x の番地）と回数", () => {
    expect(measureParams("main.c:10", "main.c:20", "3")).toEqual({ params: { start: "main.c:10", end: "main.c:20", count: 3 } });
    expect(measureParams(" foo ", "0x08000100", "")).toEqual({ params: { start: "foo", end: "0x08000100", count: 1 } });
  });

  it("空・回数が整数でない・0 は送らない", () => {
    expect(measureParams("", "b", "1")).toEqual({ error: "start" });
    expect(measureParams("a", "", "1")).toEqual({ error: "end" });
    expect(measureParams("a", "b", "0")).toEqual({ error: "count" });
    expect(measureParams("a", "b", "1.5")).toEqual({ error: "count" });
  });
});

describe("lineSpec", () => {
  it("選んだ行を「ファイル:行」に（ブレークポイントと同じく、ソースの名前をそのまま。同名の別ファイルと取り違えない）", () => {
    expect(lineSpec({ file: "app/main.c", line: 42 })).toBe("app/main.c:42");
    expect(lineSpec({ file: "main.c", line: 1 })).toBe("main.c:1");
  });
});
