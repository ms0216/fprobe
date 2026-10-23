// Logic Analyzer（Saleae Logic 2 の Analyzers・DSView のトリガ・PulseView の測定）の計算: 解読器の設定の欄 → la.decode
// の settings、解読の注釈 → 時間軸の印、多段トリガ・番地一致・つなぎ先の欄 ↔ xtrig.* の引数、カーソル間の測定の範囲と
// 表。Svelte・DOM に依らない。仕様: docs/reports/2026-10-18-gap-analysis.md G09
import type { Mark, Source } from "./timeline/state";
import { parseNumber } from "./eventlog";

// --- 解読（la.decoders / la.decode） ------------------------------------------------------------------

export interface DecoderParam { name: string; required: boolean; default?: unknown }
export interface DecoderInfo {
  name: string;
  lower: string[];                    // 積み重ねる解読器が受け付ける下の解読（空なら波形を読む）
  summary: string;
  params: DecoderParam[];
  default_kinds: string[] | null;
  origin: string;
}

/** 設定の欄の値（文字）。積み重ねる解読器は下の解読の欄を lower に */
export interface DecoderForm {
  protocol: string;
  values: Record<string, string>;
  lower: DecoderForm | null;
}

/** チャンネルを選ぶ設定の名前（組み込みの解読器の線の名前） */
const CHANNEL_PARAMS = new Set(["channel", "scl", "sda", "sck", "mosi", "miso", "cs", "ws", "sd", "tck", "tms", "tdi",
                                "tdo", "trst", "swclk", "swdio", "clock", "enable"]);
export const isChannelParam = (name: string) => CHANNEL_PARAMS.has(name);

/** 解読器の欄の初期値（必須のチャンネルは並びの順に ch0, ch1… を割り当てる。ほかは空 = 既定） */
export function newForm(protocol: string, decoders: DecoderInfo[]): DecoderForm {
  const info = decoders.find((d) => d.name === protocol);
  const values: Record<string, string> = {};
  let ch = 0;
  for (const p of info?.params ?? []) values[p.name] = isChannelParam(p.name) && p.required ? String(ch++) : "";
  const lower = info?.lower.length ? newForm(info.lower[0], decoders) : null;
  return { protocol, values, lower };
}

/** 欄の文字 → 設定の値（数・0x・真偽・null・「,」区切りの数の並び・JSON の並び、ほかは文字） */
export function settingValue(text: string): unknown {
  const s = text.trim();
  if (/^(true|false|null)$/.test(s)) return JSON.parse(s);
  if (/^\[.*\]$/.test(s)) return JSON.parse(s);
  const int = "-?(0x[0-9a-f_]+|[0-9_]+)";
  if (new RegExp(`^${int}(\\s*,\\s*${int})+$`, "i").test(s)) return s.split(",").map((x) => parseNumber(x));
  if (new RegExp(`^${int}$`, "i").test(s)) return parseNumber(s);
  if (/^-?\d*\.\d+(e-?\d+)?$|^-?\d+e-?\d+$/i.test(s)) return Number(s);
  return text;
}

/** 欄 → la.decode の settings（空の欄は既定に任せて送らない。下の解読は下の名前のキーに） */
export function settingsOf(form: DecoderForm): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(form.values)) if (v.trim() !== "") out[k] = settingValue(v);
  if (form.lower) out[form.lower.protocol] = settingsOf(form.lower);
  return out;
}

/** 解読の注釈（la.decode の annotations の 1 件） */
export interface Annotation {
  start: number;
  end: number;
  start_rel_s: number;
  end_rel_s: number;
  kind: string;
  text: string;
  value: number | null;
  error: string | null;
  fields: Record<string, unknown> | null;
}

/** 絞った種類（la.decode の kinds。既定の種類）の外で表に出ない件数と種類。counts は絞る前の種類ごとの件数 */
export function hiddenKinds(r: { kinds?: string[] | null; counts?: Record<string, number> }): { n: number; kinds: string[] } {
  if (!r.kinds) return { n: 0, kinds: [] };
  const out = Object.entries(r.counts ?? {}).filter(([k]) => !r.kinds!.includes(k));
  return { n: out.reduce((sum, [, n]) => sum + n, 0), kinds: out.map(([k]) => k).sort() };
}

