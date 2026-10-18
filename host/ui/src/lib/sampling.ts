// Data Sampling（Ozone の Data Sampling・J-Scope・Live Watch / Live Expressions）の計算。Svelte に依らない。
// 式の一覧と周期（デーモンの画面の状態 ui.state の sampling で別の窓・別の PC・AI と分け合う。古いデーモンでは
// ブラウザに覚える）、watch.series の値の積み上げと統計（最小・最大・平均・件数）、値の数への変換、
// XY（2 つの式の散布）、配列の波形（debug.evaluate の配列）、取得の状態（周期・飛ばした周期・誤り）、条件での取得の引数。
// 時刻はすべてタイムベースの µs（watch.series の time_us。記録の 1 単位の秒で時間軸の時刻と行き来できる）。
import type { DaemonConnection } from "./api";
import type { Series } from "./chart";

export interface SampledExpr {
  expr: string;
  plot: boolean;                     // グラフに描く
  axis: "left" | "right";            // 右の目盛り（単位の違う量）
}

export const RATES = [10, 100, 1000, 10_000, 100_000];   // 周期の選択（Hz）

/** 周期の欄の選択肢（いまの周期が一覧に無ければ足す。AI・ほかの窓が別の周期で始めたとき、欄が実際と食い違わない） */
export function rateOptions(current: number): number[] {
  return current > 0 && !RATES.includes(current) ? [...RATES, current].sort((a, b) => a - b) : RATES;
}

type Conn = Pick<DaemonConnection, "call" | "on">;

/** 式の一覧と周期をデーモンの画面の状態（ui.state の sampling）で分け合う。ほかの窓・AI の変更は onChange で届く
 *  （自分の変更の通知は読み飛ばす）。ui.state の無いデーモンでは何もしない（手元の一覧のまま） */
export class SharedSampling {
  readonly origin = `s${Math.random().toString(36).slice(2, 10)}`;
  private available = false;
  private off: () => void;

  constructor(private conn: Conn, private onChange: (list: SampledExpr[], rate: number | null) => void) {
    this.off = conn.on("ui.state", (n: { origin: string | null; changed?: string[]; state?: any }) => {
      if (n.origin === this.origin || !n.changed?.includes("sampling")) return;
      const s = n.state?.sampling;
      if (s) this.onChange(parseExprList(JSON.stringify(s.expressions ?? [])), s.rate_hz ?? null);
    });
  }

  /** つながったとき: デーモンに一覧があればそれ（onChange）、無ければ手元の一覧を上げる */
  async load(local: SampledExpr[], rate: number): Promise<void> {
    let state: any;
    try {
      state = await this.conn.call("ui.state.get", {}, { quiet: true });
    } catch {
      this.available = false;
      return;
    }
    this.available = true;
    const s = state?.sampling;
    if (s) this.onChange(parseExprList(JSON.stringify(s.expressions ?? [])), s.rate_hz ?? null);
    else await this.save(local, rate);
  }

  /** 手元の変更を上げる（記録に残さない） */
  async save(list: SampledExpr[], rate: number): Promise<void> {
    if (!this.available) return;
    const sampling = { expressions: list.map((e) => ({ expr: e.expr, plot: e.plot, axis: e.axis })), rate_hz: rate };
    try {
      await this.conn.call("ui.state.set", { sampling, origin: this.origin } as any, { quiet: true });
    } catch {
      /* 切れた等: つながり直したら load で合わせる */
    }
  }

  dispose(): void {
    this.off();
  }
}
export const WINDOWS_S = [1, 5, 10, 30, 0];                 // グラフの幅（秒。0 = 全部）
export const STORE_LIMIT = 50_000;                          // 式ごとに画面に残す点（古い方から捨てる）

/** ブラウザに覚えた一覧（壊れていれば空）。式の重複・空は除く */
export function parseExprList(json: string): SampledExpr[] {
  let raw: unknown;
  try {
    raw = JSON.parse(json);
  } catch {
    return [];
  }
  if (!Array.isArray(raw)) return [];
  let out: SampledExpr[] = [];
  for (const item of raw) {
    const expr = typeof item === "string" ? item : typeof item?.expr === "string" ? item.expr : "";
    const next = addExpr(out, expr);
    if (next !== out && typeof item === "object") {
      next[next.length - 1] = { ...next[next.length - 1], plot: item.plot !== false, axis: item.axis === "right" ? "right" : "left" };
    }
    out = next;
  }
  return out;
}

