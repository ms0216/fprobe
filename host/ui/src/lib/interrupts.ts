// Interrupts のビューの計算（SystemView・IAR Interrupt Log・MCUXpresso SWO Interrupt Trace の型）。Svelte・DOM に依らない。
// 材料は 2 つ: 記録の stream "rtos" の割り込みの区間（isr。時刻がタイムベース = 時間軸へ飛べる。SQL で読む）と、
// Code Profile の SWO 例外トレース（profile.exceptions。時刻はコアのクロック数なので時間軸へは飛べない）。
import type { Bar } from "./chart";
import type { DaemonConnection } from "./api";

type Conn = Pick<DaemonConnection, "call">;

/** 1 回の割り込み（記録の時刻） */
export interface Activation {
  t: number;
  tEnd: number;
  inclusive: number;
  self: number;
}

/** 割り込みごとの統計。時間は秒（unit の 1 秒あたりの数 hz で割った値） */
export interface IsrStat {
  number: number;
  name: string;
  count: number;
  total: number;
  self: number;
  min: number;
  max: number;
  mean: number;
  load: number | null;
  maxAt: number | null;            // 最長の回の始め（記録の時刻。SWO はコアのクロック数）
  maxEnd: number | null;
}

export const ISR_SQL = "SELECT t, t_end, a, json_extract(body, '$.name') FROM events WHERE stream = 'rtos' "
  + "AND kind = 'isr' AND t_end IS NOT NULL ORDER BY t";
export const SPAN_SQL = "SELECT MIN(t), MAX(COALESCE(t_end, t)), timebase_hz() FROM events WHERE stream = 'rtos'";

/** 入れ子の区間から、回ごとの自分の時間（中に入れ子になった回の時間を引く） */
export function activations(rows: [number, number, number, string | null][]): Map<number, { name: string; acts: Activation[] }> {
  const ordered = rows.map((r, i) => ({ r, i })).sort((x, y) => x.r[0] - y.r[0] || y.r[1] - x.r[1]);
  const nested = new Map<number, number>();
  const stack: { r: [number, number, number, string | null]; i: number }[] = [];
  for (const cur of ordered) {
    while (stack.length && stack[stack.length - 1].r[1] <= cur.r[0]) stack.pop();
    const parent = stack[stack.length - 1];
    if (parent && cur.r[1] <= parent.r[1]) nested.set(parent.i, (nested.get(parent.i) ?? 0) + cur.r[1] - cur.r[0]);
    stack.push(cur);
  }
  const out = new Map<number, { name: string; acts: Activation[] }>();
  rows.forEach(([t, tEnd, n, name], i) => {
    const e = out.get(n) ?? { name: name ?? `#${n}`, acts: [] };
    e.acts.push({ t, tEnd, inclusive: tEnd - t, self: tEnd - t - (nested.get(i) ?? 0) });
    out.set(n, e);
  });
  return out;
}

/** 回の並び → 統計（時間は秒） */
export function statsOf(groups: Map<number, { name: string; acts: Activation[] }>, hz: number,
                        span: number | null): IsrStat[] {
  const rows: IsrStat[] = [];
  for (const [number, { name, acts }] of groups) {
    if (!acts.length) continue;
    let total = 0, self = 0, min = Infinity, longest = acts[0];
    for (const a of acts) {
      total += a.inclusive;
      self += a.self;
      min = Math.min(min, a.inclusive);
      if (a.inclusive > longest.inclusive) longest = a;
    }
    rows.push({ number, name, count: acts.length, total: total / hz, self: self / hz, min: min / hz,
                max: longest.inclusive / hz, mean: total / acts.length / hz, load: span ? self / span : null,
                maxAt: longest.t, maxEnd: longest.tEnd });
  }
  return rows.sort((a, b) => b.self - a.self);
}

/** 記録の割り込み（統計と回の並び） */
export async function recordedIsrs(conn: Conn): Promise<{ stats: IsrStat[]; groups: Map<number, { name: string; acts: Activation[] }>;
                                                         hz: number; truncated: boolean }> {
  const [r, s] = await Promise.all([conn.call("recording.query", { sql: ISR_SQL, limit: 100000 }, { quiet: true }),
                                    conn.call("recording.query", { sql: SPAN_SQL }, { quiet: true })]);
  const [t0, t1, hz] = s.rows?.[0] ?? [0, 0, 1];
  const groups = activations(r.rows ?? []);
  return { stats: statsOf(groups, hz || 1, t1 > t0 ? t1 - t0 : null), groups, hz: hz || 1, truncated: !!r.truncated };
}

/** profile.exceptions の結果 → 統計（秒。clock_hz が無ければクロック数のまま） */
export function statsFromProfile(r: any): IsrStat[] {
  const hz = r.clock_hz || 1;
  return (r.exceptions ?? []).map((e: any): IsrStat => ({
    number: e.exception, name: e.name, count: e.count, total: e.total_inclusive / hz, self: e.total_self / hz,
    min: e.inclusive.min / hz, max: e.inclusive.max / hz, mean: e.inclusive.mean / hz, load: e.load ?? null,
    maxAt: e.max_at ?? null, maxEnd: e.max_end ?? null,
  }));
}

/** 分布図: 時間（含む）を bins 本の棒に。外れ値（Q3 + 1.5 IQR を超える）を含む棒に印 */
export interface Histogram {
  bars: Bar[];
  outliers: Activation[];          // 長い順
  bins: Activation[][];            // 棒ごとの回
}

export function histogram(acts: Activation[], bins = 24): Histogram {
  if (!acts.length) return { bars: [], outliers: [], bins: [] };
  const sorted = acts.map((a) => a.inclusive).sort((a, b) => a - b);
  const q = (p: number) => sorted[Math.min(sorted.length - 1, Math.floor(p * (sorted.length - 1)))];
  const fence = q(0.75) + 1.5 * (q(0.75) - q(0.25));
  const integral = sorted.every(Number.isInteger);
  // 整数（タイムベースの数）で種類が棒の数より少なければ 1 つの値に 1 本（空の棒を挟まない）
  const narrow = integral && sorted[sorted.length - 1] - sorted[0] + 1 <= bins;
  const lo = narrow ? sorted[0] - 0.5 : sorted[0], hi = narrow ? sorted[sorted.length - 1] + 0.5 : sorted[sorted.length - 1];
  const n = narrow ? hi - lo : hi > lo ? bins : 1;
  const width = narrow ? 1 : hi > lo ? (hi - lo) / n : 1;
  const groups: Activation[][] = Array.from({ length: n }, () => []);
  for (const a of acts) groups[Math.min(n - 1, Math.floor((a.inclusive - lo) / width))].push(a);
  const bars = groups.map((g, i): Bar => ({ x0: lo + i * width, x1: lo + (i + 1) * width, y: g.length,
                                          mark: g.some((a) => a.inclusive > fence) }));
  const outliers = acts.filter((a) => a.inclusive > fence).sort((a, b) => b.inclusive - a.inclusive);
  return { bars, outliers, bins: groups };
}

/** 棒の中でいちばん長い回（時間軸へ飛ぶ先） */
export function longestIn(h: Histogram, bar: number | null): Activation | null {
  const g = bar === null ? null : h.bins[bar];
  if (!g?.length) return null;
  return g.reduce((a, b) => (b.inclusive > a.inclusive ? b : a));
}