/** 注釈 → 時間軸の印（区間・ラベル = 本文・誤りは tone 1） */
export function decodeMarks(annotations: Annotation[]): Mark[] {
  return annotations.map((a) => ({ t: a.start, ...(a.end > a.start ? { tEnd: a.end } : {}), label: a.text,
                                   ...(a.error ? { tone: 1 as const } : {}) }));
}

// --- トリガ（xtrig.*） ----------------------------------------------------------------------------

export const EVENTS = ["pattern_a", "pattern_b", "dap_match", "host"] as const;
export type TriggerEvent = (typeof EVENTS)[number];
// obs_match・sampler_match は条件を fpga_obs.trigger_set・sampler.trigger_set で書く（ここでは経路だけ。表は機器の経路の事実から始める）
export const SOURCES = ["la_trigger", "dap_match", "host", "sequence", "obs_match", "sampler_match"] as const;
export type TriggerSource = (typeof SOURCES)[number];

export interface StageForm { when: TriggerEvent[]; count: number; withinUs: string }
/** ピンの条件: チャンネルごとの値（x = 見ない）と立ち上がり・立ち下がり */
export interface PatternForm { level: ("x" | "0" | "1")[]; rise: boolean[]; fall: boolean[] }
export type Pattern = { mask: number; value: number; rise: number; fall: number };

export function emptyPattern(channels: number): PatternForm {
  return { level: Array(channels).fill("x"), rise: Array(channels).fill(false), fall: Array(channels).fill(false) };
}

export function patternOf(p: PatternForm): Pattern {
  let mask = 0, value = 0, rise = 0, fall = 0;
  p.level.forEach((l, ch) => {
    if (l !== "x") mask |= 1 << ch;
    if (l === "1") value |= 1 << ch;
    if (p.rise[ch]) rise |= 1 << ch;
    if (p.fall[ch]) fall |= 1 << ch;
  });
  return { mask, value, rise, fall };
}

export function patternForm(p: Partial<Pattern> | null | undefined, channels: number): PatternForm {
  const f = emptyPattern(channels);
  for (let ch = 0; ch < channels; ch++) {
    const bit = 1 << ch;
    if ((p?.mask ?? 0) & bit) f.level[ch] = (p?.value ?? 0) & bit ? "1" : "0";
    f.rise[ch] = !!((p?.rise ?? 0) & bit);
    f.fall[ch] = !!((p?.fall ?? 0) & bit);
  }
  return f;
}

/** ピンの条件が何も無い（どの段も使わないなら送らない） */
export const patternEmpty = (p: Pattern) => !p.mask && !p.rise && !p.fall;

/** 段の欄 → xtrig.set_sequence の stages（段 0 の制限時間は意味が無いので送らない） */
export function stagesOf(stages: StageForm[]): { when: TriggerEvent[]; count: number; within_us?: number }[] {
  return stages.map((s, i) => {
    if (!s.when.length) throw new Error(`stage ${i + 1}: no event`);
    const within = s.withinUs.trim();
    return { when: [...s.when], count: Math.max(1, Math.round(s.count)),
             ...(i > 0 && within ? { within_us: Number(within) } : {}) };
  });
}

export function stageForms(stages: { when: string[]; count?: number; within_us?: number | null }[]): StageForm[] {
  return stages.map((s) => ({ when: s.when.filter((w): w is TriggerEvent => (EVENTS as readonly string[]).includes(w)),
                              count: s.count ?? 1, withinUs: s.within_us === undefined || s.within_us === null ? ""
                                : String(s.within_us) }));
}

/** 段の図の箱の文字: 事象（A / B / Match / Host を「|」で）、回数、制限時間 */
export function stageLabel(s: StageForm, i: number): { events: string; detail: string } {
  const short: Record<TriggerEvent, string> = { pattern_a: "A", pattern_b: "B", dap_match: "Match", host: "Host" };
  const detail = [s.count > 1 ? `×${s.count}` : "", i > 0 && s.withinUs.trim() ? `≤${s.withinUs.trim()} µs` : ""]
    .filter(Boolean).join(" ");
  return { events: s.when.map((w) => short[w]).join(" | ") || "—", detail };
}

