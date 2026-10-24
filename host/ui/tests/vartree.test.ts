// 変数の木: デーモンの値の形（kind を持つ object）→ 行・子・式・表示・書く値
import { describe, expect, it } from "vitest";
import { childrenOf, display, editable, elementType, expandable, inputText, kindOf, parseInput, pointeeType,
         pointerChildren, scalarOf, structType, truncated, wrap, type VarNode } from "../src/lib/vartree";
import { ints, varray, vbool, vbytes, venum, vfloat, vint, vna, vptr, vstruct } from "./value-kinds";

const node = (value: unknown, expr: string | null = "s", type?: string): VarNode => ({ key: "k", name: "s", expr, type, value });

describe("変数の木", () => {
  it("種類はデーモンの kind を読むだけ（キーの有無・型の名前で推さない）", () => {
    expect(kindOf(vint(3))).toBe("int");
    expect(kindOf(vfloat(3))).toBe("float");
    expect(kindOf(vbool(true))).toBe("bool");
    expect(kindOf(vbytes("ff"))).toBe("bytes");
    expect(kindOf(venum("MODE_RUN", 1))).toBe("enum");
    expect(kindOf(vptr(0x20000000, "struct Sensor"))).toBe("pointer");
    expect(kindOf(varray(ints(97, 98, 0), 3, { string: "ab" }))).toBe("array");
    expect(kindOf(vna("optimized out"))).toBe("unavailable");
    // メンバー名が enum・value、pointer・to 等でも構造体（前の形ではキーの有無で別の種類に見えた）
    const tricky = vstruct({ enum: venum("A", 0), value: vint(1), pointer: vint(2), to: vint(3), unavailable: vint(4) });
    expect(kindOf(tricky)).toBe("struct");
    expect(display(tricky)).toBe("{enum: A (0), value: 1, pointer: 2, to: 3, unavailable: 4}");
    // kind の無いもの（古い形で残った記録の値等）は none: JSON のまま出す（形を推して読み替えない）
    expect(kindOf(3)).toBe("none");
    expect(kindOf({ enum: "MODE_RUN", value: 1 })).toBe("none");
    expect(display({ enum: "MODE_RUN", value: 1 })).toBe('{"enum":"MODE_RUN","value":1}');
    expect(display(3)).toBe("3");
    expect(scalarOf(vint(5))).toBe(5);
    expect(scalarOf(tricky)).toBeNull();
    expect(truncated(varray(ints(1), 5))).toBe(true);
    expect(truncated(varray(ints(1)))).toBe(false);
  });

  it("表示は短く（整数は 10 進と 16 進、浮動小数は種類で小数、ポインタはシンボル、構造体は中身の一部）", () => {
    expect(display(vint(42))).toBe("42 (0x2A)");
    expect(display(vfloat(1.5))).toBe("1.5");
    expect(display(vfloat(3))).toBe("3.0");                     // JSON では 3.0 が 3 になる。float32_t 等の typedef でも
    expect(display(vfloat("nan"))).toBe("nan");
    expect(display(vbool(false))).toBe("false");
    expect(display(vbytes("ff7f"))).toBe("ff7f");
    expect(display(venum("MODE_RUN", 1))).toBe("MODE_RUN (1)");
    expect(display(venum(null, 7))).toBe("7");
    expect(display(vptr(0x20000068, "uint32_t", { symbol: "table" }))).toBe("0x20000068 <table>");
    expect(display(vptr(0x20000100, "char", { string: "hi" }))).toBe('0x20000100 "hi"');
    expect(display(varray(ints(100, 101, 0), 3, { string: "de" }))).toBe('"de"');
    expect(display(vstruct({ id: vint(1), pos: vstruct({ x: vint(1) }), list: varray(ints(1, 2)) })))
      .toBe("{id: 1, pos: {…}, list: […]}");
    expect(display(varray(ints(1, 2, 3)))).toBe("[1, 2, 3]");
    expect(display(varray(ints(0, 1), 100))).toBe("[100] [0, 1, …]");
    expect(display(vna("optimized out"))).toBe("<optimized out>");
    expect(display(vbytes("x".repeat(100))).length).toBe(64);
  });

  it("開けるのは構造体・配列（切った配列・文字の配列も）と、先のあるポインタ（NULL・void・関数は開かない）", () => {
    expect(expandable(vstruct({ a: vint(1) }))).toBe(true);
    expect(expandable(vstruct({}))).toBe(false);
    expect(expandable(varray(ints(1)))).toBe(true);
    expect(expandable(varray([]))).toBe(false);
    expect(expandable(varray([], 9))).toBe(true);                // 1 つも読まずに切った: 続きを読める
    expect(expandable(vptr(0x2000, "struct S"))).toBe(true);
    expect(expandable(vptr(0, "struct S"))).toBe(false);
    expect(expandable(vptr(0x2000, "void"))).toBe(false);
    expect(expandable(vptr(0x2000, "int (int)"))).toBe(false);
    expect(expandable(vint(5))).toBe(false);
  });

  it("子の式は C の式（メンバー・添字・矢印）。式で指せない無名のメンバーは null", () => {
    const s = node(vstruct({ id: vint(1), "<anonymous@4>": vint(2), samples: varray(ints(3, 4)) }), "sensor", "struct Sensor");
    const kids = childrenOf(s, { id: "uint8_t", samples: "int16_t[2]" });
    expect(kids.map((k) => [k.name, k.expr, k.type])).toEqual([
      ["id", "sensor.id", "uint8_t"], ["<anonymous@4>", null, undefined], ["samples", "sensor.samples", "int16_t[2]"]]);
    const arr = childrenOf({ ...kids[2], key: "k2" });
    expect(arr.map((k) => [k.name, k.expr, k.type, k.value])).toEqual([
      ["[0]", "sensor.samples[0]", "int16_t", vint(3)], ["[1]", "sensor.samples[1]", "int16_t", vint(4)]]);
    expect(childrenOf(node(varray(ints(1), 9), "*p")).map((k) => k.expr)).toEqual(["(*p)[0]"]);
    expect(childrenOf(node(varray(ints(97, 98), 2, { string: "ab" }), "name")).map((k) => k.expr)).toEqual(["name[0]", "name[1]"]);
  });

  it("ポインタの先: 構造体はメンバーを直接（p->x）、ほかは *p の 1 行", () => {
    const p = { ...node(vptr(0x2000, "struct S"), "cfg", "struct S *"), name: "cfg" };
    expect(pointerChildren(p, { type: "struct S", value: vstruct({ a: vint(1) }) }, { a: "int" })
      .map((k) => [k.name, k.expr, k.type])).toEqual([["a", "cfg->a", "int"]]);
    expect(pointerChildren(p, { value: vint(7) }).map((k) => [k.name, k.expr, k.type])).toEqual([["*cfg", "*cfg", "struct S"]]);
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
    expect(editable(node(vint(1)))).toBe(true);
    expect(editable(node(vfloat(1.5)))).toBe(true);
    expect(editable(node(vbool(true)))).toBe(true);
    expect(editable(node(venum("A", 0)))).toBe(true);
    expect(editable(node(vptr(0, "int")))).toBe(true);
    expect(editable(node(vstruct({ a: vint(1) })))).toBe(false);
    expect(editable(node(vbytes("ff")))).toBe(false);
    expect(editable(node({ value: 1 }))).toBe(false);            // kind の無いもの
    expect(editable(node(vint(1), null))).toBe(false);
    expect(editable({ ...node(vint(1)), error: "x" })).toBe(false);
  });

  it("書く値: 10 進・16 進・2 進・小数・真偽・列挙の名前", () => {
    expect(parseInput("42")).toBe(42);
    expect(parseInput("-7")).toBe(-7);
    expect(parseInput("0x1F")).toBe(31);
    expect(parseInput("0b101")).toBe(5);
    expect(parseInput("1.25")).toBe(1.25);
    expect(parseInput("true")).toBe(1);
    expect(parseInput("MODE_RUN")).toBe("MODE_RUN");
    expect(inputText(venum("MODE_RUN", 1))).toBe("MODE_RUN");
    expect(inputText(venum(null, 9))).toBe("9");
    expect(inputText(vptr(255, "int"))).toBe("0xFF");
    expect(inputText(vint(12))).toBe("12");
    expect(inputText(vfloat(2.5))).toBe("2.5");
  });
});
