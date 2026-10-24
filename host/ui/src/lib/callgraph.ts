// Call Graph のビューの計算（IAR・Keil の Call Graph / Stack Usage の型）。Svelte・DOM に依らない。
// 材料は debug.stack.static（ELF だけ。機器に触らない）の関数ごとの枠・呼び先を含む最大・最深の経路・直接の呼び先と、
// 根（リセット・例外・呼ばれない関数・関数ポインタの呼び先の候補）。プロファイルがあれば関数の割合も並べる。

/** debug.stack.static の functions の要素 */
export interface StackFunction {
  function: string;
  address?: number;
  frame: number;
  frame_source: string;
  max: number;
  bounded: boolean;
  reasons: string[];
  unknown_from: string[];
  path: string[];
  calls?: string[];
}

export interface StackRoot {
  root: string;
  kind: string;                    // reset / exception / uncalled / address_taken / user
  max: number;
  bounded: boolean;
  path: string[];
  exceptions?: number[];
  total?: number;
}

/** 木の見える行 */
export interface GraphRow {
  key: string;                     // 根からの経路（"a/b/c"）。開いた状態の鍵
  name: string;
  depth: number;
  fn: StackFunction | null;        // ELF に無い（解析できない）呼び先は null
  root: StackRoot | null;          // 根の行だけ
  hasChildren: boolean;
  open: boolean;
  recursive: boolean;              // 経路の上に同じ関数がある（それ以上開かない）
  deepest: boolean;                // 根の最深の経路の上
}

/** 根の並び: 報告（debug.stack.static の roots）の並びのまま（画面で種類・大きさの順に並べ替えない）。重複は除く */
export function rootsOf(report: { roots?: StackRoot[] }): StackRoot[] {
  const seen = new Set<string>();
  return (report.roots ?? []).filter((r) => !seen.has(r.root) && seen.add(r.root));
}

/** 開いている行（open: 経路の鍵）を木の順に並べる */
export function treeRows(functions: StackFunction[], roots: StackRoot[], open: Set<string>): GraphRow[] {
  const by = new Map(functions.map((f) => [f.function, f]));
  const out: GraphRow[] = [];
  const walk = (name: string, depth: number, trail: string[], key: string, root: StackRoot | null,
                deepest: string[] | null) => {
    const fn = by.get(name) ?? null;
    const recursive = trail.includes(name);
    const calls = recursive ? [] : fn?.calls ?? [];
    const onPath = !!deepest && deepest[depth] === name;          // 根から最深の経路をたどっている
    const isOpen = open.has(key) && calls.length > 0;
    out.push({ key, name, depth, fn, root, hasChildren: calls.length > 0, open: isOpen, recursive,
               deepest: onPath && depth > 0 });
    if (!isOpen) return;
    for (const c of calls) walk(c, depth + 1, [...trail, name], `${key}/${c}`, null, onPath ? deepest : null);
  };
  for (const r of roots) walk(r.root, 0, [], r.root, r, r.path);
  return out;
}

// --- ベクタ表の欄 ----------------------------------------------------------------------------------------------
// 例外の根と MSP の見積もりは、使う側が明示したベクタ表の番地（と大きさ）で出る（debug.stack.static の vector_table・
// vector_table_size）。画面は名前で表を探さず、VTOR も読んで欄に入れるだけ（自動で使わない）

/** VTOR（SCB の Vector Table Offset Register。Cortex-M のアーキテクチャの規格で決まる番地） */
export const VTOR = 0xe000ed08;

export interface VectorTableForm {
  address: string;                 // 空 = 渡さない
  size: string;                    // 空 = ELF のその番地の変数の大きさ（デーモンが決める）
}

const INTEGER = /^(0x[0-9a-f]+|\d+)$/i;
const integer = (text: string): number | null => (INTEGER.test(text) ? Number(text) : null);

/** 欄から debug.stack.static の引数（0x は 16 進、ほかは 10 進）。誤りは欄の名前 */
export function vectorTableParams(form: VectorTableForm):
    { params: { vector_table?: number; vector_table_size?: number } } | { error: "address" | "size" } {
  const a = form.address.trim(), s = form.size.trim();
  if (!a && !s) return { params: {} };
  const address = integer(a);
  if (address === null || address > 0xffffffff) return { error: "address" };
  if (!s) return { params: { vector_table: address } };
  const size = integer(s);
  if (size === null || size < 1) return { error: "size" };
  return { params: { vector_table: address, vector_table_size: size } };
}

/** 使った表（debug.stack.static の結果の vector_table）の欄。無ければ空 */
export function vectorTableForm(table: { address: number; size: number } | null | undefined): VectorTableForm {
  if (!table) return { address: "", size: "" };
  return { address: `0x${table.address.toString(16).toUpperCase().padStart(8, "0")}`, size: String(table.size) };
}

/** target.memory.read（16 進の文字列。番地の順のバイト）の 4 バイトを 32 ビットの値に（リトル エンディアン）。
 *  4 バイトでなければ null */
export function vtorValue(hex: string | null | undefined): number | null {
  if (typeof hex !== "string" || !/^[0-9a-f]{8}$/i.test(hex)) return null;
  const b = hex.match(/../g)!.map((x) => parseInt(x, 16));
  return (b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)) >>> 0;
}

/** 根の最深の経路をすべて開く鍵（「最深の経路を開く」） */
export function deepestKeys(root: StackRoot): string[] {
  return root.path.map((_, i) => root.path.slice(0, i + 1).join("/"));
}
