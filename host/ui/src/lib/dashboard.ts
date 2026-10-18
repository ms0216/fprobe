// Dashboard（STM32CubeMonitor・µC/Probe の部品を並べた操作パネル）の配置と部品の計算。Svelte に依らない。
// 部品: ゲージ・数値・棒・グラフ（小さな時系列）・ボタン（押すと値を書く）・スライダ（書く）・切り替え（真偽）・ランプ。
// 配置は JSON（ブラウザに覚える・書き出す・読み込む）。読み込んだ配置は validateLayout で検証して直す（直した所は
// problems に）。配置はデーモンの画面の状態（ui.state の dashboard）でも分け合う（SharedDashboard）。
import type { DaemonConnection } from "./api";

export const KINDS = ["gauge", "number", "bar", "chart", "button", "slider", "toggle", "lamp"] as const;
export type WidgetKind = (typeof KINDS)[number];

/** 書く部品（ボタン・スライダ・切り替え） */
export const WRITERS: ReadonlySet<WidgetKind> = new Set(["button", "slider", "toggle"]);

export interface Widget {
  id: string;
  kind: WidgetKind;
  expr: string;                      // 読む・書く式（変数）
  label: string;                     // 空なら式
  min: number;                       // ゲージ・棒・スライダ・グラフの範囲
  max: number;
  unit: string;
  digits: number;                    // 小数の桁（0〜6）
  value: string;                     // ボタンが書く値（数・列挙の名前）
  w: number;                         // 幅（格子の列。1〜4）
  h: number;                         // 高さ（格子の行。1〜3）
}

export interface Layout {
  version: 1;
  widgets: Widget[];
}

export const MAX_W = 4, MAX_H = 3, MAX_DIGITS = 6, MAX_WIDGETS = 64;

const DEFAULT_SIZE: Record<WidgetKind, [number, number]> = {
  gauge: [1, 2], number: [1, 1], bar: [2, 1], chart: [2, 2], button: [1, 1], slider: [2, 1], toggle: [1, 1], lamp: [1, 1],
};

let seq = 0;
const newId = () => `w${Date.now().toString(36)}${(seq++).toString(36)}`;

export function newWidget(kind: WidgetKind, expr = ""): Widget {
  const [w, h] = DEFAULT_SIZE[kind];
  return { id: newId(), kind, expr, label: "", min: 0, max: kind === "toggle" || kind === "lamp" ? 1 : 100, unit: "",
           digits: kind === "gauge" || kind === "number" || kind === "bar" ? 1 : 0, value: kind === "button" ? "1" : "",
           w, h };
}

export const emptyLayout = (): Layout => ({ version: 1, widgets: [] });

/** デモ・初めて開いたときの例（デモの ELF の変数） */
export function exampleLayout(): Layout {
  const g = newWidget("gauge", "sensors[0].value");
  Object.assign(g, { min: 0, max: 4096, label: "accel", digits: 0 });
  const c = newWidget("chart", "sensors[0].value");
  Object.assign(c, { min: 0, max: 4096, label: "accel", digits: 0 });
  const n = newWidget("number", "counter");
  n.digits = 0;
  const s = newWidget("slider", "sensors[0].gain");
  Object.assign(s, { min: 0, max: 200, label: "gain" });
  const t = newWidget("toggle", "config.verbose");
  const l = newWidget("lamp", "sensors[0].enabled");
  const b = newWidget("button", "counter");
  Object.assign(b, { label: "Reset counter", value: "0" });
  const bar = newWidget("bar", "sensors[1].value");
  Object.assign(bar, { label: "saw", unit: "%" });
  return { version: 1, widgets: [g, c, n, bar, s, t, l, b] };
}

/** 直した所の種類（画面の文言は i18n の dashboard.problem.<code>） */
export type ProblemCode = "notLayout" | "invalidJson" | "notObject" | "unknownKind" | "noExpression" | "range" | "clamped"
  | "duplicateId" | "tooMany";

export interface Problem {
  index: number | null;             // 部品の位置（1 から。配置全体なら null）
  code: ProblemCode;
  detail: string;                   // 値（知らない部品の名前・直した範囲など）
}

