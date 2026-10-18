// RTOS のビュー（Threads・CPU Load）の計算。Svelte・DOM に依らない。
// 材料は 2 つ: 取っている（取った）タスクの時間軸（rtos.timeline.get / load。デーモンが要約する）と、記録の
// stream "rtos" の区間（run = タスクの実行、isr = 割り込み、gap = 抜け。recording.query の SQL・recording.find）。
// 記録だけがあるときも、時間軸と同じ表（タスクごとの使用率・回数・最長・最短・平均）と負荷の推移を作る
// （本物の Probe にそのまま渡せる問い合わせだけを使う）。
import type { DaemonConnection } from "./api";
import type { Series } from "./chart";
import type { Key, Translate } from "./i18n";

type Conn = Pick<DaemonConnection, "call">;
const quiet = { quiet: true };

/** 記録から読む区間の上限（recording.query の limit の最大） */
export const ROW_LIMIT = 100000;

/** タスクごとの統計（時間はタイムベースの単位と µs の両方） */
export interface TaskStat {
  task: string;
  handle: number;
  cpu: number;               // 情報のある時間に対する割合
  runs: number;
  total: number;
  max: number;
  min: number;
  mean: number;
}

export interface TaskTable {
  tasks: TaskStat[];         // 使用率の多い順
  hz: number;                // 1 秒のタイムベース
  t0: number;
  t1: number;
  covered: number;           // 情報のある時間（抜けを除く）
  lowerBound: boolean;       // 回数が下限（方法 sampled）
}

/** 窓ごとの負荷（rtos.timeline.load の windows と同じ形） */
export interface LoadWindow {
  t0: number;
  t1: number;
  covered: number;
  load: number | null;       // 1 − アイドルの割合（割り込みは負荷に入る）。情報が無ければ null
  isr: number | null;
  tasks: Record<string, number>;
}

export interface LoadSeries {
  window: number;            // 窓の幅（タイムベース）
  windows: LoadWindow[];
  peak: { t0: number; t1: number; load: number } | null;
  meanLoad: number | null;
}

/** アイドルのタスクの名前（FreeRTOS の IDLE・Zephyr の idle 等） */
export const isIdle = (name: string): boolean => /^idle\b/i.test(name.trim());

// --- 取った時間軸（デーモンの要約） ---------------------------------------------------------------------

/** rtos.timeline.get の結果 → 表 */
export function tableFromTimeline(r: any): TaskTable {
  const tasks: TaskStat[] = Object.entries<any>(r.stats?.tasks ?? {}).map(([task, s]) => ({
    task, handle: s.handle, cpu: s.cpu, runs: s.runs, total: s.total, max: s.max_run, min: s.min_run, mean: s.mean_run,
  }));
  return { tasks: tasks.sort((a, b) => b.cpu - a.cpu), hz: r.timebase_hz, t0: r.t0, t1: r.t1,
           covered: r.stats?.covered ?? r.t1 - r.t0, lowerBound: !!r.lower_bound };
}

/** rtos.timeline.load の結果 → 推移 */
export function loadFromTimeline(r: any): LoadSeries {
  return { window: r.window, windows: r.windows ?? [], peak: r.peak ?? null, meanLoad: r.mean_load ?? null };
}

// --- 記録 ---------------------------------------------------------------------------------------

const RUNS_SQL = "SELECT t, t_end, a, json_extract(body, '$.task') FROM events WHERE stream = 'rtos' AND kind = 'run' "
  + "AND t_end IS NOT NULL ORDER BY t";
const ISRS_SQL = "SELECT t, t_end, a, json_extract(body, '$.name') FROM events WHERE stream = 'rtos' AND kind = 'isr' "
  + "AND t_end IS NOT NULL ORDER BY t";
const GAPS_SQL = "SELECT t, t_end FROM events WHERE stream = 'rtos' AND kind = 'gap' AND t_end IS NOT NULL ORDER BY t";
/** タスクごとの集計は SQL で（区間がいくら多くても正確） */
export const TASK_STATS_SQL = "SELECT a, json_extract(body, '$.task'), COUNT(*), SUM(t_end - t), MAX(t_end - t), "
  + "MIN(t_end - t), MIN(t), MAX(t_end), timebase_hz() FROM events WHERE stream = 'rtos' AND kind = 'run' "
  + "AND t_end IS NOT NULL GROUP BY a";