/** 式を足す（前後の空白を除く。空・既にあるものは足さない） */
export function addExpr(list: SampledExpr[], expr: string): SampledExpr[] {
  const e = expr.trim();
  if (!e || list.some((x) => x.expr === e)) return list;
  return [...list, { expr: e, plot: true, axis: "left" }];
}

export function removeExpr(list: SampledExpr[], expr: string): SampledExpr[] {
  return list.filter((x) => x.expr !== expr);
}

/** 観測の値を数に（整数・浮動小数・真偽、列挙・ビットフィールドの {value}）。数でなければ NaN */
export function numberOf(v: unknown): number {
  if (typeof v === "number") return v;
  if (typeof v === "boolean") return v ? 1 : 0;
  if (v && typeof v === "object" && typeof (v as any).value === "number") return (v as any).value;
  return NaN;
}

/** 値の短い表し方（列挙は名前、真偽、数は桁を抑える。構造体・配列は JSON の短縮） */
export function displayValue(v: unknown, digits?: number): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    if (digits !== undefined) return v.toFixed(digits);
    return Number.isInteger(v) ? String(v) : String(parseFloat(v.toPrecision(6)));
  }
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    if (typeof o.enum === "string") return String(o.enum);
    if (typeof o.unavailable === "string") return `<${o.unavailable}>`;
    if (typeof o.string === "string") return JSON.stringify(o.string);
  }
  const text = JSON.stringify(v);
  return text.length > 60 ? `${text.slice(0, 57)}…` : text;
}

/** 走りながら足す統計（最小・最大・平均・件数。数でない値は数えない） */
export class Stats {
  count = 0;
  min = Infinity;
  max = -Infinity;
  private sum = 0;

  add(v: number): void {
    if (!Number.isFinite(v)) return;
    this.count++;
    this.sum += v;
    if (v < this.min) this.min = v;
    if (v > this.max) this.max = v;
  }

  get mean(): number {
    return this.count ? this.sum / this.count : NaN;
  }
}

export interface Sample {
  time_us: number;
  value: unknown;
}

interface Track {
  x: number[];
  y: number[];
  stats: Stats;
  last: Sample | null;              // 最新の値（数でなくても）
}

/** 式ごとの値の列（watch.series を何度読んでも重ならないように、前回の最後の時刻より後だけを足す） */
export class SampleStore {
  private tracks = new Map<string, Track>();

  constructor(private limit = STORE_LIMIT) {}

  private track(name: string): Track {
    let t = this.tracks.get(name);
    if (!t) {
      t = { x: [], y: [], stats: new Stats(), last: null };
      this.tracks.set(name, t);
    }
    return t;
  }

  /** 新しい値を足す（古い順）。足した数を返す */
  merge(name: string, samples: Sample[]): number {
    const t = this.track(name);
    let added = 0;
    for (const s of samples) {
      if (typeof s.time_us !== "number" || (t.last && !(s.time_us > t.last.time_us))) continue;
      const y = numberOf(s.value);
      t.x.push(s.time_us);
      t.y.push(y);
      t.stats.add(y);
      t.last = s;
      added++;
    }
    if (t.x.length > this.limit) {
      const cut = t.x.length - this.limit;
      t.x.splice(0, cut);
      t.y.splice(0, cut);
    }
    return added;
  }

  /** 最新の値だけを足す（watch.values。値の列を読まない式の表） */
  note(name: string, sample: Sample | null): void {
    if (sample) this.merge(name, [sample]);
  }

  latest(name: string): Sample | null {
    return this.tracks.get(name)?.last ?? null;
  }

  stats(name: string): Stats {
    return this.tracks.get(name)?.stats ?? new Stats();
  }

  points(name: string): { x: number[]; y: number[] } {
    const t = this.tracks.get(name);
    return t ? { x: t.x, y: t.y } : { x: [], y: [] };
  }

