// Interrupts のビュー（SystemView・IAR Interrupt Log・MCUXpresso SWO Interrupt Trace の型）の、API の応答を描く形への写し。
// Svelte・DOM に依らない。材料は 2 つ: 記録の割り込みの区間（recording.interrupts。時刻がタイムベース = 時間軸へ飛べる）
// と、Code Profile の SWO 例外トレース（profile.exceptions。時刻はコアのクロック数なので時間軸へは飛べない）。どちらも
// デーモンが同じ形で数える（回数・時間・自分の時間・負荷・分布）。画面では数えない。
import type { Bar } from "./chart";
import type { DaemonConnection } from "./api";

type Conn = Pick<DaemonConnection, "call">;
const quiet = { quiet: true };

/** 割り込みごとの統計。時間は秒（unit の 1 秒あたりの数 hz で割った値）。hz が分からなければ unit の数のまま
 *  （タイムベース・コアのクロックの数。秒として出さない） */
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

/** 1 回の割り込み（記録の時刻。inclusive はタイムベースの数） */
export interface Activation {
  t: number;
  tEnd: number;
  inclusive: number;
}

/** profile.exceptions・recording.interrupts の時間の単位（Hz。分からなければ null） */
export function profileHz(r: any): number | null {
  const hz = r?.clock_hz ?? r?.timebase_hz;
  return Number(hz) > 0 ? Number(hz) : null;
}

/** profile.exceptions・recording.interrupts の exceptions → 統計（秒。単位が分からなければ数のまま）。並びは API のまま */
export function statsFromProfile(r: any): IsrStat[] {
  const hz = profileHz(r) ?? 1;
  return (r?.exceptions ?? []).map((e: any): IsrStat => ({
    number: e.exception, name: e.name, count: e.count, total: e.total_inclusive / hz, self: e.total_self / hz,
    min: e.inclusive.min / hz, max: e.inclusive.max / hz, mean: e.inclusive.mean / hz, load: e.load ?? null,
    maxAt: e.max_at ?? null, maxEnd: e.max_end ?? null,
  }));
}

/** 分布図（recording.interrupts の distribution）: 棒・長い順の上位・棒ごとのいちばん長い回（ダブルクリックの飛ぶ先） */
export interface Histogram {
  bars: Bar[];
  longest: Activation[];
  binLongest: (Activation | null)[];
}

const act = (a: any): Activation => ({ t: a.t, tEnd: a.t_end, inclusive: a.inclusive });

/** distribution → 分布図。長い順の上位の入った棒に印（外れ値の経験則は使わない。API の bin のまま） */
export function histogramOf(d: any): Histogram | null {
  if (!d) return null;
  const marked = new Set<number>((d.longest ?? []).map((a: any) => a.bin));
  const boxes: any[] = d.histogram ?? [];
  return { bars: boxes.map((b, i): Bar => ({ x0: b.lo, x1: b.hi, y: b.count, mark: marked.has(i) })),
           longest: (d.longest ?? []).map(act), binLongest: boxes.map((b) => (b.longest ? act(b.longest) : null)) };
}

/** 棒の中でいちばん長い回（時間軸へ飛ぶ先。API の箱ごとの longest） */
export function longestIn(h: Histogram, bar: number | null): Activation | null {
  return bar === null ? null : h.binLongest[bar] ?? null;
}

/** 記録の割り込み（recording.interrupts）。number・bins・longest があればその割り込みの分布も */
export async function recordedIsrs(conn: Conn, dist: { number: number; bins: number; longest: number } | null):
  Promise<{ stats: IsrStat[]; hz: number | null; histogram: Histogram | null }> {
  const r: any = await conn.call("recording.interrupts", dist ?? {}, quiet);
  return { stats: statsFromProfile(r), hz: profileHz(r), histogram: histogramOf(r.distribution) };
}

/** 記録の時刻 t に実行していた割り込み（recording.interrupts の at_numbers の最後 = いちばん内側。無ければ null） */
export async function isrAt(conn: Conn, t: number): Promise<number | null> {
  const r: any = await conn.call("recording.interrupts", { at: t }, quiet);
  const at: number[] = r?.at_numbers ?? [];
  return at.length ? at[at.length - 1] : null;
}