/** その時刻に動いていたタスク */
export const RUN_AT_SQL = "SELECT a, json_extract(body, '$.task'), t, t_end FROM events WHERE stream = 'rtos' "
  + "AND kind = 'run' AND t <= :t AND t_end > :t ORDER BY t DESC LIMIT 1";

export type Interval = [number, number];

/** 記録の区間（タスクの実行・割り込み・抜け） */
export interface RecordedRuns {
  runs: [number, number, number, string][];          // t0, t1, TCB, タスク名
  isrs: [number, number, number, string][];          // t0, t1, 例外番号, 名前
  gaps: Interval[];
  truncated: boolean;                                // どれかが ROW_LIMIT で切れた
}

export async function recordedRuns(conn: Conn): Promise<RecordedRuns> {
  const [runs, isrs, gaps] = await Promise.all([RUNS_SQL, ISRS_SQL, GAPS_SQL]
    .map((sql) => conn.call("recording.query", { sql, limit: ROW_LIMIT }, quiet)));
  return { runs: runs.rows, isrs: isrs.rows, gaps: gaps.rows,
           truncated: !!(runs.truncated || isrs.truncated || gaps.truncated) };
}

/** 記録の run から表（SQL で集計）。使用率の分母はタスクの実行の区間の合計（= 情報のある時間。止まっていた間・
 *  記録していない間は run が無いので数えない） */
export async function recordedTable(conn: Conn): Promise<TaskTable | null> {
  const stats = await conn.call("recording.query", { sql: TASK_STATS_SQL, limit: 10000 }, quiet);
  const rows: any[][] = stats.rows ?? [];
  if (!rows.length) return null;
  const t0 = Math.min(...rows.map((r) => r[6])), t1 = Math.max(...rows.map((r) => r[7]));
  const covered = Math.max(1, rows.reduce((sum, r) => sum + r[3], 0));
  const tasks = rows.map((r): TaskStat => ({ handle: r[0], task: r[1] ?? `0x${(r[0] >>> 0).toString(16)}`, runs: r[2],
                                             total: r[3], max: r[4], min: r[5], mean: r[3] / r[2],
                                             cpu: r[3] / covered }));
  return { tasks: tasks.sort((a, b) => b.cpu - a.cpu), hz: rows[0][8], t0, t1, covered, lowerBound: false };
}

/** 区間の和集合（昇順・重ならない） */
export function union(intervals: Interval[]): Interval[] {
  const sorted = [...intervals].filter(([a, b]) => b > a).sort((x, y) => x[0] - y[0]);
  const out: Interval[] = [];
  for (const [a, b] of sorted) {
    const last = out[out.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b);
    else out.push([a, b]);
  }
  return out;
}

/** 区間の差（a − b。どちらも和集合にしてから） */
export function subtract(a: Interval[], b: Interval[]): Interval[] {
  const cut = union(b);
  const out: Interval[] = [];
  for (const [a0, a1] of union(a)) {
    let start = a0;
    for (const [b0, b1] of cut) {
      if (b1 <= start || b0 >= a1) continue;
      if (b0 > start) out.push([start, b0]);
      start = Math.max(start, b1);
    }
    if (start < a1) out.push([start, a1]);
  }
  return out;
}

/** 重ならない昇順の区間の、[a, b) との重なりの長さ */
export function overlap(sorted: Interval[], a: number, b: number): number {
  let lo = 0, hi = sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (sorted[mid][1] <= a) lo = mid + 1;
    else hi = mid;
  }
  let sum = 0;
  for (let i = lo; i < sorted.length && sorted[i][0] < b; i++) sum += Math.min(b, sorted[i][1]) - Math.max(a, sorted[i][0]);
  return sum;
}

