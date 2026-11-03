// ソースの構文の色（lib/highlight.ts）: キーワード・型・文字列・数・コメント・前処理。複数行のコメントは行をまたぐ
import { describe, expect, it } from "vitest";
import { highlightLines } from "../src/lib/highlight";

const kinds = (line: string) => highlightLines([line])[0].filter((t) => t.t !== "pl").map((t) => `${t.t}:${t.s}`);

describe("highlightLines", () => {
  it("キーワード・型・数・文字列を分ける", () => {
    expect(kinds('static const uint32_t x = 0xFF; /* c */')).toEqual(["kw:static", "kw:const", "type:uint32_t", "num:0xFF", "cm:/* c */"]);
    expect(kinds('printf("a\\"b", 1.5f);')).toEqual(['str:"a\\"b"', "num:1.5f"]);
  });
  it("行コメント・前処理の行", () => {
    expect(kinds("int a; // 足す")).toEqual(["type:int", "cm:// 足す"]);
    expect(kinds("#include <stdio.h>")).toEqual(["pp:#include <stdio.h>"]);
  });
  it("複数行のコメントは次の行へ続く", () => {
    const t = highlightLines(["x = 1; /* start", "still comment", "end */ y = 2;"]);
    expect(t[1]).toEqual([{ t: "cm", s: "still comment" }]);
    expect(t[2].map((k) => k.t)).toEqual(["cm", "pl", "num", "pl"]);
  });
  it("文字を失わない（トークンをつなぐと元の行になる）", () => {
    const lines = ["for (long i = 0; i < rounds; i++)", "    add_one(who);", 'char *s = "x"; // y', "  /* a */ int b;"];
    highlightLines(lines).forEach((toks, i) => expect(toks.map((k) => k.s).join("")).toBe(lines[i]));
  });
});