/** 段の状態（図の色）: 済んだ段・いまの段・まだの段。発火したら全部済み */
export function stageState(i: number, status: { stage: number; fired: boolean } | null, armed: boolean):
  "done" | "current" | "waiting" | "idle" {
  if (!status || !armed) return "idle";
  if (status.fired || i < status.stage) return "done";
  return i === status.stage ? "current" : "waiting";
}

// --- 測定（la.measure / recording.waveform_measure） ------------------------------------------------

/** 測る範囲: 基準と選択のカーソルの間（両方あれば）、無ければ全体（null） */
export function measureRange(reference: number | null, selection: number | null): [number, number] | null {
  if (reference === null || selection === null || reference === selection) return null;
  return [Math.min(reference, selection), Math.max(reference, selection) + 1];
}

/** 範囲 → 測る API の呼び方（源が捕捉なら la.measure、記録なら recording.waveform_measure） */
export function measureCall(source: Source, range: [number, number] | null):
  { method: "la.measure"; params: { start: number; end: number | null } }
  | { method: "recording.waveform_measure"; params: { t0: number | null; t1: number | null } } {
  if (source === "capture") return { method: "la.measure", params: { start: range?.[0] ?? 0, end: range?.[1] ?? null } };
  return { method: "recording.waveform_measure", params: { t0: range?.[0] ?? null, t1: range?.[1] ?? null } };
}

export interface WidthStats { count: number; min_ns?: number; max_ns?: number; mean_ns?: number; stdev_ns?: number;
                              min_at?: number; max_at?: number }
export interface ChannelMeasure {
  rises: number;
  falls: number;
  high_ratio: number;
  frequency_hz?: number | null;
  duty?: number | null;
  duty_min?: number | null;
  duty_max?: number | null;
  period?: WidthStats;
  high?: WidthStats;
  low?: WidthStats;
}

/** いちばん新しい成功した設定の操作（xtrig.set_sequence 等。AI・ほかの画面のものも）。画面の欄に読み込める */
export function lastApplied<E extends { method: string; error?: string }>(activity: E[], method: string): E | null {
  for (let i = activity.length - 1; i >= 0; i--) {
    const e = activity[i];
    if (e.method === method && e.error === undefined) return e;
  }
  return null;
}

/** 周波数の表示（Hz・kHz・MHz、有効数字 4 桁） */
export function formatHz(hz: number | null | undefined): string {
  if (hz === null || hz === undefined || !Number.isFinite(hz)) return "—";
  const [scale, unit] = hz >= 1e6 ? [1e-6, "MHz"] : hz >= 1e3 ? [1e-3, "kHz"] : [1, "Hz"];
  return `${parseFloat((hz * scale).toPrecision(4))} ${unit}`;
}

/** 測定の結果のチャンネル（変化のあるものだけ、番号の順） */
export function measuredChannels(channels: Record<string, ChannelMeasure> | undefined): [number, ChannelMeasure][] {
  return Object.entries(channels ?? {}).map(([k, v]) => [Number(k), v] as [number, ChannelMeasure])
    .filter(([, v]) => v.rises + v.falls > 0).sort((a, b) => a[0] - b[0]);
}

/** 捕捉の保存（la.export）の形式: sr = PulseView（sigrok）、vcd */
export type LaExportFormat = "sr" | "vcd";

/** la.export の引数。path が null なら日時と形式の拡張子の既定の名前（2 回目に前のファイルを上書きしない。パスはデーモンの
 *  PC。相対ならデーモンの作業場所） */
export function laExportParams(format: LaExportFormat, path: string | null, at: Date = new Date()):
  { format: LaExportFormat; path: string } {
  const p = (n: number) => String(n).padStart(2, "0");
  const stamp = `${at.getFullYear()}${p(at.getMonth() + 1)}${p(at.getDate())}-${p(at.getHours())}${p(at.getMinutes())}${p(at.getSeconds())}`;
  return { format, path: path ?? `capture-${stamp}.${format}` };
}
