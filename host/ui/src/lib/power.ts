// Power（J-Link の Power・Ozone の Power Graph に当たる）の計算。Svelte に依らない。
// いまのターゲットの電圧（probe.voltage を周期的に読む）の窓と統計（最小・最大・平均）、記録の電圧（stream aux、
// kind mv）の列、電圧の低下（基準より下がった区間）の検出。
import type { Series } from "./chart";

export interface VoltageReading {
  volts: number | null;              // 測れない（over_range・未計測）なら null
  over_range: boolean;
  live: boolean;                     // 補助マイコンが計測値を送っている
  samples?: number;
  errors?: number;
}

export interface WindowStats {
  count: number;                     // 値のある読みの数
  min: number;
  max: number;
  mean: number;
  overRange: number;                 // 測れる上限を超えた読み
  notLive: number;                   // 補助マイコンが送っていなかった読み
}

export const SPANS_S = [10, 60, 300, 1800];                 // 窓の長さ（秒）

/** 読みの窓（時刻は ms。古い読みは窓の長さで捨てる） */
export class VoltageWindow {
  private t: number[] = [];
  private v: (number | null)[] = [];
  private flags: { over: boolean; live: boolean }[] = [];

  constructor(public spanS = 60) {}

  add(tMs: number, r: VoltageReading): void {
    this.t.push(tMs);
    this.v.push(r.live && !r.over_range && typeof r.volts === "number" ? r.volts : null);
    this.flags.push({ over: Boolean(r.over_range), live: Boolean(r.live) });
    this.trim(tMs);
  }

  setSpan(spanS: number): void {
    this.spanS = spanS;
    if (this.t.length) this.trim(this.t[this.t.length - 1]);
  }

  private trim(now: number): void {
    let cut = 0;
    while (cut < this.t.length && this.t[cut] < now - this.spanS * 1000) cut++;
    if (cut) {
      this.t.splice(0, cut);
      this.v.splice(0, cut);
      this.flags.splice(0, cut);
    }
  }

  clear(): void {
    this.t = [];
    this.v = [];
    this.flags = [];
  }

  get length(): number {
    return this.t.length;
  }

  stats(): WindowStats {
    let count = 0, min = Infinity, max = -Infinity, sum = 0, overRange = 0, notLive = 0;
    this.v.forEach((x, i) => {
      if (this.flags[i].over) overRange++;
      if (!this.flags[i].live) notLive++;
      if (x === null) return;
      count++;
      sum += x;
      if (x < min) min = x;
      if (x > max) max = x;
    });
    return count ? { count, min, max, mean: sum / count, overRange, notLive }
      : { count, min: NaN, max: NaN, mean: NaN, overRange, notLive };
  }

  /** グラフの系列（x = 最初の読みからの秒。値の無い読みで線を切る） */
  series(name: string, originMs: number): Series {
    return { name, x: this.t.map((t) => (t - originMs) / 1000), y: this.v.map((v) => (v === null ? NaN : v)), unit: "V" };
  }

  /** CSV（時刻は ISO、電圧 V。測れなかった読みは空） */
  csv(): string {
    const lines = ["time,volts,over_range,live"];
    this.t.forEach((t, i) => lines.push(`${new Date(t).toISOString()},${this.v[i] ?? ""},${this.flags[i].over},${this.flags[i].live}`));
    return `${lines.join("\n")}\n`;
  }
}

/** 記録の電圧の出来事（recording.find の stream aux・kind mv。b = mV）を時刻（タイムベース）と V の列に */
export function recordingVolts(events: { t: number; b: number; kind?: string }[]): { x: number[]; y: number[] } {
  const rows = events.filter((e) => e.kind === undefined || e.kind === "mv").slice().sort((a, b) => a.t - b.t);
  return { x: rows.map((e) => e.t), y: rows.map((e) => e.b / 1000) };
}

/** いちばん低い点の位置（無ければ -1） */
export function minIndex(ys: ArrayLike<number>): number {
  let k = -1;
  for (let i = 0; i < ys.length; i++) if (Number.isFinite(ys[i]) && (k < 0 || ys[i] < ys[k])) k = i;
  return k;
}

export interface Dip {
  t: number;                         // 下がり始め（x の単位）
  tEnd: number;                      // 戻ったところ
  at: number;                        // いちばん低い点の時刻
  min: number;                       // いちばん低い値
  drop: number;                      // 基準（中央値）からの下がり
}

/** 電圧の低下: 中央値より drop 以上下がった区間（連続する点をまとめる）。低い順ではなく時刻の順 */
export function findDips(xs: ArrayLike<number>, ys: ArrayLike<number>, drop: number): Dip[] {
  const finite = Array.from(ys).filter(Number.isFinite).sort((a, b) => a - b);
  if (!finite.length) return [];
  const median = finite[Math.floor(finite.length / 2)];
  const out: Dip[] = [];
  let cur: Dip | null = null;
  for (let i = 0; i < xs.length; i++) {
    const y = ys[i];
    const low = Number.isFinite(y) && y <= median - drop;
    if (low) {
      if (!cur) cur = { t: xs[i], tEnd: xs[i], at: xs[i], min: y, drop: median - y };
      cur.tEnd = xs[i];
      if (y < cur.min) Object.assign(cur, { min: y, at: xs[i], drop: median - y });
    } else if (cur) {
      cur.tEnd = xs[i];
      out.push(cur);
      cur = null;
    }
  }
  if (cur) out.push(cur);
  return out;
}

/** 電圧の表し方（3 桁の V） */
/** いまの電圧（補助マイコンが計測値を送っていて、範囲の中のときだけ。ほかは null = 「データなし」。
 *  補助の Pico が無いと volts = 0・live = false が返る。0 V と見せない） */
export function measuredVolts(r: Pick<VoltageReading, "volts" | "over_range" | "live"> | null | undefined): number | null {
  return r && r.live && !r.over_range && typeof r.volts === "number" ? r.volts : null;
}

export function formatVolts(v: number): string {
  return Number.isFinite(v) ? `${v.toFixed(3)} V` : "—";
}