/** 窓ごとの負荷（デーモンの rtos.timeline.load と同じ定義: 割合の分母は窓の中の情報のある時間、タスクの時間から
 *  割り込みの時間を除く、負荷 = 1 − アイドルの割合）。記録では、情報のある時間 = タスクの実行の区間 − 抜け
 *  （止まっていた間・記録していない間は run が無い） */
export function loadWindows(rec: Pick<RecordedRuns, "runs" | "isrs" | "gaps">, window: number,
                            span?: Interval): LoadSeries {
  const empty: LoadSeries = { window, windows: [], peak: null, meanLoad: null };
  if (!(window > 0) || !rec.runs.length) return empty;
  const t0 = span?.[0] ?? rec.runs[0][0];
  const t1 = span?.[1] ?? Math.max(...rec.runs.map((r) => r[1]));
  if (t1 <= t0) return empty;
  const info = subtract(rec.runs.map(([a, b]) => [a, b] as Interval), rec.gaps);
  const isr = subtract(rec.isrs.map(([a, b]) => [a, b] as Interval), rec.gaps);
  const gaps = union(rec.gaps);
  const count = Math.ceil((t1 - t0) / window);
  const time: Map<string, number>[] = Array.from({ length: count }, () => new Map());
  for (const [a0, a1, , name] of rec.runs) {
    for (let k = Math.max(0, Math.floor((a0 - t0) / window)); k < count; k++) {
      const w0 = t0 + k * window;
      if (w0 >= a1) break;
      const a = Math.max(a0, w0), b = Math.min(a1, w0 + window);
      if (b > a) time[k].set(name, (time[k].get(name) ?? 0) + (b - a) - overlap(gaps, a, b) - overlap(isr, a, b));
    }
  }
  const windows: LoadWindow[] = time.map((m, k) => {
    const w0 = t0 + k * window, w1 = Math.min(w0 + window, t1);
    const covered = overlap(info, w0, w1);
    if (covered <= 0) return { t0: w0, t1: w1, covered: 0, load: null, isr: null, tasks: {} };
    let idle = 0;
    const tasks: Record<string, number> = {};
    for (const [name, v] of [...m].sort((x, y) => y[1] - x[1])) {
      if (isIdle(name)) idle += v;
      if (v > 0) tasks[name] = v / covered;
    }
    return { t0: w0, t1: w1, covered, load: Math.max(0, 1 - idle / covered), isr: overlap(isr, w0, w1) / covered, tasks };
  });
  const loads = windows.filter((w) => w.load !== null);
  const peak = loads.reduce<LoadWindow | null>((p, w) => (!p || w.load! > p.load! ? w : p), null);
  const covered = loads.reduce((s, w) => s + w.covered, 0);
  return { window, windows, peak: peak && { t0: peak.t0, t1: peak.t1, load: peak.load! },
           meanLoad: covered ? loads.reduce((s, w) => s + w.load! * w.covered, 0) / covered : null };
}

/** 推移の窓の中の時刻 t を含む窓（無ければ null） */
export function windowAt(series: LoadSeries, t: number): LoadWindow | null {
  return series.windows.find((w) => t >= w.t0 && t < w.t1) ?? null;
}

/** 多い順のタスクの名前（グラフの系列。アイドルは除く） */
export function busyTasks(series: LoadSeries, max = 6): string[] {
  const sum = new Map<string, number>();
  for (const w of series.windows) for (const [k, v] of Object.entries(w.tasks)) sum.set(k, (sum.get(k) ?? 0) + v * w.covered);
  return [...sum].filter(([k]) => !isIdle(k)).sort((a, b) => b[1] - a[1]).slice(0, max).map(([k]) => k);
}

/** CPU 負荷の積み上げ（Chart の type stacked）の系列（%）: 下から多い順のタスク・割り込み・その他（負荷 − 割り込み −
 *  挙げたタスク）。上端が負荷になる。x は窓の始めを xOf で変えたもの。情報の無い窓（load = null）は NaN（線を切る）。
 *  focus のタスクがあれば、ほかの系列は薄く */