export interface Checked {
  layout: Layout;
  problems: Problem[];              // 直した所（画面に出す）
}

/** 読み込んだ配置（JSON の値）を検証して直す: 知らない部品は捨て、範囲・桁・大きさを収め、id の重複を振り直す */
export function validateLayout(raw: unknown): Checked {
  const problems: Problem[] = [];
  const note = (index: number | null, code: ProblemCode, detail = "") => problems.push({ index, code, detail });
  const list: unknown[] = Array.isArray(raw) ? raw : raw && typeof raw === "object" && Array.isArray((raw as any).widgets)
    ? (raw as any).widgets : (note(null, "notLayout"), []);
  const seen = new Set<string>();
  const widgets: Widget[] = [];
  list.slice(0, MAX_WIDGETS).forEach((item, i) => {
    const at = i + 1;
    if (!item || typeof item !== "object") {
      note(at, "notObject");
      return;
    }
    const o = item as Record<string, unknown>;
    if (!KINDS.includes(o.kind as WidgetKind)) {
      note(at, "unknownKind", String(o.kind));
      return;
    }
    const kind = o.kind as WidgetKind;
    const base = newWidget(kind);
    const str = (k: keyof Widget, fallback: string) => (typeof o[k] === "string" ? (o[k] as string) : fallback);
    const num = (k: keyof Widget, fallback: number) => (typeof o[k] === "number" && Number.isFinite(o[k]) ? (o[k] as number) : fallback);
    const w: Widget = {
      id: str("id", base.id), kind, expr: str("expr", "").trim(), label: str("label", ""), unit: str("unit", ""),
      min: num("min", base.min), max: num("max", base.max), digits: Math.round(num("digits", base.digits)),
      value: typeof o.value === "number" ? String(o.value) : str("value", base.value),
      w: Math.round(num("w", base.w)), h: Math.round(num("h", base.h)),
    };
    if (!w.expr) note(at, "noExpression");
    if (!(w.max > w.min)) {
      const before = `${w.min}..${w.max}`;
      w.max = w.min + (base.max - base.min || 1);
      note(at, "range", `${before} → ${w.min}..${w.max}`);
    }
    const clamp = (v: number, lo: number, hi: number, name: string) => {
      const c = Math.min(hi, Math.max(lo, v));
      if (c !== v) note(at, "clamped", `${name} ${v} → ${c}`);
      return c;
    };
    w.digits = clamp(w.digits, 0, MAX_DIGITS, "digits");
    w.w = clamp(w.w, 1, MAX_W, "width");
    w.h = clamp(w.h, 1, MAX_H, "height");
    if (!w.id || seen.has(w.id)) {
      if (w.id) note(at, "duplicateId", w.id);
      w.id = newId();
    }
    seen.add(w.id);
    widgets.push(w);
  });
  if (list.length > MAX_WIDGETS) note(null, "tooMany", String(MAX_WIDGETS));
  return { layout: { version: 1, widgets }, problems };
}

/** 文字の JSON から（壊れていれば空の配置と問題） */
export function parseLayout(text: string): Checked {
  try {
    return validateLayout(JSON.parse(text));
  } catch {
    return { layout: emptyLayout(), problems: [{ index: null, code: "invalidJson", detail: "" }] };
  }
}

export const layoutJson = (l: Layout) => `${JSON.stringify(l, null, 2)}\n`;

/** 比べるための JSON（キーの順に依らない） */
function canonical(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (v && typeof v === "object") {
    return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canonical((v as any)[k])}`).join(",")}}`;
  }
  return JSON.stringify(v) ?? "null";
}

type Conn = Pick<DaemonConnection, "call" | "on">;

/** 配置をデーモンの画面の状態（ui.state の dashboard）で分け合う: 別の窓・遠隔（--lan）の画面・AI が同じ操作パネルを
 *  見る。ほかの窓・AI の変更は onChange（validateLayout で検証して直したもの）で届く。自分の変更の通知と、最後に
 *  分け合った配置と同じ配置は送らない（受け取った配置を送り返さない）。ui.state の無いデーモンでは何もしない */
