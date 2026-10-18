// Event Log（Tracealyzer / SystemView の Events・TRACE32 のトレースリスト）の計算: 構造化された検索の条件 →
// recording.find の query、結果の要約・CSV・時間軸の印、区間の統計（timing.measure）の分布図・外れ値、調査の印とメモ。
// Svelte・DOM に依らない。仕様: docs/reports/2026-10-18-gap-analysis.md G07
import type { Bar } from "./chart";
import { formatDuration } from "./time";
import type { Mark } from "./timeline/state";

/** 記録の出来事（recording.find / recording.events の 1 件。fprobe.recording.Event） */
export interface RecEvent {
  t: number;
  stream: string;
  kind: string;
  a: number;
  b: number;
  body: any;
  t_end: number | null;
  uncertainty: number | null;
}

export const STREAMS = ["rtos", "uart", "swo", "rtt", "dap", "sample", "aux", "user", "host", "target", "annotation",
                        "testpoint", "rule", "clock"] as const;
export const COMPARES = ["eq", "ne", "lt", "le", "gt", "ge"] as const;
export type Compare = (typeof COMPARES)[number];
export const COMPARE_TEXT: Record<Compare, string> = { eq: "=", ne: "≠", lt: "<", le: "≤", gt: ">", ge: "≥" };

/** 検索の条件（画面の欄。空の欄は条件にしない） */
export interface Condition {
  stream: string;
  kind: string;                       // 「,」で区切ると複数
  aOp: Compare;
  a: string;                          // 10 進か 0x
  bOp: Compare;
  b: string;
  field: string;                      // body の欄の名前
  regex: boolean;                     // true: 欄の値を正規表現で、false: 値が等しい
  value: string;
}

export const emptyCondition = (): Condition => ({ stream: "", kind: "", aOp: "eq", a: "", bOp: "eq", b: "", field: "",
                                                  regex: true, value: "" });

/** 数（10 進・0x・負）。読めなければ投げる（画面は欄の誤りとして出す） */
export function parseNumber(text: string): number {
  const s = text.trim().replace(/_/g, "");
  const v = /^-?0x[0-9a-f]+$/i.test(s) ? (s.startsWith("-") ? -parseInt(s.slice(1), 16) : parseInt(s, 16)) : Number(s);
  if (s === "" || !Number.isFinite(v)) throw new Error(`not a number: ${text}`);
  return v;
}

/** 欄の値（正規表現でないとき）: JSON の数・真偽・null なら値、ほかは文字 */
function bodyValue(text: string): unknown {
  const s = text.trim();
  if (/^(-?\d+(\.\d+)?|true|false|null)$/.test(s)) return JSON.parse(s);
  if (/^-?0x[0-9a-f]+$/i.test(s)) return parseNumber(s);
  return text;
}

/** 条件 → recording.find の query。range は時刻の範囲 [t0, t1)（無ければ全体） */
export function toQuery(c: Condition, range: [number, number] | null = null): Record<string, unknown> {
  const q: Record<string, unknown> = {};
  if (c.stream) q.stream = c.stream;
  const kinds = c.kind.split(",").map((k) => k.trim()).filter(Boolean);
  if (kinds.length) q.kind = kinds.length === 1 ? kinds[0] : kinds;
  if (c.a.trim()) q.a = c.aOp === "eq" ? parseNumber(c.a) : { [c.aOp]: parseNumber(c.a) };
  if (c.b.trim()) q.b = c.bOp === "eq" ? parseNumber(c.b) : { [c.bOp]: parseNumber(c.b) };
  if (c.field.trim()) q.body = { [c.field.trim()]: c.regex ? { regex: c.value } : bodyValue(c.value) };
  if (range) {
    q.t0 = range[0];
    q.t1 = range[1];
  }
  return q;
}

/** 条件の短い表し方（統計の開始・終了の見出し・印のラベル） */
export function describeCondition(c: Condition): string {
  const parts = [c.stream || "*", c.kind ? `.${c.kind}` : ""];
  if (c.a.trim()) parts.push(` a${COMPARE_TEXT[c.aOp]}${c.a.trim()}`);
  if (c.b.trim()) parts.push(` b${COMPARE_TEXT[c.bOp]}${c.b.trim()}`);
  if (c.field.trim()) parts.push(` ${c.field.trim()}${c.regex ? "~" : "="}${c.value}`);
  return parts.join("");
}

