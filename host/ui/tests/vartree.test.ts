// 変数の木: デーモンの値の形 → 行・子・式・表示・書く値
import { describe, expect, it } from "vitest";
import { childrenOf, display, editable, elementType, expandable, inputText, kindOf, parseInput, pointeeType,
         pointerChildren, structType, wrap, type VarNode } from "../src/lib/vartree";

const node = (value: unknown, expr: string | null = "s", type?: string): VarNode => ({ key: "k", name: "s", expr, type, value });

describe("変数の木", () => {
  it("値の形を見分ける", () => {
    expect(kindOf(3)).toBe("number");
    expect(kindOf(true)).toBe("bool");
    expect(kindOf({ enum: "MODE_RUN", value: 1 })).toBe("enum");
    expect(kindOf({ pointer: 0x20000000, to: "struct Sensor" })).toBe("pointer");
    expect(kindOf({ string: "ab", values: [97, 98, 0] })).toBe("string");
    expect(kindOf({ length: 100, head: [1, 2] })).toBe("truncated");
    expect(kindOf({ unavailable: "optimized out" })).toBe("unavailable");
    expect(kindOf({ id: 1, mode: { enum: "A", value: 0 } })).toBe("struct");
    expect(kindOf([1, 2])).toBe("array");
  });

  it("表示は短く（数は 10 進と 16 進、ポインタはシンボル、構造体は中身の一部）", () => {
    expect(display(42)).toBe("42 (0x2A)");
    expect(display(1.5)).toBe("1.5");
    expect(display(3, 64, "float")).toBe("3.0");                 // JSON では 3.0 が 3 になる
    expect(display({ enum: "MODE_RUN", value: 1 })).toBe("MODE_RUN (1)");
    expect(display({ pointer: 0x20000068, to: "uint32_t", symbol: "table" })).toBe("0x20000068 <table>");
    expect(display({ pointer: 0x20000100, to: "char", string: "hi" })).toBe('0x20000100 "hi"');
    expect(display({ string: "demo", values: [] })).toBe('"demo"');
    expect(display({ id: 1, pos: { x: 1, y: 2 }, list: [1, 2] })).toBe("{id: 1, pos: {…}, list: […]}");
    expect(display([1, 2, 3])).toBe("[1, 2, 3]");
    expect(display({ length: 100, head: [0, 1] })).toBe("[100] [0, 1, …]");
    expect(display({ unavailable: "optimized out" })).toBe("<optimized out>");
    expect(display("x".repeat(100)).length).toBe(64);
  });

  it("開けるのは構造体・配列・多い配列・文字の配列と、先のあるポインタ（NULL・void・関数は開かない）", () => {
    expect(expandable({ a: 1 })).toBe(true);
    expect(expandable([1])).toBe(true);
    expect(expandable([])).toBe(false);
    expect(expandable({ pointer: 0x2000, to: "struct S" })).toBe(true);
    expect(expandable({ pointer: 0, to: "struct S" })).toBe(false);
    expect(expandable({ pointer: 0x2000, to: "void" })).toBe(false);
    expect(expandable({ pointer: 0x2000, to: "int (int)" })).toBe(false);
    expect(expandable(5)).toBe(false);
  });

  it("子の式は C の式（メンバー・添字・矢印）。式で指せない無名のメンバーは null", () => {
    const s = node({ id: 1, "<anonymous@4>": 2, samples: [3, 4] }, "sensor", "struct Sensor");
    const kids = childrenOf(s, { id: "uint8_t", samples: "int16_t[2]" });
    expect(kids.map((k) => [k.name, k.expr, k.type])).toEqual([
      ["id", "sensor.id", "uint8_t"], ["<anonymous@4>", null, undefined], ["samples", "sensor.samples", "int16_t[2]"]]);
    const arr = childrenOf({ ...kids[2], key: "k2" });
    expect(arr.map((k) => [k.name, k.expr, k.type])).toEqual([["[0]", "sensor.samples[0]", "int16_t"], ["[1]", "sensor.samples[1]", "int16_t"]]);
    expect(childrenOf(node({ length: 9, head: [1] }, "*p")).map((k) => k.expr)).toEqual(["(*p)[0]"]);
    expect(childrenOf(node({ string: "ab", values: [97, 98] }, "name")).map((k) => k.expr)).toEqual(["name[0]", "name[1]"]);
  });

  it("ポインタの先: 構造体はメンバーを直接（p->x）、ほかは *p の 1 行", () => {
    const p = { ...node({ pointer: 0x2000, to: "struct S" }, "cfg", "struct S *"), name: "cfg" };
    expect(pointerChildren(p, { type: "struct S", value: { a: 1 } }, { a: "int" }).map((k) => [k.name, k.expr, k.type]))
      .toEqual([["a", "cfg->a", "int"]]);
    expect(pointerChildren(p, { value: 7 }).map((k) => [k.name, k.expr, k.type])).toEqual([["*cfg", "*cfg", "struct S"]]);
  });

  it("型の文字から要素・先の型、構造体の型", () => {
    expect(elementType("uint32_t[4]")).toBe("uint32_t");
    expect(pointeeType("struct Sensor *")).toBe("struct Sensor");
    expect(pointeeType("char *const")).toBe("char");
    expect(structType("struct Sensor")).toBe("struct Sensor");
    expect(structType("uint8_t[3]")).toBeNull();
    expect(wrap("a.b[2]")).toBe("a.b[2]");
    expect(wrap("p->x")).toBe("p->x");
    expect(wrap("*p")).toBe("(*p)");
  });

  it("書けるのは式のある基本型・列挙・ポインタ", () => {
    expect(editable(node(1))).toBe(true);
    expect(editable(node({ enum: "A", value: 0 }))).toBe(true);
    expect(editable(node({ pointer: 0, to: "int" }))).toBe(true);
    expect(editable(node({ a: 1 }))).toBe(false);
    expect(editable(node(1, null))).toBe(false);
    expect(editable({ ...node(1), error: "x" })).toBe(false);
  });

  it("書く値: 10 進・16 進・2 進・小数・真偽・列挙の名前", () => {
    expect(parseInput("42")).toBe(42);
    expect(parseInput("-7")).toBe(-7);
    expect(parseInput("0x1F")).toBe(31);
    expect(parseInput("0b101")).toBe(5);
    expect(parseInput("1.25")).toBe(1.25);
    expect(parseInput("true")).toBe(1);
    expect(parseInput("MODE_RUN")).toBe("MODE_RUN");
    expect(inputText({ enum: "MODE_RUN", value: 1 })).toBe("MODE_RUN");
    expect(inputText({ pointer: 255, to: "int" })).toBe("0xFF");
    expect(inputText(12)).toBe("12");
  });
});
