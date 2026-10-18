// 変数の木（Watch・Locals）: デーモンの解読した値（fprobe.dwarf.values の形）を、展開できる行に分ける。Svelte に依らない。
//
// 値の形（debug.evaluate / debug.frame_variables）:
//   整数・浮動小数・真偽 → そのまま、列挙 → {enum, value}、ポインタ → {pointer, to, symbol?, string?}、
//   構造体 → {メンバー: 値}、配列 → [...]（多ければ {length, head}）、文字の配列 → {string, values}、
//   読めない → {unavailable}
//
// 構造体・配列の中身は値に入っているのでその場で子にする。ポインタの先は展開したときに `*式` を読む（遅延。
// VS Code・Ozone と同じ）。子の式（`a.b`・`a[2]`・`p->x`）はそのまま debug.evaluate / debug.write_expression に渡せる。
import type { Key } from "./i18n";

export interface VarNode {
  key: string;              // 木の中で一意（開いている行を覚えるのに使う）
  name: string;             // 表示の名前（メンバー名・[i]・*p）
  expr: string | null;      // C の式（無名のメンバー等、式で指せないものは null）
  type?: string;
  value: unknown;
  error?: string;           // 評価できなかった（Watch の行）
  tag?: Key;                // 名前の後ろに添える印（引数）
}

export type ValueKind = "number" | "bool" | "text" | "enum" | "pointer" | "struct" | "array" | "string" | "truncated"
  | "unavailable" | "none";

const isObject = (v: unknown): v is Record<string, any> => !!v && typeof v === "object" && !Array.isArray(v);

export function kindOf(v: unknown): ValueKind {
  if (typeof v === "number") return "number";
  if (typeof v === "boolean") return "bool";
  if (typeof v === "string") return "text";
  if (Array.isArray(v)) return "array";
  if (!isObject(v)) return "none";
  if ("unavailable" in v) return "unavailable";
  if ("pointer" in v && "to" in v) return "pointer";
  if ("enum" in v && "value" in v && Object.keys(v).length === 2) return "enum";
  if ("string" in v && "values" in v) return "string";
  if ("length" in v && "head" in v && Array.isArray(v.head)) return "truncated";
  return "struct";
}

const hex = (n: number) => `0x${(n >>> 0).toString(16).toUpperCase()}`;

/** 浮動小数の型（JSON では 3.0 が 3 になるので、型で小数として表す） */
export function isFloatType(type: string | undefined): boolean {
  return !!type && /^(const |volatile )*(float|double|long double)$/.test(type.trim());
}