const hex = (v: number) => `0x${(v >>> 0).toString(16).toUpperCase()}`;

/** 本文の要約（出来事の種類ごとに読みやすい欄、無ければ JSON） */
export function summary(e: RecEvent): string {
  const b = e.body;
  if (b && typeof b === "object") {
    if (typeof b.text === "string") return b.error ? `${b.text} (${b.error})` : b.text;
    if (typeof b.method === "string") {
      const who = b.client ? `${b.client}: ` : "";
      return `${who}${b.method}${b.error ? ` — ${b.error}` : ""}`;
    }
    if (typeof b.task === "string") return b.on ? `${b.task} ← ${b.on}` : b.task;
    if (typeof b.to === "string") return `${b.from ?? "?"} → ${b.to}`;
    if (typeof b.name === "string") return b.value !== undefined ? `${b.name} = ${b.value}` : b.name;
    if (typeof b.state === "string") return b.reason ? `${b.state} (${b.reason})` : b.state;
    if (typeof b.object === "string") return b.object;
    return JSON.stringify(b);
  }
  if (e.stream === "uart") return e.b >= 0x20 && e.b < 0x7f ? `'${String.fromCharCode(e.b)}'` : hex(e.b);
  if (e.stream === "dap" || e.stream === "sample") return `${hex(e.a)} = ${hex(e.b)}`;
  if (e.stream === "aux") return `${e.b} mV`;
  return "";
}

/** a・b が番地・値・ハンドル（16 進で読む）の stream。ほか（aux の mV 等の量・回数）は 10 進 */
const HEX_FIELDS = new Set(["dap", "sample", "uart", "swo", "rtt", "rtos", "target"]);

/** 表の a・b の列（内容の列の要約と同じ表記） */
export function fieldText(e: RecEvent, field: "a" | "b"): string {
  const v = e[field];
  return HEX_FIELDS.has(e.stream) ? hex(v) : String(v);
}

/** 結果の CSV（時刻は秒と元の単位の両方） */
export function eventsCsv(events: RecEvent[], origin: number, tickS: number): string {
  const quote = (s: string) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  const lines = ["seconds,t,t_end,stream,kind,a,b,summary,body"];
  for (const e of events) {
    lines.push([((e.t - origin) * tickS).toPrecision(12), e.t, e.t_end ?? "", e.stream, e.kind, e.a, e.b,
                quote(summary(e)), quote(e.body === null || e.body === undefined ? "" : JSON.stringify(e.body))].join(","));
  }
  return `${lines.join("\n")}\n`;
}

/** 検索の結果 → 時間軸の印（区間なら区間。ラベルは要約の頭） */
export function searchMarks(events: RecEvent[]): Mark[] {
  return events.map((e) => ({ t: e.t, ...(e.t_end !== null && e.t_end > e.t ? { tEnd: e.t_end } : {}),
                              label: summary(e).slice(0, 24) || `${e.stream}.${e.kind}` }));
}

// --- 区間の統計（timing.measure） --------------------------------------------------------------------

export interface Histogram { lo: number; hi: number; count: number }
export interface Outlier { t: number; t_end: number; duration: number; score: number }

/** 時間 d が入る棒の番号（無ければ -1）。棒の分け方は fprobe.profile.stats.Distribution.histogram と同じ
 *  （最小〜最大を等しい幅に、最後の棒は最大を含む。全部同じ値なら棒は 1 つ） */
export function binOf(d: number, hist: Histogram[]): number {
  if (!hist.length) return -1;
  const lo = hist[0].lo, hi = hist[hist.length - 1].hi;
  if (d < lo || d > hi) return -1;
  if (hi === lo) return 0;
  return Math.min(Math.floor((d - lo) / ((hi - lo) / hist.length)), hist.length - 1);
}

/** 分布図の棒（x は秒。選んだ棒・外れ値のある棒を目立たせる） */
export function histogramBars(hist: Histogram[], timebaseHz: number, picked: number | null, outliers: Outlier[]): Bar[] {
  const outlying = new Set(outliers.map((o) => binOf(o.duration, hist)));
  return hist.map((h, i) => ({ x0: h.lo / timebaseHz, x1: (h.hi > h.lo ? h.hi : h.lo + 1) / timebaseHz, y: h.count,
                               mark: i === picked || outlying.has(i) }));
}

