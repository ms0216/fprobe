// 名前が placeholder だけの欄にも名前のツールチップ（lib/fieldnames.ts）。DOM の部品は入れていないので、必要な口だけの偽物で試す
import { describe, expect, it } from "vitest";
import { nameFields } from "../src/lib/fieldnames";

function field(attrs: Record<string, string>, inTitledLabel = false) {
  const a = { ...attrs };
  return { getAttribute: (k: string) => a[k] ?? null, setAttribute: (k: string, v: string) => { a[k] = v; },
           closest: (sel: string) => (inTitledLabel && sel === "label[title]" ? {} : null), attrs: a };
}

describe("nameFields", () => {
  it("title の無い欄に、aria-label か placeholder を title として付ける。付いている欄・ラベルの下の欄は変えない", () => {
    const f = [field({ placeholder: "page" }), field({ placeholder: "x", "aria-label": "Name" }), field({ placeholder: "y", title: "既存" }),
               field({ placeholder: "z" }, true), field({ placeholder: " " })];
    const root = { querySelectorAll: () => f } as unknown as ParentNode;
    expect(nameFields(root)).toBe(2);
    expect(f.map((e) => e.attrs.title ?? null)).toEqual(["page", "Name", "既存", null, null]);
  });
});
