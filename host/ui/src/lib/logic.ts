// Logic Analyzer（Saleae Logic 2 の Analyzers・DSView のトリガ・PulseView の測定）の計算: 解読器の設定の欄 → la.decode
// の settings、解読の注釈 → 時間軸の印、多段トリガ・番地一致・つなぎ先の欄 ↔ xtrig.* の引数、カーソル間の測定の範囲と
// 表。Svelte・DOM に依らない。仕様: docs/reports/2026-10-18-gap-analysis.md G09
import type { Mark, Source } from "./timeline/state";
import { parseNumber } from "./eventlog";

// --- 解読（la.decoders / la.decode） ------------------------------------------------------------------

/** 設定の型（la.decoders の params の type。解読器の引数の型の申告。null = 申告が無い・1 つの型に決まらない） */
export type ParamType = "channel" | "channels" | "integer" | "number" | "boolean" | "string" | "object";
/** 解読器の設定（la.decoders の params）。required = 既定を持たない（通信の設定は必須）、nullable = null を渡せる
 *  （つながない線等）、type = 型の申告 */
export interface DecoderParam { name: string; required: boolean; nullable?: boolean; default?: unknown; type?: ParamType | null }
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

/** チャンネルを選ぶ欄か: 解読器が型で申告したもの（type = channel）だけ。設定の名前では推さない */
export const isChannelParam = (p: DecoderParam) => p.type === "channel";

/** 解読器の欄の初期値: すべて空（チャンネルも割り当てない。既定の値を画面に持たない。必須の欄は使う人が埋める） */
export function newForm(protocol: string, decoders: DecoderInfo[]): DecoderForm {
  const info = decoders.find((d) => d.name === protocol);
  const values: Record<string, string> = {};
  for (const p of info?.params ?? []) values[p.name] = "";
  const lower = info?.lower.length ? newForm(info.lower[0], decoders) : null;
  return { protocol, values, lower };
}

/** 空の必須の欄（足りない設定）の名前。下の解読の欄は「下の名前.設定」。空でなければ la.decode を呼べない */
export function missingSettings(form: DecoderForm, decoders: DecoderInfo[], prefix = ""): string[] {
  const info = decoders.find((d) => d.name === form.protocol);
  const own = (info?.params ?? []).filter((p) => p.required && (form.values[p.name] ?? "").trim() === "")
    .map((p) => prefix + p.name);
  const lower = form.lower ? missingSettings(form.lower, decoders, `${prefix}${form.lower.protocol}.`) : [];
  return [...own, ...lower];
}

const integer = (s: string): number => {
  const v = parseNumber(s);
  if (!Number.isInteger(v)) throw new RangeError(`not an integer: ${s}`);
  return v;
};

/** 欄の文字 → 設定の値。解読器が申告した型で読む（文字の形から型を推さない。"1,2" は文字の設定なら文字のまま）。
 *  読めなければ投げる。"null" は null を渡せる設定だけ。型の申告が無い設定は、JSON として読めれば JSON、読めなければ
 *  文字（決まった規則） */
export function settingValue(text: string, p: DecoderParam): unknown {
  const s = text.trim();
  if (s === "null") {
    if (p.nullable || !p.type) return null;
    throw new RangeError(`${p.name} cannot be null`);
  }
  switch (p.type) {
    case "channel":
    case "integer":
      return integer(s);
    case "number":
      return parseNumber(s);
    case "boolean":
      if (s === "true" || s === "false") return s === "true";
      throw new RangeError(`not a boolean: ${s}`);
    case "channels": {
      const items = /^\[.*\]$/.test(s) ? JSON.parse(s) : s.split(",");
      if (!Array.isArray(items) || !items.length) throw new RangeError(`not a list: ${s}`);
      return items.map((x: unknown) => integer(String(x)));
    }
    case "string":
      return s;
    case "object": {
      const v = JSON.parse(s);
      if (v === null || typeof v !== "object" || Array.isArray(v)) throw new RangeError(`not an object: ${s}`);
      return v;
    }
    default:
      try {
        return JSON.parse(s);
      } catch {
        return s;
      }
  }
}

const paramsOf = (form: DecoderForm, decoders: DecoderInfo[]) =>
  decoders.find((d) => d.name === form.protocol)?.params ?? [];