  /** いちばん新しい時刻（無ければ null） */
  end(): number | null {
    let end: number | null = null;
    for (const t of this.tracks.values()) if (t.x.length && (end === null || t.x[t.x.length - 1] > end)) end = t.x[t.x.length - 1];
    return end;
  }

  forget(name: string): void {
    this.tracks.delete(name);
  }

  clear(): void {
    this.tracks.clear();
  }
}

/** グラフの系列（描く式だけ。値を写して、一時停止の間も変わらないように） */
export function chartSeries(store: SampleStore, list: SampledExpr[]): Series[] {
  return list.filter((e) => e.plot).map((e) => {
    const p = store.points(e.expr);
    return { name: e.expr, x: p.x.slice(), y: p.y.slice(), step: true, axis: e.axis };
  });
}

/** グラフの横の範囲（最新から windowS 秒。0 は全部 = 自動） */
export function windowDomain(end: number | null, windowS: number): [number, number] | null {
  if (end === null || !windowS) return null;
  return [end - windowS * 1e6, end];
}

/** XY: a の各時刻に、その時刻までの b の最新の値を組む（サンプル・アンド・ホールド）。数でない組は除く */
export function pairXY(a: { x: ArrayLike<number>; y: ArrayLike<number> },
                       b: { x: ArrayLike<number>; y: ArrayLike<number> }): { x: number[]; y: number[] } {
  const xs: number[] = [], ys: number[] = [];
  let j = -1;
  for (let i = 0; i < a.x.length; i++) {
    while (j + 1 < b.x.length && b.x[j + 1] <= a.x[i]) j++;
    if (j < 0) continue;
    const x = a.y[i], y = b.y[j];
    if (Number.isFinite(x) && Number.isFinite(y)) {
      xs.push(x);
      ys.push(y);
    }
  }
  return { x: xs, y: ys };
}

/** 配列の値（debug.evaluate）を添字 → 数の列に。数の配列・文字の配列・多い配列の先頭（{length, head}）・
 *  構造体の配列は member の欄。数にならなければ null */
export function arrayWave(value: unknown, member = ""): { values: number[]; length: number } | null {
  let items: unknown[] | null = null;
  let length = 0;
  if (Array.isArray(value)) {
    items = value;
    length = value.length;
  } else if (value && typeof value === "object") {
    const o = value as Record<string, any>;
    const inner = Array.isArray(o.values) ? o.values : o.values && Array.isArray(o.values.head) ? o.values.head : null;
    if (Array.isArray(o.head)) {
      items = o.head;
      length = Number(o.length) || o.head.length;
    } else if (inner) {
      items = inner;
      length = Number(o.values?.length) || inner.length;
    }
  }
  if (!items) return null;
  const values = items.map((v) => numberOf(member && v && typeof v === "object" ? (v as any)[member] : v));
  return values.some(Number.isFinite) ? { values, length } : null;
}

export interface Health {
  rate: number;                       // 設定した周期（Hz）
  sampled: string[];                  // FPGA のサンプラで読む式
  host: string[];                     // ホストがたどって読む式（ポインタ）
  rounds: number | null;
  overruns: number | null;            // 飛ばした周期（取りこぼし）
  errors: number | null;              // 読みの誤り（ACK が OK でない）
  lossRatio: number;                  // 飛ばした周期の割合（0〜1）
  chaseErrors: Record<string, string>;
  rejected: Record<string, string>;   // watch.set が外した誤った式 → 誤り
  sinceStartS: number | null;         // 始めてからの秒（巡回・取りこぼしの数はここから）
  measuredHz: number | null;          // 実際の周期（巡回の数 ÷ 秒）
  running: boolean;
}

/** watch.status から取得の状態（取りこぼしの割合） */
export function health(status: any): Health {
  const watches: any[] = Array.isArray(status?.watches) ? status.watches : [];
  const num = (v: unknown) => (typeof v === "number" ? v : null);
  const rounds = num(status?.rounds), overruns = num(status?.overruns);
  const total = (rounds ?? 0) + (overruns ?? 0);
  return {
    rate: Number(status?.rate_hz) || 0,
    sampled: watches.filter((w) => w.method !== "host").map((w) => w.name),
    host: watches.filter((w) => w.method === "host").map((w) => w.name),
    rounds, overruns, errors: num(status?.errors),
    lossRatio: total > 0 ? (overruns ?? 0) / total : 0,
    chaseErrors: status?.chase_errors ?? {},
    rejected: Object.fromEntries((Array.isArray(status?.rejected) ? status.rejected : [])
      .map((r: any) => [String(r.expression), String(r.error)])),
    sinceStartS: num(status?.since_start_s),
    measuredHz: rounds !== null && num(status?.since_start_s) ? rounds / status.since_start_s : null,
    running: watches.length > 0,
  };
}

