// Event Log（Tracealyzer / SystemView の Events・TRACE32 のトレースリスト）の計算: 構造化された検索の条件 →
// recording.find の query、結果の要約・CSV・時間軸の印、区間の統計（timing.measure）の分布図・長い順の上位、調査の印とメモ。
// Svelte・DOM に依らない。仕様: docs/reports/2026-10-18-gap-analysis.md G07
import type { Bar } from "./chart";
import { parseCount } from "./system";
import { formatDuration } from "./time";
import type { Mark } from "./timeline/state";
import { display } from "./vartree";

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

/** 区間の統計（timing.measure）の引数。棒の数 bins・長い順の上位の数 longest は使う人が入れた正の整数だけ（無ければ
 *  null = 送らない。画面の数・API の既定を暗黙に使わない） */
export function measureParams(start: Condition, end: Condition, binsText: string, longestText: string):
  { start: Record<string, unknown>; end: Record<string, unknown>; bins: number; longest: number } | null {
  const bins = parseCount(binsText), longest = parseCount(longestText);
  return bins === null || longest === null ? null : { start: toQuery(start), end: toQuery(end), bins, longest };
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

/** 本文の要約（出来事の stream・kind ごとに読みやすい欄。決めていない stream・kind は本文の JSON）。
 *  表し方は stream・kind で選ぶ（本文のキーの有無で推さない） */
export function summary(e: RecEvent): string {
  const text = summaryOf(e, (e.body ?? {}) as Record<string, any>);
  if (text !== null) return text;
  return e.body === null || e.body === undefined ? "" : JSON.stringify(e.body);
}

const withError = (text: unknown, error: unknown) => (error ? `${text} (${error})` : String(text ?? ""));

function summaryOf(e: RecEvent, b: Record<string, any>): string | null {
  switch (e.stream) {
    case "annotation":                                   // 調査の印（mark）・解読の注釈（<protocol>.<種類>）
      return e.kind === "mark" || e.kind.includes(".") ? withError(b.text, b.error) : null;
    case "log":                                          // ログの形式の解読（fprobe.logfmt）
      return withError(b.text, b.error);
    case "semihosting":
    case "rtt":
      return e.kind === "line" ? String(b.text ?? "") : null;
    case "host":
      return e.kind === "call" ? `${b.client ? `${b.client}: ` : ""}${b.method}${b.error ? ` — ${b.error}` : ""}` : null;
    case "target":
      return e.kind === "state" ? [b.state, b.halt_reason].filter(Boolean).join(" ") : null;
    case "rtos":
      switch (e.kind) {
        case "switch": return `${b.from ?? "?"} → ${b.to ?? "?"}`;
        case "blocked": return `${b.task ?? "?"} ← ${b.on ?? "?"}`;
        case "isr": return String(b.name ?? "");
        case "gap": return String(b.reason ?? "");
        default: return typeof b.task === "string" ? b.task : null;    // run・カーネルの出来事（タスクの名前）
      }
    case "user":
      return e.kind === "value" ? `${b.name} = ${b.value}` : String(b.name ?? "");
    case "swo":
      if (e.kind === "line") return String(b.text ?? "");
      return e.kind === "data_access" ? `${b.name} = ${display(b.value)}` : null;   // 値は fprobe.dwarf.values の形
    case "testpoint":
      return e.kind === "anomaly" ? String(b.message ?? "") : String(b.name ?? `#${e.a}`);
    case "trace":                                        // 取り込んだトレース（fprobe.tracefmt）
      return String(b.text ?? b.name ?? "");
    case "uart":
      return e.b >= 0x20 && e.b < 0x7f ? `'${String.fromCharCode(e.b)}'` : hex(e.b);
    case "dap":
    case "sample":
      return `${hex(e.a)} = ${hex(e.b)}`;
    case "aux":
      return `${e.b} mV`;
    default:
      return null;
  }
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
/** 長い順の上位（timing.measure の longest。bin = 入る棒の番号） */
export interface Longest { t: number; t_end: number; duration: number; bin: number }

/** 分布図の棒（x は秒。選んだ棒・長い順の上位のある棒（デーモンが示した bin）を目立たせる） */
export function histogramBars(hist: Histogram[], timebaseHz: number, picked: number | null, longest: Longest[]): Bar[] {
  const outlying = new Set(longest.map((o) => o.bin));
  return hist.map((h, i) => ({ x0: h.lo / timebaseHz, x1: (h.hi > h.lo ? h.hi : h.lo + 1) / timebaseHz, y: h.count,
                               mark: i === picked || outlying.has(i) }));
}

/** 棒 i に入る区間（[開始の時刻, 時間]）。棒は timing.measure の interval_bins（intervals と同じ並び）で選ぶ */
export function intervalsInBin(intervals: [number, number][], bins: number[], i: number): [number, number][] {
  return intervals.filter((_, k) => bins[k] === i);
}

/** 長い順の上位 → 時間軸の印（区間。普通の色: 異常とは言わない） */
export function longestMarks(longest: Longest[], timebaseHz: number): Mark[] {
  return longest.map((o) => ({ t: o.t, tEnd: o.t_end, label: formatDuration(o.duration / timebaseHz), tone: 0 as const }));
}


// --- 調査の印とメモ（annotation.mark） ---------------------------------------------------------------

export interface Note {
  t: number;
  text: string;
  by: string | null;                  // 置いた利用者（分からなければ null）
  role: string | null;                // ai / human / script / hook
}

/** 印（annotation.mark）に置いた者を添える: 印の本文の事実だけ（フックの印は body.hook、recording.mark の印は
 *  デーモンが残した body.client）。無ければ null（fprobe が置いた印・古い記録。操作の記録から推さない） */
export function notesOf(marks: RecEvent[]): Note[] {
  return marks.map((m) => {
    const text = String(m.body?.text ?? "");
    if (m.body?.hook !== undefined) return { t: m.t, text, by: `hook ${m.body.hook}`, role: "hook" };
    const client = m.body?.client;
    return { t: m.t, text, by: client?.name ?? null, role: client?.role ?? null };
  });
}

// --- 記録の概要（stream・kind ごとの件数。Tracealyzer の Event Log の種類・TRACE32 の統計の入口） ---------------

export interface KindStat {
  stream: string;
  kind: string;
  count: number;
  first: number;                      // 最初・最後の出来事の時刻（タイムベース）
  last: number;
  share: number;                      // 全体の件数に対する割合（0〜1）
  perSecond: number | null;           // 1 秒あたりの件数（範囲の長さが分からなければ null）
}

/** recording.kinds の応答 → 表（数え方・割合・1 秒あたり・並びはデーモンが決める）。max は棒の長さの目盛り */
export function kindRows(result: any): { rows: KindStat[]; total: number; max: number } {
  const rows: KindStat[] = (Array.isArray(result?.kinds) ? result.kinds : []).map((k: any) => ({
    stream: String(k.stream), kind: String(k.kind), count: Number(k.count) || 0, first: Number(k.first),
    last: Number(k.last), share: Number(k.share) || 0, perSecond: typeof k.per_second === "number" ? k.per_second : null,
  }));
  return { rows, total: Number(result?.total) || 0, max: rows.reduce((m, r) => Math.max(m, r.count), 0) };
}