/** 値の列の文字（短く。構造体・配列は中身の一部）。type は値の型（浮動小数を見分ける） */
export function display(v: unknown, limit = 64, type?: string): string {
  const text = (() => {
    switch (kindOf(v)) {
      case "number": {
        const n = v as number;
        if (isFloatType(type)) return Number.isInteger(n) ? n.toFixed(1) : String(n);
        return Number.isInteger(n) ? `${n} (${hex(n)})` : String(n);
      }
      case "bool":
        return String(v);
      case "text":
        return v as string;
      case "enum": {
        const e = v as { enum: string | null; value: number };
        return e.enum ? `${e.enum} (${e.value})` : String(e.value);
      }
      case "pointer": {
        const p = v as { pointer: number; symbol?: string; string?: string | null };
        const pad = `0x${(p.pointer >>> 0).toString(16).toUpperCase().padStart(8, "0")}`;
        if (p.string !== undefined && p.string !== null) return `${pad} "${p.string}"`;
        return p.symbol ? `${pad} <${p.symbol}>` : pad;
      }
      case "string":
        return `"${(v as { string: string }).string}"`;
      case "array":
        return `[${(v as unknown[]).map((x) => brief(x)).join(", ")}]`;
      case "truncated": {
        const t = v as { length: number; head: unknown[] };
        return `[${t.length}] [${t.head.map((x) => brief(x)).join(", ")}, …]`;
      }
      case "struct":
        return `{${Object.entries(v as object).map(([k, x]) => `${k}: ${brief(x)}`).join(", ")}}`;
      case "unavailable":
        return `<${(v as { unavailable: string }).unavailable}>`;
      default:
        return String(v);
    }
  })();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

/** 入れ子の中の値（数は 10 進だけ、入れ子は {…} / […]） */
function brief(v: unknown): string {
  switch (kindOf(v)) {
    case "struct":
      return "{…}";
    case "array":
    case "truncated":
      return "[…]";
    case "number":
      return String(v);
    default:
      return display(v, 24);
  }
}

/** void・関数へのポインタは先を読まない */
function derefable(p: { pointer: number; to: string }): boolean {
  return p.pointer !== 0 && !/^(const |volatile )*void$/.test(p.to.trim()) && !/\(/.test(p.to);
}

export function expandable(v: unknown): boolean {
  switch (kindOf(v)) {
    case "struct":
      return Object.keys(v as object).length > 0;
    case "array":
      return (v as unknown[]).length > 0;
    case "string":
    case "truncated":
      return true;
    case "pointer":
      return derefable(v as { pointer: number; to: string });
    default:
      return false;
  }
}

/** その場で書ける値（基本型・列挙・ポインタ）。構造体・配列は中のメンバーを書く */
export function editable(node: VarNode): boolean {
  if (!node.expr || node.error) return false;
  return ["number", "bool", "enum", "pointer"].includes(kindOf(node.value));
}

/** 式を後置の演算子の前に置けるようにする（`a.b` `p[1]` はそのまま、`*p` `a + 1` は括弧で） */
export function wrap(expr: string): string {
  return /^[A-Za-z_]\w*(?:(?:\.|->)[A-Za-z_]\w*|\[\d+\])*$/.test(expr) ? expr : `(${expr})`;
}

export function derefExpr(expr: string): string {
  return `*${wrap(expr)}`;
}

/** 型の文字から要素・指す先の型（"uint32_t[4]" → "uint32_t"、"struct S *" → "struct S"） */
export function elementType(type: string | undefined): string | undefined {
  const m = type ? /^(.*?)\s*\[\d*\]$/.exec(type.trim()) : null;
  return m ? m[1] : undefined;
}

export function pointeeType(type: string | undefined): string | undefined {
  const m = type ? /^(.*?)\s*\*\s*(const\s*)?$/.exec(type.trim()) : null;
  return m ? m[1] : undefined;
}

const ANONYMOUS = /^<anonymous@\d+>$/;

/** 値に入っている子（構造体のメンバー・配列の要素・文字の配列の要素・多い配列の先頭）。
 *  members はメンバーの型（debug.type_info から。分からなければ空） */
export function childrenOf(node: VarNode, members: Record<string, string> = {}): VarNode[] {
  const v = node.value;
  const base = node.expr;
  const element = elementType(node.type);
  const items = (list: unknown[]): VarNode[] =>
    list.map((x, i) => ({ key: `${node.key}[${i}]`, name: `[${i}]`, expr: base === null ? null : `${wrap(base)}[${i}]`,
                          type: element, value: x }));
  switch (kindOf(v)) {
    case "array":
      return items(v as unknown[]);
    case "truncated":
      return items((v as { head: unknown[] }).head);
    case "string": {
      const values = (v as { values: unknown }).values;
      return Array.isArray(values) ? items(values) : kindOf(values) === "truncated" ? items((values as any).head) : [];
    }
    case "struct":
      return Object.entries(v as object).map(([name, x]) => ({
        key: `${node.key}.${name}`, name, type: members[name], value: x,
        expr: base === null || ANONYMOUS.test(name) ? null : `${wrap(base)}.${name}`,
      }));
    default:
      return [];
  }
}

/** ポインタの先（`*p` を読んだ結果）の子: 構造体ならメンバーを直接（`p->x`）、そうでなければ `*p` の 1 行 */
export function pointerChildren(node: VarNode, target: { type?: string; value: unknown }, members: Record<string, string> = {}): VarNode[] {
  const expr = node.expr;
  if (expr !== null && kindOf(target.value) === "struct") {
    return Object.entries(target.value as object).map(([name, x]) => ({
      key: `${node.key}->${name}`, name, type: members[name], value: x,
      expr: ANONYMOUS.test(name) ? null : `${wrap(expr)}->${name}`,
    }));
  }
  return [{ key: `${node.key}*`, name: `*${node.name}`, expr: expr === null ? null : derefExpr(expr),
            type: target.type ?? pointeeType(node.type), value: target.value }];
}

/** 構造体の型の名前（メンバーの型を debug.type_info で引く）。配列・ポインタは null */
export function structType(type: string | undefined): string | null {
  if (!type || elementType(type) || pointeeType(type)) return null;
  return type.trim();
}

/** 書く値の文字 → debug.write_expression の value（整数・小数・真偽は 1/0、ほかは列挙の名前として文字のまま） */
export function parseInput(text: string): number | string {
  const s = text.trim();
  if (/^[-+]?0x[0-9a-f]+$/i.test(s)) return (s.startsWith("-") ? -1 : 1) * parseInt(s.replace(/^[-+]?0x/i, ""), 16);
  if (/^[-+]?0b[01]+$/i.test(s)) return (s.startsWith("-") ? -1 : 1) * parseInt(s.replace(/^[-+]?0b/i, ""), 2);
  if (/^[-+]?\d+$/.test(s)) return parseInt(s, 10);
  if (/^[-+]?(\d+\.\d*|\.\d+|\d+)(e[-+]?\d+)?$/i.test(s)) return Number(s);
  if (s === "true") return 1;
  if (s === "false") return 0;
  return s;
}

/** 書く欄の最初の文字（いまの値。数は 10 進、ポインタは 16 進、列挙は名前） */
export function inputText(v: unknown): string {
  switch (kindOf(v)) {
    case "enum":
      return (v as { enum: string | null; value: number }).enum ?? String((v as { value: number }).value);
    case "pointer":
      return hex((v as { pointer: number }).pointer);
    default:
      return String(v);
  }
}
