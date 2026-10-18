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

/** 根の並び: 報告の根（リセット → 例外 → 呼ばれない関数 → その他）。重複は除く */
export function rootsOf(report: { roots?: StackRoot[] }): StackRoot[] {
  const order = ["reset", "exception", "user", "uncalled", "address_taken"];
  const seen = new Set<string>();
  return [...(report.roots ?? [])]
    .sort((a, b) => (order.indexOf(a.kind) + 99) % 99 - (order.indexOf(b.kind) + 99) % 99 || b.max - a.max)
    .filter((r) => !seen.has(r.root) && seen.add(r.root));
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

/** 根の最深の経路をすべて開く鍵（「最深の経路を開く」） */
export function deepestKeys(root: StackRoot): string[] {
  return root.path.map((_, i) => root.path.slice(0, i + 1).join("/"));
}
