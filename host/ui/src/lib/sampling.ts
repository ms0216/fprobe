// Data Sampling（Ozone の Data Sampling・J-Scope・Live Watch / Live Expressions）の計算。Svelte に依らない。
// 式の一覧と周期（デーモンの画面の状態 ui.state の sampling で別の窓・別の PC・AI と分け合う。古いデーモンでは
// ブラウザに覚える）、watch.series の値の積み上げと統計（最小・最大・平均・件数）、値の数への変換、
// XY（2 つの式の散布）、配列の波形（debug.evaluate の配列）、取得の状態（周期・飛ばした周期・誤り）、条件での取得の引数。
// 時刻はすべてタイムベースの µs（watch.series の time_us。記録の 1 単位の秒で時間軸の時刻と行き来できる）。
import type { DaemonConnection } from "./api";
import type { Series } from "./chart";
import { display, kindOf, scalarOf } from "./vartree";

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
  async load(local: SampledExpr[], rate: number | null): Promise<void> {
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
  async save(list: SampledExpr[], rate: number | null): Promise<void> {
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

/** 観測の値（fprobe.dwarf.values の形）を数に（int・float・enum の value、bool は 1 / 0）。数でなければ NaN
 *  （float の NaN・無限大の "nan" 等、ほかの種類、kind の無い古い形） */
export function numberOf(v: unknown): number {
  const n = scalarOf(v);
  if (typeof n === "boolean") return n ? 1 : 0;
  return typeof n === "number" ? n : NaN;
}

/** 値の短い表し方（列挙は名前、真偽、数は桁を抑える。ほかの種類は変数の木と同じ短い文字、kind の無いものは JSON の短縮） */
export function displayValue(v: unknown, digits?: number): string {
  if (v === null || v === undefined) return "—";
  const o = v as Record<string, any>;
  switch (kindOf(v)) {
    case "int":
    case "float":
      if (typeof o.value !== "number") return String(o.value);           // "nan" "inf" "-inf"
      if (digits !== undefined) return o.value.toFixed(digits);
      return Number.isInteger(o.value) ? String(o.value) : String(parseFloat(o.value.toPrecision(6)));
    case "bool":
      return o.value ? "true" : "false";
    case "enum":
      return o.name ?? String(o.value);
    case "unavailable":
      return `<${o.reason}>`;
    case "array":
      if (typeof o.string === "string") return JSON.stringify(o.string);
      return display(v, 60);
    case "none": {
      const text = JSON.stringify(v);
      return text.length > 60 ? `${text.slice(0, 57)}…` : text;
    }
    default:
      return display(v, 60);
  }
}

export interface Sample {
  time_us: number;
  value: unknown;
}

interface Track {
  x: number[];
  y: number[];
  discarded: number;                // 上限を超えて捨てた点の数（グラフから）
  gaps: [number, number][];         // 読まなかった区間（前回の最後の時刻, API が返した読んだ範囲の始め）。µs
  last: Sample | null;              // 最新の値（数でなくても）
}

/** 式ごとの値の列（watch.series を何度読んでも重ならないように、前回の最後の時刻より後だけを足す） */
export class SampleStore {
  private tracks = new Map<string, Track>();

  constructor(readonly limit = STORE_LIMIT) {}

  private track(name: string): Track {
    let t = this.tracks.get(name);
    if (!t) {
      t = { x: [], y: [], discarded: 0, gaps: [], last: null };
      this.tracks.set(name, t);
    }
    return t;
  }

  /** 新しい値を足す（古い順）。足した数を返す。fromUs = API（watch.series の from_us）が返した読んだ範囲の始め
   *  （それより古い値を読まなかった）: 前回の最後がそれより前なら、その間を読まなかった区間（gaps）に数える */
  merge(name: string, samples: Sample[], fromUs: number | null = null): number {
    const t = this.track(name);
    if (fromUs !== null && t.last && t.last.time_us < fromUs) t.gaps.push([t.last.time_us, fromUs]);
    let added = 0;
    for (const s of samples) {
      if (typeof s.time_us !== "number" || (t.last && !(s.time_us > t.last.time_us))) continue;
      const y = numberOf(s.value);
      t.x.push(s.time_us);
      t.y.push(y);
      t.last = s;
      added++;
    }
    if (t.x.length > this.limit) {
      const cut = t.x.length - this.limit;
      t.x.splice(0, cut);
      t.y.splice(0, cut);
      t.discarded += cut;
    }
    return added;
  }

  /** 最新の値だけを足す（watch.values。値の列を読まない式の表） */
  note(name: string, sample: Sample | null): void {
    if (sample) this.merge(name, [sample]);
  }

  /** 画面の読みが追いつかず読まなかった区間（µs。グラフに無い） */
  gaps(name: string): [number, number][] {
    return this.tracks.get(name)?.gaps ?? [];
  }

  /** 上限（limit）を超えてグラフの点から捨てた数（統計は全部の分） */
  discarded(name: string): number {
    return this.tracks.get(name)?.discarded ?? 0;
  }

  latest(name: string): Sample | null {
    return this.tracks.get(name)?.last ?? null;
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

/** XY: watch.xy の組（組み方・時刻の差はデーモンが決めて返す）を描く点に。数でない値の組は描けないので除く */
export function xyPoints(result: any): { x: number[]; y: number[] } {
  const xs: number[] = [], ys: number[] = [];
  for (const p of Array.isArray(result?.points) ? result.points : []) {
    const x = numberOf(p.x), y = numberOf(p.y);
    if (Number.isFinite(x) && Number.isFinite(y)) {
      xs.push(x);
      ys.push(y);
    }
  }
  return { x: xs, y: ys };
}

/** 配列の値（debug.evaluate）を添字 → 数の列に。数の配列・文字の配列・構造体の配列は member の欄。length は配列の
 *  要素数（max_elements で切ったときは values より多い）。配列でない・数にならなければ null */
export function arrayWave(value: unknown, member = ""): { values: number[]; length: number } | null {
  if (kindOf(value) !== "array") return null;
  const a = value as { length: number; items: unknown[] };
  const values = a.items.map((v) => numberOf(!member ? v : kindOf(v) === "struct" ? (v as any).members[member] : undefined));
  return values.some(Number.isFinite) ? { values, length: a.length } : null;
}

export interface Health {
  rate: number;                       // 設定した周期（Hz）
  sampled: string[];                  // FPGA のサンプラで読む式
  host: string[];                     // ホストがたどって読む式（ポインタ）
  rounds: number | null;
  overruns: number | null;            // 飛ばした周期（取りこぼし）
  errors: number | null;              // 読みの誤り（ACK が OK でない）
  lossRatio: number | null;           // 飛ばした周期の割合（0〜1。watch.status の loss_ratio）
  chaseErrors: Record<string, string>;
  rejected: Record<string, string>;   // watch.set が外した誤った式 → 誤り
  sinceStartS: number | null;         // 始めてからの秒（巡回・取りこぼしの数はここから）
  measuredHz: number | null;          // 実際の周期（巡回の数 ÷ 秒。watch.status の measured_hz）
  running: boolean;
}

/** watch.status から取得の状態（割合・周期はデーモンが数から出したもの） */
export function health(status: any): Health {
  const watches: any[] = Array.isArray(status?.watches) ? status.watches : [];
  const num = (v: unknown) => (typeof v === "number" ? v : null);
  const rounds = num(status?.rounds), overruns = num(status?.overruns);
  return {
    rate: Number(status?.rate_hz) || 0,
    sampled: watches.filter((w) => w.method !== "host").map((w) => w.name),
    host: watches.filter((w) => w.method === "host").map((w) => w.name),
    rounds, overruns, errors: num(status?.errors),
    lossRatio: num(status?.loss_ratio),
    chaseErrors: status?.chase_errors ?? {},
    rejected: Object.fromEntries((Array.isArray(status?.rejected) ? status.rejected : [])
      .map((r: any) => [String(r.expression), String(r.error)])),
    sinceStartS: num(status?.since_start_s),
    measuredHz: num(status?.measured_hz),
    running: watches.length > 0,
  };
}

export const OPS = [">", ">=", "<", "<=", "==", "!="] as const;

export interface TriggerForm {
  variable: string;                   // 空 = いまから
  op: (typeof OPS)[number];
  value: string;
  edge: boolean;
  preMs: string;                      // 使う人が入れる（空は誤り。画面の既定を持たない）
  postMs: string;                     // 0 = 止まりの条件か件数の上限まで
}

/** 条件での取得の引数（watch.trigger）。誤りは文字列 */
export function triggerParams(f: TriggerForm): { start: Record<string, unknown> | null; pre_s: number; post_s: number | null }
  | string {
  const value = Number(f.value);
  if (f.variable && (f.value.trim() === "" || !Number.isFinite(value))) return "value";
  const ms = (s: string) => (s.trim() === "" ? NaN : Number(s));
  const pre = ms(f.preMs), post = ms(f.postMs);
  if (!(pre >= 0) || !(post >= 0)) return "time";
  return {
    start: f.variable ? { variable: f.variable, op: f.op, value, edge: f.edge } : null,
    pre_s: pre / 1000,
    post_s: post > 0 ? post / 1000 : null,
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


