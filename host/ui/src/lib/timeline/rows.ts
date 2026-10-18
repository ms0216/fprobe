// 時間軸の段（行）の表と並び: タイルの見出しから知った段（ロジアナのチャンネル・記録の段）を覚え、利用者の並べ替え・
// 表示 / 非表示・まとまりの畳みを当てて、描く段の並び（上から）を作る。Svelte・DOM に依らない。
//
// - まとまり（group）= ロジアナ（logic）と記録の stream。まとまりの見出しの段は、その stream の件数の密度を描く
//   （畳むと見出しだけ）。段の名前（id）は記録の中で一意（fprobe.timeline_rows.row_id）で、並びと表示はこれで覚える。
// - 段はタイルごとに増えることがある（記録が伸びる）。一度知った段は源（捕捉 / 記録）を変えるまで消さない。
// 仕様: docs/superpowers/specs/2026-10-18-timeline-tiles-design.md §5.1
import type { EventRowType, RowHeader, TileHeader } from "./tile";

export type RowType = "logic" | "group" | EventRowType;

export interface RowDef {
  id: string;                         // "wave:<ch>" / "group:<まとまり>" / 記録の段の名前
  group: string;                      // "logic" か stream
  type: RowType;
  title: string | null;               // データから（タスクの名前・チャンネルの名前）。無ければ規則の名前で
  rule?: string;
  unit?: string;
  jump?: "memory" | "source";
  channel?: number;
  color: number;                      // パレットの番号（まとまりの中で順に）
  hiddenByDefault?: boolean;
}

export interface GroupDef {
  id: string;
  rows: RowDef[];
}

/** 利用者の段の構成（ブラウザに覚える） */
export interface RowConfig {
  groups: string[];                   // まとまりの並び（知らないものは後ろに知った順）
  rows: Record<string, string[]>;     // まとまりの中の段の並び
  hidden: string[];                   // 隠した段（既定で隠す段を見せたら shown に）
  shown: string[];
  folded: string[];                   // 畳んだまとまり
}

export const EMPTY_CONFIG: RowConfig = { groups: [], rows: {}, hidden: [], shown: [], folded: [] };

/** 描く段（上から） */
export interface VisibleRow {
  def: RowDef;
  header: boolean;                    // まとまりの見出し
  folded: boolean;                    // 見出しのとき: 畳んでいる
  count: number;                      // 見出しのとき: まとまりの中の段の数（隠したものを含む）
}

export const PALETTE_SIZE = 12;       // 段の色（layout.ts の PALETTE と同じ数）

export const ROW_HEIGHT: Record<RowType, number> = {
  group: 20, logic: 22, intervals: 20, bus: 22, state: 18, points: 18, values: 44,
};

export function groupId(group: string): string {
  return `group:${group}`;
}

export class RowRegistry {
  private groups = new Map<string, GroupDef>();
  private ids = new Set<string>();

  /** タイルの見出しから段を足す。増えたら true */
  merge(header: TileHeader): boolean {
    let changed = false;
    const w = header.wave;
    w.channels.forEach((ch, k) => {
      changed = this.add("logic", { id: `wave:${ch}`, group: "logic", type: "logic", channel: ch,
                                    title: w.names?.[k] ?? null, color: 0 }) || changed;
    });
    for (const lane of header.lanes) {
      const rows: RowHeader[] = lane.rows ?? [{ id: lane.stream, rule: "all", type: "intervals", title: lane.stream }];
      if (!this.groups.has(lane.stream)) {
        this.groups.set(lane.stream, { id: lane.stream, rows: [] });
        changed = true;
      }
      for (const r of rows) {
        changed = this.add(lane.stream, { id: r.id, group: lane.stream, type: r.type, title: r.title, rule: r.rule,
                                          unit: r.unit, jump: r.jump, hiddenByDefault: r.hidden, color: 0 }) || changed;
      }
    }
    return changed;
  }

  private add(group: string, def: RowDef): boolean {
    let g = this.groups.get(group);
    if (!g) this.groups.set(group, (g = { id: group, rows: [] }));
    if (this.ids.has(def.id)) {
      const known = g.rows.find((r) => r.id === def.id);
      if (known && def.title && !known.title) known.title = def.title;
      return false;
    }
    this.ids.add(def.id);
    def.color = g.rows.length % PALETTE_SIZE;
    g.rows.push(def);
    return true;
  }

