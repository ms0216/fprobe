// Call Graph のベクタ表の欄: 番地（と大きさ）は使う側が明示して debug.stack.static に渡す。VTOR は読んで欄に入れるだけ
import { describe, expect, it } from "vitest";
import { VTOR, vectorTableForm, vectorTableParams, vtorValue } from "../src/lib/callgraph";

describe("ベクタ表の欄 → debug.stack.static の引数", () => {
  it("空なら渡さない（例外の根も MSP も無い。画面は番地を推さない）", () => {
    expect(vectorTableParams({ address: "", size: "" })).toEqual({ params: {} });
    expect(vectorTableParams({ address: "  ", size: "" })).toEqual({ params: {} });
  });

  it("番地だけ・番地と大きさ（0x は 16 進、ほかは 10 進）", () => {
    expect(vectorTableParams({ address: "0x08000000", size: "" })).toEqual({ params: { vector_table: 0x08000000 } });
    expect(vectorTableParams({ address: "0x10000100", size: "0xC0" }))
      .toEqual({ params: { vector_table: 0x10000100, vector_table_size: 0xc0 } });
    expect(vectorTableParams({ address: "4096", size: "408" }))
      .toEqual({ params: { vector_table: 4096, vector_table_size: 408 } });
  });

  it("読めない値・番地の無い大きさ・0 の大きさは誤り（欄の名前）", () => {
    expect(vectorTableParams({ address: "g000", size: "" })).toEqual({ error: "address" });
    expect(vectorTableParams({ address: "0x1_0000_0000", size: "" })).toEqual({ error: "address" });
    expect(vectorTableParams({ address: "0x100000000", size: "" })).toEqual({ error: "address" });
    expect(vectorTableParams({ address: "", size: "408" })).toEqual({ error: "address" });
    expect(vectorTableParams({ address: "0x0", size: "0" })).toEqual({ error: "size" });
    expect(vectorTableParams({ address: "0x0", size: "-4" })).toEqual({ error: "size" });
  });

  it("使った表（結果の vector_table）を欄に描く。無ければ空", () => {
    expect(vectorTableForm({ address: 0x08000000, size: 408 })).toEqual({ address: "0x08000000", size: "408" });
    expect(vectorTableForm(null)).toEqual({ address: "", size: "" });
    expect(vectorTableForm(undefined)).toEqual({ address: "", size: "" });
  });
});

describe("VTOR（SCB の 0xE000ED08。Cortex-M のアーキテクチャの規格のレジスタ）", () => {
  it("番地は規格の値", () => {
    expect(VTOR).toBe(0xe000ed08);
  });

  it("target.memory.read の 4 バイト（16 進文字列・リトル エンディアン）を値にする。4 バイトでなければ null", () => {
    expect(vtorValue("00000008")).toBe(0x08000000);
    expect(vtorValue("00010010")).toBe(0x10000100);
    expect(vtorValue("ffffffff")).toBe(0xffffffff);
    expect(vtorValue("0000")).toBeNull();
    expect(vtorValue(null)).toBeNull();
  });
});