export class SharedDashboard {
  readonly origin = `d${Math.random().toString(36).slice(2, 10)}`;
  private available = false;
  private last: string | null = null;           // 最後に分け合った配置（canonical）
  private off: () => void;

  constructor(private conn: Conn, private onChange: (checked: Checked) => void) {
    this.off = conn.on("ui.state", (n: { origin: string | null; changed?: string[]; state?: any }) => {
      if (n.origin === this.origin || !n.changed?.includes("dashboard")) return;
      const d = n.state?.dashboard;
      if (d) this.take(d);
    });
  }

  /** つながったとき: デーモンに配置があればそれ（onChange）、無ければ手元の配置を上げる */
  async load(local: Layout): Promise<void> {
    let state: any;
    try {
      state = await this.conn.call("ui.state.get", {}, { quiet: true });
    } catch {
      this.available = false;
      return;
    }
    this.available = true;
    if (state?.dashboard) this.take(state.dashboard);
    else await this.save(local);
  }

  /** 手元の変更を上げる（記録に残さない） */
  async save(layout: Layout): Promise<void> {
    const text = canonical(layout);
    if (!this.available || text === this.last) return;
    this.last = text;
    try {
      await this.conn.call("ui.state.set", { dashboard: layout, origin: this.origin } as any, { quiet: true });
    } catch {
      this.last = null;                          // 切れた等: つながり直したら load で合わせる
    }
  }

  dispose(): void {
    this.off();
  }

  private take(raw: unknown): void {
    this.last = canonical(raw);
    this.onChange(validateLayout(raw));
  }
}

// --- 編集 ---------------------------------------------------------------------------------------

export function addWidget(l: Layout, kind: WidgetKind, expr = ""): Layout {
  return l.widgets.length >= MAX_WIDGETS ? l : { ...l, widgets: [...l.widgets, newWidget(kind, expr)] };
}

export function removeWidget(l: Layout, id: string): Layout {
  return { ...l, widgets: l.widgets.filter((w) => w.id !== id) };
}

/** 並べ替え（dir = -1 で前へ、+1 で後ろへ） */
export function moveWidget(l: Layout, id: string, dir: -1 | 1): Layout {
  const i = l.widgets.findIndex((w) => w.id === id), j = i + dir;
  if (i < 0 || j < 0 || j >= l.widgets.length) return l;
  const widgets = l.widgets.slice();
  [widgets[i], widgets[j]] = [widgets[j], widgets[i]];
  return { ...l, widgets };
}

/** 部品の設定を変える（大きさ・桁・範囲は収める） */
export function updateWidget(l: Layout, id: string, patch: Partial<Omit<Widget, "id">>): Layout {
  return { ...l, widgets: l.widgets.map((w) => {
    if (w.id !== id) return w;
    const next = { ...w, ...patch };
    next.w = Math.min(MAX_W, Math.max(1, Math.round(next.w)));
    next.h = Math.min(MAX_H, Math.max(1, Math.round(next.h)));
    next.digits = Math.min(MAX_DIGITS, Math.max(0, Math.round(next.digits)));
    return next;
  }) };
}

/** 配置の式（重複なし、並びの順） */
export function expressionsOf(l: Layout): string[] {
  return [...new Set(l.widgets.map((w) => w.expr).filter(Boolean))];
}

// --- 表示と書き込み ------------------------------------------------------------------------------

/** 範囲の中の位置（0〜1 に収める。数でなければ NaN） */
export function fraction(v: number, w: Pick<Widget, "min" | "max">): number {
  if (!Number.isFinite(v)) return NaN;
  return Math.min(1, Math.max(0, (v - w.min) / (w.max - w.min)));
}

/** 数の表示（桁・単位） */
export function formatNumber(v: number, w: Pick<Widget, "digits" | "unit">): string {
  if (!Number.isFinite(v)) return "—";
  return `${v.toFixed(w.digits)}${w.unit ? ` ${w.unit}` : ""}`;
}