  clear(): void {
    this.groups.clear();
    this.ids.clear();
  }

  get size(): number {
    return this.ids.size;
  }

  /** まとまり（構成の並びを当てたもの） */
  ordered(config: RowConfig): GroupDef[] {
    const known = [...this.groups.values()];
    const rank = (list: string[], id: string) => {
      const i = list.indexOf(id);
      return i < 0 ? Infinity : i;
    };
    const groups = known.map((g, i) => ({ g, i }))
      .sort((a, b) => rank(config.groups, a.g.id) - rank(config.groups, b.g.id) || a.i - b.i).map(({ g }) => g);
    return groups.map((g) => {
      const order = config.rows[g.id] ?? [];
      const rows = g.rows.map((r, i) => ({ r, i }))
        .sort((a, b) => rank(order, a.r.id) - rank(order, b.r.id) || a.i - b.i).map(({ r }) => r);
      return { id: g.id, rows };
    });
  }

  /** 描く段の並び */
  visible(config: RowConfig): VisibleRow[] {
    const out: VisibleRow[] = [];
    for (const g of this.ordered(config)) {
      if (!g.rows.length && g.id === "logic") continue;
      const folded = config.folded.includes(g.id);
      out.push({ def: { id: groupId(g.id), group: g.id, type: "group", title: null, color: 0 }, header: true, folded,
                 count: g.rows.length });
      if (folded) continue;
      for (const r of g.rows) if (isShown(r, config)) out.push({ def: r, header: false, folded: false, count: 0 });
    }
    return out;
  }
}

export function isShown(r: RowDef, config: RowConfig): boolean {
  if (config.hidden.includes(r.id)) return false;
  return !r.hiddenByDefault || config.shown.includes(r.id);
}

// --- 構成の操作（新しい構成を返す。元は変えない） -----------------------------------------------------------

export function toggleFold(c: RowConfig, group: string): RowConfig {
  const folded = c.folded.includes(group) ? c.folded.filter((g) => g !== group) : [...c.folded, group];
  return { ...c, folded };
}

export function setShown(c: RowConfig, r: RowDef, show: boolean): RowConfig {
  const hidden = c.hidden.filter((id) => id !== r.id), shown = c.shown.filter((id) => id !== r.id);
  if (!show) hidden.push(r.id);
  else if (r.hiddenByDefault) shown.push(r.id);
  return { ...c, hidden, shown };
}

/** まとまりの中で段 id を before の前へ（before = null なら最後へ） */
export function moveRow(c: RowConfig, groups: GroupDef[], id: string, before: string | null): RowConfig {
  const g = groups.find((x) => x.rows.some((r) => r.id === id));
  if (!g || before === id) return c;
  const order = g.rows.map((r) => r.id).filter((x) => x !== id);
  const at = before === null ? order.length : order.indexOf(before);
  if (at < 0) return c;
  order.splice(at, 0, id);
  return { ...c, rows: { ...c.rows, [g.id]: order } };
}

/** まとまり group を before の前へ */
export function moveGroup(c: RowConfig, groups: GroupDef[], group: string, before: string | null): RowConfig {
  if (before === group) return c;
  const order = groups.map((g) => g.id).filter((x) => x !== group);
  const at = before === null ? order.length : order.indexOf(before);
  if (at < 0) return c;
  order.splice(at, 0, group);
  return { ...c, groups: order };
}

export function parseConfig(text: string | null): RowConfig {
  try {
    const v = JSON.parse(text ?? "");
    const list = (x: unknown) => (Array.isArray(x) ? x.filter((s) => typeof s === "string") : []);
    const rows: Record<string, string[]> = {};
    if (v && typeof v.rows === "object") for (const [k, x] of Object.entries(v.rows)) rows[k] = list(x);
    return { groups: list(v?.groups), rows, hidden: list(v?.hidden), shown: list(v?.shown), folded: list(v?.folded) };
  } catch {
    return EMPTY_CONFIG;
  }
}

/** 段の上端（CSS の画素。目盛りの帯の下から）と全体の高さ */
export function rowTops(rows: VisibleRow[]): { tops: number[]; heights: number[]; total: number } {
  const tops: number[] = [], heights: number[] = [];
  let y = 0;
  for (const r of rows) {
    tops.push(y);
    const h = ROW_HEIGHT[r.def.type];
    heights.push(h);
    y += h;
  }
  return { tops, heights, total: y };
}