/** 棒 i に入る区間（[開始の時刻, 時間]） */
export function intervalsInBin(intervals: [number, number][], hist: Histogram[], i: number): [number, number][] {
  return intervals.filter(([, d]) => binOf(d, hist) === i);
}

/** 外れ値 → 時間軸の印（区間、tone warn） */
export function outlierMarks(outliers: Outlier[], timebaseHz: number): Mark[] {
  return outliers.map((o) => ({ t: o.t, tEnd: o.t_end, label: formatDuration(o.duration / timebaseHz), tone: 3 as const }));
}


// --- 調査の印とメモ（annotation.mark） ---------------------------------------------------------------

export interface Note {
  t: number;
  text: string;
  by: string | null;                  // 置いた利用者（分からなければ null）
  role: string | null;                // ai / human / script / hook
}

/** 印（annotation.mark）に置いた者を添える: フックの印は body.hook、ほかは同じ本文の recording.mark の操作
 *  （記録の host.call。params の text・t が合うもの）から。分からなければ null（記録の外で置いた印・古い記録） */
export function notesOf(marks: RecEvent[], calls: RecEvent[]): Note[] {
  const placing = calls.filter((c) => c.body?.method === "recording.mark");
  const used = new Set<RecEvent>();
  return marks.map((m) => {
    const text = String(m.body?.text ?? "");
    if (m.body?.hook !== undefined) return { t: m.t, text, by: `hook ${m.body.hook}`, role: "hook" };
    const candidates = placing.filter((c) => !used.has(c) && c.body?.params?.text === text
      && (c.body?.params?.t === undefined || c.body?.params?.t === null || c.body.params.t === m.t));
    // 同じ本文が何度もあれば、印の時刻にいちばん近い操作
    const call = candidates.sort((x, y) => Math.abs(x.t - m.t) - Math.abs(y.t - m.t))[0];
    if (!call) return { t: m.t, text, by: null, role: null };
    used.add(call);
    return { t: m.t, text, by: String(call.body.client ?? ""), role: call.body.role ?? null };
  });
}

// --- 記録の概要（stream・kind ごとの件数。Tracealyzer の Event Log の種類・TRACE32 の統計の入口） ---------------

/** recording.query の引数: stream・kind ごとの件数・最初・最後。range は時刻の範囲 [t0, t1)（SQL の条件と、長期の
 *  記録の分割を選ぶ t0 / t1 の両方に。無ければ全体） */
export function kindQuery(range: [number, number] | null): { sql: string; params: number[]; t0?: number; t1?: number } {
  const where = range ? " WHERE t >= ? AND t < ?" : "";
  const sql = `SELECT stream, kind, COUNT(*) AS n, MIN(t) AS first, MAX(t) AS last FROM events${where} GROUP BY stream, kind`;
  return range ? { sql, params: [range[0], range[1]], t0: range[0], t1: range[1] } : { sql, params: [] };
}

export interface KindStat {
  stream: string;
  kind: string;
  count: number;
  first: number;                      // 最初・最後の出来事の時刻（タイムベース）
  last: number;
  share: number;                      // 全体の件数に対する割合（0〜1）
  perSecond: number;                  // 1 秒あたりの件数（範囲の長さで。分からなければ NaN）
}

/** recording.query の結果 → 件数の多い順の表。timebaseHz・spanTicks は範囲の長さ（1 秒あたりの件数） */
export function kindStats(result: { rows?: unknown[][] }, timebaseHz: number, spanTicks: number):
  { rows: KindStat[]; total: number; max: number } {
  const raw = (result.rows ?? []).map((r) => ({ stream: String(r[0]), kind: String(r[1]), count: Number(r[2]) || 0,
                                                first: Number(r[3]), last: Number(r[4]) }));
  const total = raw.reduce((n, r) => n + r.count, 0);
  const seconds = timebaseHz > 0 && spanTicks > 0 ? spanTicks / timebaseHz : NaN;
  const rows = raw.map((r) => ({ ...r, share: total ? r.count / total : 0, perSecond: r.count / seconds }))
    .sort((x, y) => y.count - x.count || x.stream.localeCompare(y.stream) || x.kind.localeCompare(y.kind));
  return { rows, total, max: rows.length ? rows[0].count : 0 };
}