/** ランプ・切り替えの真偽（0 以外が真。数でなければ null） */
export function truthOf(v: number): boolean | null {
  return Number.isFinite(v) ? v !== 0 : null;
}

/** 書く値（ボタンの値は数か列挙の名前・true / false。スライダは数。切り替えは反対の真偽を 1 / 0 で） */
export function writeValue(w: Widget, current: number, input?: number): number | string | boolean | null {
  if (w.kind === "slider") return input !== undefined && Number.isFinite(input) ? input : null;
  if (w.kind === "toggle") return truthOf(current) ? 0 : 1;
  if (w.kind === "button") {
    const text = w.value.trim();
    if (!text) return null;
    if (text === "true" || text === "false") return text === "true";
    const n = Number(text);
    return Number.isFinite(n) ? n : text;
  }
  return null;
}

/** スライダの刻み（範囲の 1/100 を桁で丸める。整数の桁なら 1 以上） */
export function sliderStep(w: Pick<Widget, "min" | "max" | "digits">): number {
  const step = (w.max - w.min) / 100;
  const unit = 10 ** -w.digits;
  return Math.max(unit, Math.round(step / unit) * unit);
}

/** ゲージの弧（中心 (50,50)・半径 40、下の開いた 240°）の SVG の path。f = 0〜1 */
export function gaugeArc(f: number): string {
  const start = (210 * Math.PI) / 180;
  const end = start - Math.max(0, Math.min(1, Number.isFinite(f) ? f : 0)) * ((240 * Math.PI) / 180);
  const p = (a: number) => `${(50 + 40 * Math.cos(a)).toFixed(2)} ${(50 - 40 * Math.sin(a)).toFixed(2)}`;
  const large = start - end > Math.PI ? 1 : 0;
  return `M ${p(start)} A 40 40 0 ${large} 1 ${p(end)}`;
}

// --- 読み・書きの API -------------------------------------------------------------------------------

type Call = (method: any, params?: any, opts?: { quiet?: boolean }) => Promise<any>;

export interface Reading {
  value: unknown;                    // 値（読めなければ undefined）
  time_us: number | null;            // 走行中の読みの時刻（タイムベースの µs）。止まっているときは null
  error: string | null;              // 読めない理由（式の誤り）
  sampled: boolean;                  // 走行中: watch で取っている（false = watch の組に入っていない）
}

/** 部品の式の値を読む。走行中は watch.values（watch の組にある式だけ。無い式は sampled = false）、止まっているときは
 *  debug.evaluate（式ごと。誤りは error に） */
export async function readValues(call: Call, exprs: string[], running: boolean): Promise<Map<string, Reading>> {
  const out = new Map<string, Reading>();
  if (running) {
    let values: any[] = [];
    try {
      values = (await call("watch.values", {}, { quiet: true }))?.values ?? [];
    } catch {
      values = [];                   // 観測していない
    }
    const byName = new Map(values.map((v) => [String(v.name), v]));
    for (const e of exprs) {
      const v = byName.get(e);
      out.set(e, v ? { value: v.value ?? undefined, time_us: v.time_us ?? null, error: null, sampled: true }
                   : { value: undefined, time_us: null, error: null, sampled: false });
    }
    return out;
  }
  await Promise.all(exprs.map(async (e) => {
    try {
      const r = await call("debug.evaluate", { expression: e }, { quiet: true });
      out.set(e, { value: r?.value, time_us: null, error: null, sampled: false });
    } catch (err) {
      out.set(e, { value: undefined, time_us: null, error: err instanceof Error ? err.message : String(err), sampled: false });
    }
  }));
  return out;
}

/** 書く操作: 走行中は watch.write（止めない）、止まっているときは debug.write_expression（真偽は 1 / 0） */
export function writeCall(running: boolean, expression: string, value: number | string | boolean):
  { method: "watch.write" | "debug.write_expression"; params: { expression: string; value: number | string | boolean } } {
  if (running) return { method: "watch.write", params: { expression, value } };
  return { method: "debug.write_expression", params: { expression, value: typeof value === "boolean" ? Number(value) : value } };
}
