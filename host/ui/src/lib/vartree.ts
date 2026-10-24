// 変数の木（Watch・Locals）: デーモンの解読した値（fprobe.dwarf.values の形）を、展開できる行に分ける。Svelte に依らない。
//
// 値の形（debug.evaluate / debug.frame_variables / watch.*）: どの値も kind（値の種類。デーモンが DWARF の型で決めた
// 事実）を持つ object。画面は kind を読むだけで、キーの有無・型の名前から種類を推さない:
//   int・float・bool → {kind, value}（float の NaN・無限大は value が "nan" "inf" "-inf"）、bytes → {kind, hex}、
//   enum → {kind, name, value}、pointer → {kind, address, to, symbol?, string?}、struct → {kind, members}、
//   array → {kind, length, items, string?}（items が length より少なければ max_elements で切った）、
//   unavailable → {kind, reason}
// kind の無いもの（古い形で残った記録の値等）は "none": JSON のまま出す（形を推して読み替えない）。
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

const KINDS = ["int", "float", "bool", "bytes", "enum", "pointer", "struct", "array", "unavailable"] as const;
export type ValueKind = (typeof KINDS)[number] | "none";

const isObject = (v: unknown): v is Record<string, any> => !!v && typeof v === "object" && !Array.isArray(v);

/** 値の kind（デーモンが付けた欄）。kind の無いもの・知らない kind は "none" */
export function kindOf(v: unknown): ValueKind {
  const k = isObject(v) ? v.kind : undefined;
  return (KINDS as readonly unknown[]).includes(k) ? (k as ValueKind) : "none";
}

/** value の欄に 1 つの数（真偽）を持つ種類 */
const SCALARS: ValueKind[] = ["int", "float", "bool", "enum"];

/** 値の数（int・float・bool・enum の value。float の NaN・無限大は "nan" 等の文字列のまま）。ほかの種類は null */
export function scalarOf(v: unknown): number | boolean | string | null {
  return SCALARS.includes(kindOf(v)) ? (v as { value: number | boolean | string }).value : null;
}

/** 配列の値が max_elements で切られている（items が length より少ない） */
export function truncated(v: unknown): boolean {
  return kindOf(v) === "array" && (v as { items: unknown[] }).items.length < (v as { length: number }).length;
}

const hex = (n: number) => `0x${(n >>> 0).toString(16).toUpperCase()}`;

/** kind の無い値（古い形）: JSON のまま */
const raw = (v: unknown) => (v !== null && typeof v === "object" ? JSON.stringify(v) : String(v));

/** 値の列の文字（短く。構造体・配列は中身の一部） */
export function display(v: unknown, limit = 64): string {
  const text = (() => {
    const o = v as Record<string, any>;
    switch (kindOf(v)) {
      case "int":
        return `${o.value} (${hex(o.value)})`;
      case "float":                                    // JSON では 3.0 が 3 になるので、種類で小数として表す
        return typeof o.value === "number" && Number.isInteger(o.value) ? o.value.toFixed(1) : String(o.value);
      case "bool":
        return String(o.value);
      case "bytes":
        return o.hex;
      case "enum":
        return o.name ? `${o.name} (${o.value})` : String(o.value);
      case "pointer": {
        const pad = `0x${(o.address >>> 0).toString(16).toUpperCase().padStart(8, "0")}`;
        if (o.string !== undefined && o.string !== null) return `${pad} "${o.string}"`;
        return o.symbol ? `${pad} <${o.symbol}>` : pad;
      }
      case "array": {
        if (typeof o.string === "string") return `"${o.string}"`;
        const items = (o.items as unknown[]).map((x) => brief(x)).join(", ");
        return truncated(v) ? `[${o.length}] [${items}, …]` : `[${items}]`;
      }
      case "struct":
        return `{${Object.entries(o.members as object).map(([k, x]) => `${k}: ${brief(x)}`).join(", ")}}`;
      case "unavailable":
        return `<${o.reason}>`;
      default:
        return raw(v);
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
      return typeof (v as { string?: unknown }).string === "string" ? display(v, 24) : "[…]";
    case "int":
      return String((v as { value: number }).value);
    default:
      return display(v, 24);
  }
}

/** void・関数へのポインタは先を読まない */
function derefable(p: { address: number; to: string }): boolean {
  return p.address !== 0 && !/^(const |volatile )*void$/.test(p.to.trim()) && !/\(/.test(p.to);
}

export function expandable(v: unknown): boolean {
  switch (kindOf(v)) {
    case "struct":
      return Object.keys((v as { members: object }).members).length > 0;
    case "array":
      return (v as { length: number }).length > 0;
    case "pointer":
      return derefable(v as { address: number; to: string });
    default:
      return false;
  }
}

/** その場で書ける値（基本型・列挙・ポインタ）。構造体・配列は中のメンバーを書く */
export function editable(node: VarNode): boolean {
  if (!node.expr || node.error) return false;
  return ["int", "float", "bool", "enum", "pointer"].includes(kindOf(node.value));
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

/** 値に入っている子（構造体のメンバー・配列の要素。切った配列は読んだ分）。
 *  members はメンバーの型（debug.type_info から。分からなければ空） */
export function childrenOf(node: VarNode, members: Record<string, string> = {}): VarNode[] {
  const v = node.value as Record<string, any>;
  const base = node.expr;
  const element = elementType(node.type);
  switch (kindOf(v)) {
    case "array":
      return (v.items as unknown[]).map((x, i) => ({
        key: `${node.key}[${i}]`, name: `[${i}]`, expr: base === null ? null : `${wrap(base)}[${i}]`, type: element, value: x,
      }));
    case "struct":
      return Object.entries(v.members as object).map(([name, x]) => ({
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
    return Object.entries((target.value as { members: object }).members).map(([name, x]) => ({
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
  const o = v as Record<string, any>;
  switch (kindOf(v)) {
    case "enum":
      return o.name ?? String(o.value);
    case "pointer":
      return hex(o.address);
    case "int":
    case "float":
    case "bool":
      return String(o.value);
    default:
      return display(v);
  }
}