export const OPS = [">", ">=", "<", "<=", "==", "!="] as const;

export interface TriggerForm {
  variable: string;                   // 空 = いまから
  op: (typeof OPS)[number];
  value: string;
  edge: boolean;
  preMs: number;
  postMs: number;                     // 0 = 止まりの条件か件数の上限まで
}

/** 条件での取得の引数（watch.trigger）。誤りは文字列 */
export function triggerParams(f: TriggerForm): { start: Record<string, unknown> | null; pre_s: number; post_s: number | null }
  | string {
  const value = Number(f.value);
  if (f.variable && (f.value.trim() === "" || !Number.isFinite(value))) return "value";
  if (!(f.preMs >= 0) || !(f.postMs >= 0)) return "time";
  return {
    start: f.variable ? { variable: f.variable, op: f.op, value, edge: f.edge } : null,
    pre_s: f.preMs / 1000,
    post_s: f.postMs > 0 ? f.postMs / 1000 : null,
  };
}

/** 条件で取った値（watch.capture の samples）をグラフの系列に */
export function captureSeries(samples: Record<string, Sample[]> | undefined, names: string[]): Series[] {
  if (!samples) return [];
  return names.filter((n) => samples[n]?.length).map((n) => ({
    name: n, x: samples[n].map((s) => s.time_us), y: samples[n].map((s) => numberOf(s.value)), step: true,
  }));
}

// --- グラフの 2 つのカーソル（A・B。MPLAB Data Visualizer・Tracealyzer の 2 点の間の測定） -----------------------

/** A・B のカーソル（横軸の値。時系列はタイムベースの µs） */
export interface ChartCursors {
  a: number | null;
  b: number | null;
}

/** グラフのクリック: 普通のクリックで A、Shift + クリックで B（A の無いうちは A） */
export function toggleCursor(c: ChartCursors, x: number, shift: boolean): ChartCursors {
  return shift && c.a !== null ? { a: c.a, b: x } : { a: x, b: c.b };
}

export interface MeasureRow {
  name: string;
  a: number;                          // A の時刻に保たれている値（無ければ NaN）
  b: number;
  delta: number;                      // b − a
  min: number;                        // A〜B の間（両端を含む）の点の最小・最大・平均・件数（値の無い点は数えない）
  max: number;
  mean: number;
  count: number;
}

export interface Measurement {
  dx: number;                         // B − A（横軸の単位）
  rows: MeasureRow[];
}

/** 昇順の x の系列で、x 以下のいちばん後の点の値（サンプル・アンド・ホールド。無ければ NaN） */
function heldAt(s: Series, x: number): number {
  let lo = 0, hi = s.x.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (s.x[mid] <= x) lo = mid + 1;
    else hi = mid;
  }
  return lo > 0 ? s.y[lo - 1] : NaN;
}

/** A・B の 2 つのカーソルの測定: 系列ごとの A・B の値と差、A〜B の間の点の統計。どちらかが無ければ null */
export function measureBetween(series: Series[], a: number | null, b: number | null): Measurement | null {
  if (a === null || b === null) return null;
  const lo = Math.min(a, b), hi = Math.max(a, b);
  return {
    dx: b - a,
    rows: series.map((s) => {
      const stats = new Stats();
      for (let i = 0; i < s.x.length; i++) if (s.x[i] >= lo && s.x[i] <= hi) stats.add(s.y[i]);
      const va = heldAt(s, a), vb = heldAt(s, b);
      return { name: s.name, a: va, b: vb, delta: vb - va, min: stats.count ? stats.min : NaN,
               max: stats.count ? stats.max : NaN, mean: stats.mean, count: stats.count };
    }),
  };
}