export function loadStack(series: LoadSeries, xOf: (t: number) => number, labels: { isr: string; other: string },
                          focus: string | null = null, max = 6): Series[] {
  const ws = series.windows;
  const x = ws.map((w) => xOf(w.t0));
  const names = busyTasks(series, max);
  const pct = (w: LoadWindow, v: number) => (w.load === null ? NaN : v * 100);
  const out: Series[] = names.map((name) => ({ name, x, y: ws.map((w) => pct(w, w.tasks[name] ?? 0)), step: true,
                                               unit: "%" }));
  if (ws.some((w) => w.isr)) out.push({ name: labels.isr, x, y: ws.map((w) => pct(w, w.isr ?? 0)), step: true, unit: "%" });
  const rest = ws.map((w) => pct(w, Math.max(0, (w.load ?? 0) - (w.isr ?? 0)
                                          - names.reduce((sum, n) => sum + (w.tasks[n] ?? 0), 0))));
  if (rest.some((v) => v > 0.05)) out.push({ name: labels.other, x, y: rest, step: true, unit: "%" });
  return focus ? out.map((s) => ({ ...s, dim: s.name !== focus })) : out;
}

// --- Threads と時間軸 -----------------------------------------------------------------------------

/** 記録のその時刻に動いていたタスク（無ければ null） */
export async function runningAt(conn: Conn, t: number): Promise<{ handle: number; task: string } | null> {
  const r = await conn.call("recording.query", { sql: RUN_AT_SQL, params: { t } }, quiet);
  const row = r.rows?.[0];
  return row ? { handle: row[0], task: row[1] } : null;
}

/** 記録で、そのタスク（TCB）の after より後に始まる最初の実行の区間（無ければ null） */
export async function nextRun(conn: Conn, handle: number, after: number): Promise<{ t: number; tEnd: number } | null> {
  const r = await conn.call("recording.find", { query: { stream: "rtos", kind: "run", a: handle, t0: after + 1 } }, quiet);
  let best: { t: number; tEnd: number } | null = null;
  for (const e of r.events ?? []) {
    if (e.t > after && (!best || e.t < best.t)) best = { t: e.t, tEnd: e.t_end ?? e.t };
  }
  return best;
}

// --- 文言（状態・待っているもの。デーモンが返す語は英語の識別子なので、表に訳がある語は訳す） -------------------

const STATES = ["running", "ready", "blocked", "suspended", "deleted"] as const;
const REASONS = ["delay", "receive", "send", "event", "notification", "suspended", "sleep", "pend", "bits"] as const;

/** タスクの状態（rtos.tasks の state）。知らない状態はそのまま */
export function taskStateText(state: string, tr: Translate): string {
  return (STATES as readonly string[]).includes(state) ? tr(`threads.state.${state}` as Key) : state;
}

/** 待つ理由（タスクの wait.reason・オブジェクトの waiters の reason）。知らない理由はそのまま */
export function reasonText(reason: string, tr: Translate): string {
  return (REASONS as readonly string[]).includes(reason) ? tr(`threads.reason.${reason}` as Key) : reason;
}

/** タスクの待っているもの: 理由・名前・ミューテックスの持ち主・残りのティック（無ければ空） */
export function waitText(w: { reason: string; name?: string | null; holder?: string | null;
                              remaining_ticks?: number | null } | null | undefined, tr: Translate): string {
  if (!w) return "";
  return [reasonText(w.reason, tr), w.name ?? "", w.holder ? tr("threads.heldBy", { task: w.holder }) : "",
          w.remaining_ticks !== undefined && w.remaining_ticks !== null ? tr("threads.ticksLeft", { n: w.remaining_ticks }) : ""]
    .filter(Boolean).join(" ");
}

/** オブジェクトを待っているタスク: 名前・優先度・理由 */
export function waiterText(w: { task: string; priority?: number | null; reason: string }, tr: Translate): string {
  return [w.task, w.priority !== null && w.priority !== undefined ? `(${w.priority})` : "", reasonText(w.reason, tr)]
    .filter(Boolean).join(" ");
}

/** 割合の表し方（1 桁の %） */
export const percent = (x: number | null | undefined): string =>
  x === null || x === undefined || !Number.isFinite(x) ? "—" : `${(x * 100).toFixed(1)}%`;
