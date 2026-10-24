// RTOS のビュー（Threads・CPU Load）の、API の応答を描く形への写し。Svelte・DOM に依らない。
// 材料は 2 つ: 取っている（取った）タスクの時間軸（rtos.timeline.get / load）と、記録だけがあるときの
// recording.cpu_load（記録の stream "rtos" からデーモンが同じ定義・同じ形で数える）。画面では数えない。
import type { DaemonConnection } from "./api";
import type { Series } from "./chart";
import type { Key, Translate } from "./i18n";

type Conn = Pick<DaemonConnection, "call">;
const quiet = { quiet: true };

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
  tasks: TaskStat[];         // デーモンの返した順（rtos.timeline.get・recording.cpu_load。画面で並べ替えない）
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
  other?: number | null;     // 負荷のうちタスクにも割り込みにも入らない分（デーモンが整数で数えたもの）
  tasks: Record<string, number>;
}

export interface LoadSeries {
  window: number;            // 窓の幅（タイムベース）
  windows: LoadWindow[];
  peak: { t0: number; t1: number; load: number } | null;
  meanLoad: number | null;
  idle: string[];            // 負荷から除いたアイドルのタスク（デーモンがカーネルの情報で決めたもの。名前では決めない）
}

// --- 取った時間軸（デーモンの要約） ---------------------------------------------------------------------

/** rtos.timeline.get の結果 → 表（タスクはデーモンの返した順のまま。画面で使用率の順に並べない） */
export function tableFromTimeline(r: any): TaskTable {
  const tasks: TaskStat[] = Object.entries<any>(r.stats?.tasks ?? {}).map(([task, s]) => ({
    task, handle: s.handle, cpu: s.cpu, runs: s.runs, total: s.total, max: s.max_run, min: s.min_run, mean: s.mean_run,
  }));
  return { tasks, hz: r.timebase_hz, t0: r.t0, t1: r.t1,
           covered: r.stats?.covered ?? r.t1 - r.t0, lowerBound: !!r.lower_bound };
}

/** rtos.timeline.load の結果 → 推移 */
export function loadFromTimeline(r: any): LoadSeries {
  return { window: r.window, windows: r.windows ?? [], peak: r.peak ?? null, meanLoad: r.mean_load ?? null,
           idle: r.idle_tasks ?? [] };
}

// --- 記録（デーモンが数える） ---------------------------------------------------------------------------

/** 記録の表と推移（recording.cpu_load）。窓の幅（µs）が無ければ推移は読まない。windowsCut = 窓を上限で切ったとき
 *  の全体の数と上限（黙って切らない）。記録に run が無ければ null */
export interface RecordedLoad {
  table: TaskTable;
  series: LoadSeries | null;
  windowsCut: { shown: number; total: number; limit: number } | null;
}

export async function recordedLoad(conn: Conn, windowUs: number | null): Promise<RecordedLoad | null> {
  const r: any = await conn.call("recording.cpu_load", windowUs === null ? {} : { window_us: windowUs }, quiet);
  if (!r?.stats) return null;
  const load = r.load ?? null;
  const shown = load?.windows?.length ?? 0;
  return { table: tableFromTimeline(r), series: load ? loadFromTimeline(load) : null,
           windowsCut: load && load.windows_total > shown ? { shown, total: load.windows_total, limit: load.limit } : null };
}

/** 推移の窓の中の時刻 t を含む窓（無ければ null） */
export function windowAt(series: LoadSeries, t: number): LoadWindow | null {
  return series.windows.find((w) => t >= w.t0 && t < w.t1) ?? null;
}

/** CPU 負荷の積み上げ（Chart の type stacked）の系列（%）: 下からタスク（order = 表の並び = デーモンの順。アイドル
 *  以外の全部）・割り込み・その他（デーモンが数えた other）。上端が負荷になる。x は窓の始めを xOf で変えたもの。
 *  情報の無い窓（load = null）は NaN（線を切る）。focus のタスクがあれば、ほかの系列は薄く */
export function loadStack(series: LoadSeries, order: string[], xOf: (t: number) => number,
                          labels: { isr: string; other: string }, focus: string | null = null): Series[] {
  const ws = series.windows;
  const x = ws.map((w) => xOf(w.t0));
  const idle = new Set(series.idle);
  const names = order.filter((n) => !idle.has(n) && ws.some((w) => w.tasks[n]));
  const pct = (w: LoadWindow, v: number) => (w.load === null ? NaN : v * 100);
  const out: Series[] = names.map((name) => ({ name, x, y: ws.map((w) => pct(w, w.tasks[name] ?? 0)), step: true,
                                               unit: "%" }));
  if (ws.some((w) => w.isr)) out.push({ name: labels.isr, x, y: ws.map((w) => pct(w, w.isr ?? 0)), step: true, unit: "%" });
  if (ws.some((w) => w.other)) out.push({ name: labels.other, x, y: ws.map((w) => pct(w, w.other ?? 0)), step: true, unit: "%" });
  return focus ? out.map((s) => ({ ...s, dim: s.name !== focus })) : out;
}

// --- Threads と時間軸 -----------------------------------------------------------------------------

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
/** RTOS Objects のタブ: API の kind ごとの数（kind の文字の順。名前の形で種類をまとめない）。先頭は "all" */
export function objectKinds(objects: { kind: string }[]): { kind: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const o of objects) counts.set(o.kind, (counts.get(o.kind) ?? 0) + 1);
  return [{ kind: "all", count: objects.length },
          ...[...counts].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([kind, count]) => ({ kind, count }))];
}

/** kind が一致するオブジェクト（"all" は全部） */
export function objectsOfKind<T extends { kind: string }>(objects: T[], kind: string): T[] {
  return kind === "all" ? objects : objects.filter((o) => o.kind === kind);
}

export const percent = (x: number | null | undefined): string =>
  x === null || x === undefined || !Number.isFinite(x) ? "—" : `${(x * 100).toFixed(1)}%`;
