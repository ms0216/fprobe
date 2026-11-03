// RTOS の時間の解析（docs/superpowers/specs/2026-11-02-ui-default-panels-design.md §4.2）: タスクの時間軸
// （rtos.timeline.start で取った・取っている）から、ジョブごとの応答時間・実行時間の分布と最悪の回（rtos.timeline.jobs）、
// キュー・セマフォの使用量と満杯・空・待ち（rtos.timeline.queues）、タスクごとのスタックの最大とあふれの印・ヒープの
// 最小の空き（rtos.timeline.memory）。分布・最悪・割合・あふれは API が数えたまま、並びも API の順。Svelte に依らない
import { readQuiet, type DaemonConnection, type Methods, type Params, type Result } from "./api";
import type { Reveal } from "./timeline/state";

type Conn = Pick<DaemonConnection, "call">;

export type TimingTab = "jobs" | "queues" | "stacks";
export const TAB_METHOD = {
  jobs: "rtos.timeline.jobs", queues: "rtos.timeline.queues", stacks: "rtos.timeline.memory",
} as const satisfies Record<TimingTab, keyof Methods>;
type TabMethod = (typeof TAB_METHOD)[TimingTab];

/** 分布（API の Distribution.as_dict。値はタイムベースの刻み）の欄の並び（見せ方。値は API のまま） */
export const DIST_KEYS = ["count", "min", "median", "mean", "p90", "p99", "max", "stdev"] as const;

export interface Analysis {
  result: Result<TabMethod>;
  /** 時間軸の始め（タイムベース）と刻みの秒（1 / timebase_hz）。時刻の 0 と秒への書き換えに使う */
  t0: number;
  tickS: number;
  /** 開いている記録から作った時間軸か（rtos.timeline.get の window.source）。時刻の 0 を記録の始めにする */
  inRecording: boolean;
}

/** タブの解析を読む（quiet）。時間軸が無ければ API の誤りの文 */
export async function readAnalysis(conn: Conn, tab: TimingTab, params: Params<TabMethod> = {}):
  Promise<{ analysis: Analysis | null; error: string | null }> {
  const r = await readQuiet<TabMethod>(conn, TAB_METHOD[tab], params);
  if (r.error || r.result === null) return { analysis: null, error: r.error };
  const g = await readQuiet(conn, "rtos.timeline.get", { limit: 0 });
  const hz = r.result.timebase_hz;
  return { analysis: { result: r.result, t0: r.result.t0 ?? 0, tickS: hz ? 1 / hz : 0,
                       inRecording: g.result?.window?.source === "recording" }, error: null };
}

/** 名前ごとの表（API の object の順のまま）を行に */
export function rowsOf<T extends Record<string, any>>(table: Record<string, T> | null | undefined): (T & { name: string })[] {
  return Object.entries(table ?? {}).map(([name, row]) => ({ ...row, name }));
}

/** 区間の時間軸への飛ぶ先（start・end か release・end。タイムベース） */
export function spanReveal(w: { start?: number; release?: number; end?: number } | null | undefined): Reveal | null {
  const t = w?.start ?? w?.release;
  if (typeof t !== "number") return null;
  return { source: "recording", t, ...(typeof w?.end === "number" ? { tEnd: w.end } : {}) };
}

/** 1 点の時刻への飛ぶ先 */
export function pointReveal(t: number | null | undefined): Reveal | null {
  return typeof t === "number" ? { source: "recording", t } : null;
}