/** 申告した型で読めない欄の名前（下の解読の欄は「下の名前.設定」）。空でなければ la.decode を呼べない */
export function invalidSettings(form: DecoderForm, decoders: DecoderInfo[], prefix = ""): string[] {
  const own = paramsOf(form, decoders).filter((p) => {
    const v = form.values[p.name] ?? "";
    if (v.trim() === "") return false;
    try {
      settingValue(v, p);
      return false;
    } catch {
      return true;
    }
  }).map((p) => prefix + p.name);
  const lower = form.lower ? invalidSettings(form.lower, decoders, `${prefix}${form.lower.protocol}.`) : [];
  return [...own, ...lower];
}

/** 欄 → la.decode の settings（空の欄は送らない: 省ける設定は解読器の既定に、必須の設定は missingSettings で止める。
 *  下の解読は下の名前のキーに）。読めない欄は投げる（invalidSettings で先に止める） */
export function settingsOf(form: DecoderForm, decoders: DecoderInfo[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of paramsOf(form, decoders)) {
    const v = form.values[p.name] ?? "";
    if (v.trim() !== "") out[p.name] = settingValue(v, p);
  }
  if (form.lower) out[form.lower.protocol] = settingsOf(form.lower, decoders);
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

/** 段の欄。回数は使う人が入れる文字（空・0 を黙って 1 にしない） */
export interface StageForm { when: TriggerEvent[]; count: string; withinUs: string }
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
    const count = s.count.trim() === "" ? NaN : Number(s.count);
    if (!Number.isInteger(count) || count < 1) throw new Error(`stage ${i + 1}: count must be an integer of 1 or more`);
    const within = s.withinUs.trim();
    return { when: [...s.when], count, ...(i > 0 && within ? { within_us: Number(within) } : {}) };
  });
}

export function stageForms(stages: { when: string[]; count?: number; within_us?: number | null }[]): StageForm[] {
  return stages.map((s) => ({ when: s.when.filter((w): w is TriggerEvent => (EVENTS as readonly string[]).includes(w)),
                              count: s.count === undefined ? "" : String(s.count),
                              withinUs: s.within_us === undefined || s.within_us === null ? "" : String(s.within_us) }));
}

/** 段の図の箱の文字: 事象（A / B / Match / Host を「|」で）、回数、制限時間 */
export function stageLabel(s: StageForm, i: number): { events: string; detail: string } {
  const short: Record<TriggerEvent, string> = { pattern_a: "A", pattern_b: "B", dap_match: "Match", host: "Host" };
  const detail = [Number(s.count) > 1 ? `×${s.count.trim()}` : "", i > 0 && s.withinUs.trim() ? `≤${s.withinUs.trim()} µs` : ""]
    .filter(Boolean).join(" ");
  return { events: s.when.map((w) => short[w]).join(" | ") || "—", detail };
}

export type StageState = "done" | "current" | "waiting" | "idle";

/** xtrig.sequence_status の結果（レジスタの事実。段ごとの状態もデーモンが返す） */
export interface SequenceStatus {
  stage: number;
  fired: boolean;
  time: number;
  armed: boolean;
  stages: number;
  stage_states: readonly StageState[];
  /** 段とピンの条件のレジスタの読み戻し（xtrig.set_sequence の引数と同じ形。within_us は丸めた値、制限なしは null） */
  config?: { stages: { when: string[]; count: number; within_us: number | null }[]; pattern_a: Pattern; pattern_b: Pattern };
}

/** 段の状態（図の色）: xtrig.sequence_status の stage_states をそのまま。読めていない・申告の無い段は idle */
export function stageState(i: number, status: Pick<SequenceStatus, "stage_states"> | null): StageState {
  return status?.stage_states?.[i] ?? "idle";
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

/** 測定の結果のチャンネル: la.measure / recording.waveform_measure が返したものをそのまま（channels を省くと
 *  デーモンが変化のあるチャンネルだけを返す。画面で選ばない） */
export function measuredChannels(channels: Record<string, ChannelMeasure> | undefined): [number, ChannelMeasure][] {
  return Object.entries(channels ?? {}).map(([k, v]) => [Number(k), v] as [number, ChannelMeasure]);
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
