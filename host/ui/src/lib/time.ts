// 時刻・時間の表し方（全ビュー・時間軸・TimeLink の共通）。Svelte・DOM に依らない。
//
// - 時刻は「記録（または捕捉）の始めからの秒」。記録は記録の範囲の始め（recording.status の span[0]）、捕捉は捕捉の
//   始め（サンプル 0）を 0 とする（捕捉のトリガは時間軸の印。0 にはしない）。
// - 単位（ns / µs / ms / s）は値の大きさで選ぶ。時刻の桁は分解能（1 単位の秒、時間軸では 1 画素の時間）が見えるだけ
//   にし、表では有効数字を抑える。時間の長さ（区間・差・周期）は有効数字 4 桁。
import type { Source } from "./timeline/state";

/** 源の時刻の 0 の位置（源の単位）と 1 単位の秒 */
export interface Origin {
  t0: number;
  tickS: number;
}

/** 源ごとの 0 の位置（分からなければ null） */
export type Origins = Record<Source, Origin | null>;

export const NO_ORIGINS: Origins = { capture: null, recording: null };

/** 時刻の指し方: 源の単位（t。捕捉はサンプル番号、記録はタイムベース）か、タイムベースの µs（us。watch.series 等） */
export interface TimeRef {
  source: Source;
  t?: number;
  us?: number;
}

/** 表の時刻の有効数字の上限（分解能がもっと細かくても、これ以上は出さない） */
export const TABLE_DIGITS = 6;

function unitOf(a: number): [number, string] {
  return a < 1e-6 ? [1e9, "ns"] : a < 1e-3 ? [1e6, "µs"] : a < 1 ? [1e3, "ms"] : [1, "s"];
}

/** 時間の長さ（区間・差・周期）: 単位は大きさで、有効数字 digits 桁（末尾の 0 は落とす） */
export function formatDuration(seconds: number, digits = 4): string {
  if (!Number.isFinite(seconds)) return "—";
  if (seconds === 0) return "0 s";
  const [scale, unit] = unitOf(Math.abs(seconds));
  return `${parseFloat((seconds * scale).toPrecision(digits))} ${unit}`;
}

/** 時刻（秒）: 単位は値の大きさ（0 なら分解能の大きさ）で、小数の桁は分解能 resolution（秒）が見えるだけ。
 *  有効数字は最大 maxDigits。trim で末尾の 0 を落とす（目盛り）。分解能が無ければ有効数字 maxDigits */
export function formatSeconds(seconds: number, resolution: number, maxDigits = 9, trim = false): string {
  if (!Number.isFinite(seconds)) return "—";
  const a = Math.abs(seconds);
  const [scale, unit] = unitOf(a > 0 ? a : resolution > 0 ? resolution : 1);
  const scaled = seconds * scale;
  const whole = Math.abs(scaled) >= 1 ? Math.floor(Math.log10(Math.abs(scaled)) + 1e-9) + 1 : 1;
  const wanted = resolution > 0 ? Math.max(0, Math.ceil(-Math.log10(resolution * scale) - 1e-9)) : maxDigits;
  const decimals = Math.max(0, Math.min(wanted, maxDigits - whole, 9));
  const text = scaled.toFixed(decimals);
  return `${trim ? String(parseFloat(text)) : text} ${unit}`;
}

const UNIT_SECONDS: Record<string, number> = { ns: 1e-9, us: 1e-6, "µs": 1e-6, "μs": 1e-6, ms: 1e-3, s: 1, "": 1 };

/** 人が打った時刻（「1.5 ms」「12us」「3」= 秒）→ 秒。単位は ns / us / µs / ms / s（大文字小文字は問わない）。
 *  読めなければ null（時間軸の「時刻へ」の欄） */
export function parseSeconds(text: string): number | null {
  const m = /^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)\s*([a-zµμ]*)\s*$/i.exec(text);
  if (!m) return null;
  const scale = UNIT_SECONDS[m[2].toLowerCase()];
  const v = Number(m[1]);
  return scale === undefined || !Number.isFinite(v) ? null : v * scale;
}

/** 目盛りのラベル: 間隔 step（秒）が見える桁で、末尾の 0 を落とす */
export function formatTick(seconds: number, step: number): string {
  return seconds === 0 ? "0" : formatSeconds(seconds, step, 12, true);
}

/** 記録（または捕捉）の始めからの秒。0 の位置が分からなければ null */
export function sinceStart(origins: Origins, r: TimeRef): number | null {
  const o = origins[r.source];
  if (!o || !(o.tickS > 0)) return null;
  if (r.t !== undefined && Number.isFinite(r.t)) return (r.t - o.t0) * o.tickS;
  if (r.us !== undefined && Number.isFinite(r.us)) return r.us * 1e-6 - o.t0 * o.tickS;
  return null;
}

/** 時刻の欄の文字（始めからの秒、表の桁）。0 の位置が分からなければ null */
export function timeText(origins: Origins, r: TimeRef, digits = TABLE_DIGITS): string | null {
  const s = sinceStart(origins, r);
  return s === null ? null : formatSeconds(s, origins[r.source]!.tickS, digits);
}

/** 源の時刻 t → 始めからの秒の関数（グラフの横軸）。0 の位置が分からなければ fallback を 0 とする */
export function secondsOf(origins: Origins, source: Source, fallback?: Origin): (t: number) => number {
  const o = origins[source] ?? fallback ?? null;
  return o && o.tickS > 0 ? (t) => (t - o.t0) * o.tickS : (t) => t;
}

/** データ（始め data.t0、源の単位）の時刻の 0: 開いている記録のデータ（inRecording で、記録の始めより後）なら記録の
 *  始め、別のデータ（記録の外で取った・前の記録）ならデータの始め。記録と別のデータを記録の 0 で測ると負になる */
export function originFor(origins: Origins, source: Source, data: Origin, inRecording: boolean): Origin {
  const o = origins[source];
  return inRecording && o && o.tickS > 0 && data.t0 >= o.t0 ? o : data;
}

/** グラフの横軸の時刻（Chart の xFormat）: 横軸の値 x（scale 秒の単位）→ 始めからの秒を、目盛りの間隔の桁で */
export function axisTime(toSeconds: (x: number) => number, scale: number): (x: number, step: number) => string {
  return (x, step) => formatTick(toSeconds(x), step * scale);
}

/** タイムベースの 0（記録が無いときの µs の時刻の 0。watch.* の time_us） */
export const TIMEBASE_ZERO: Origin = { t0: 0, tickS: 1e-6 };

/** タイムベースの µs → 始めからの秒（記録が無ければタイムベースの 0 から） */
export function usSeconds(origins: Origins): (us: number) => number {
  return (us) => sinceStart(origins, { source: "recording", us }) ?? us * 1e-6;
}
