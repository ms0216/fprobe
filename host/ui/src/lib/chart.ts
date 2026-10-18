// ビューのグラフ（Data Sampling・CPU Load・Power・分布図・Dashboard）の共通の計算。Svelte・DOM に依らない。
// 描画は components/charts/Chart.svelte（Canvas 2D）。色は時間軸の段と同じ 12 色（layout.ts の CATEGORICAL）。
// グラフの型（ChartType）: line = 時系列の線（階段・右の目盛り）、stacked = 積み上げ（CPU Load のタスク別の負荷）、
// scatter = 散布図（Data Sampling の XY）。棒（分布図）は bars で渡す。
import { CATEGORICAL } from "./timeline/layout";

export interface Series {
  name: string;
  x: ArrayLike<number>;              // 昇順
  y: ArrayLike<number>;              // NaN = 値なし（線を切る）
  color?: string;                    // 省略で並びの順に CATEGORICAL
  step?: boolean;                    // 次の点まで値を保つ（階段。サンプルした変数・状態）
  axis?: "left" | "right";           // 右の目盛りで描く（単位の違う 2 つ目の量）
  unit?: string;
  dim?: boolean;                     // 薄く描く（ほかの系列を選んで強調しているとき）
}

export type ChartType = "line" | "stacked" | "scatter";

export interface Bar {
  x0: number;
  x1: number;
  y: number;
  color?: string;
  mark?: boolean;                    // 目立たせる（外れ値・選んだもの）
}

export const seriesColor = (s: Series, i: number): string => s.color ?? CATEGORICAL[i % CATEGORICAL.length];

/** 範囲 [lo, hi]（値が無ければ null）。NaN・無限は除く */
export function extentOf(values: Iterable<ArrayLike<number>>): [number, number] | null {
  let lo = Infinity, hi = -Infinity;
  for (const arr of values) {
    for (let i = 0; i < arr.length; i++) {
      const v = arr[i];
      if (!Number.isFinite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  return lo <= hi ? [lo, hi] : null;
}

/** 目盛りが切りよく収まる範囲（値が 1 つなら前後に幅を足す。includeZero で 0 を含める） */
export function niceDomain(range: [number, number] | null, includeZero = false): [number, number] {
  if (!range) return [0, 1];
  let [lo, hi] = range;
  if (includeZero) {
    lo = Math.min(lo, 0);
    hi = Math.max(hi, 0);
  }
  if (lo === hi) {
    const pad = lo === 0 ? 1 : Math.abs(lo) * 0.1;
    return [lo - pad, hi + pad];
  }
  const step = niceStep((hi - lo) / 5);
  return [Math.floor(lo / step) * step, Math.ceil(hi / step) * step];
}

/** 1・2・5 の切りのよい間隔（raw 以上の最小） */
export function niceStep(raw: number): number {
  if (!(raw > 0)) return 1;
  const mag = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw * (1 - 1e-9))!;
}

/** [lo, hi] をおよそ count 個に分ける切りのよい目盛り */
export function niceTicks(lo: number, hi: number, count: number): number[] {
  if (!(hi > lo) || count < 1) return [];
  const step = niceStep((hi - lo) / count);
  const out: number[] = [];
  for (let k = Math.ceil(lo / step - 1e-9); k * step <= hi + step * 1e-9; k++) out.push(+(k * step).toPrecision(12));
  return out;
}

/** 昇順の xs で x にいちばん近い位置（空なら -1） */
export function nearestIndex(xs: ArrayLike<number>, x: number): number {
  const n = xs.length;
  if (!n) return -1;
  let lo = 0, hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (xs[mid] < x) lo = mid + 1;
    else hi = mid;
  }
  return lo > 0 && Math.abs(xs[lo - 1] - x) <= Math.abs(xs[lo] - x) ? lo - 1 : lo;
}

/** 数の短い表し方（有効数字 4 桁、大きい・小さい数は指数） */
export function shortNumber(v: number): string {
  if (!Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e7 || a < 1e-3)) return v.toExponential(2);
  return String(parseFloat(v.toPrecision(4)));
}

/** 値の列を CSV に（x の列と系列ごとの列。系列の x が違えば x ごとに行を分け、無い所は空） */
export function toCsv(series: Series[], xName = "x"): string {
  const xs = [...new Set(series.flatMap((s) => Array.from(s.x)))].sort((a, b) => a - b);
  const maps = series.map((s) => new Map(Array.from(s.x, (x, i) => [x, s.y[i]])));
  const quote = (s: string) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  const lines = [[xName, ...series.map((s) => s.name)].map(quote).join(",")];
  for (const x of xs) lines.push([x, ...maps.map((m) => (m.has(x) && Number.isFinite(m.get(x)!) ? m.get(x) : ""))].join(","));
  return `${lines.join("\n")}\n`;
}

/** 文字をファイルとして保存する（ブラウザのダウンロード） */
export function download(name: string, text: string, type = "text/plain"): void {
  if (typeof document === "undefined") return;
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  a.click();
  URL.revokeObjectURL(url);
}

/** 積み上げの上端: 系列 i の各点の、下から系列 i までの和（同じ x の並びの系列を前提）。値の無い点（NaN）は
 *  その系列だけ切り、上の系列は下の和に積む。すべて値が無ければ全系列 NaN（線を切る） */
export function stackTops(series: Series[]): Float64Array[] {
  const n = series.length ? series[0].x.length : 0;
  const base = new Float64Array(n);
  const any = new Uint8Array(n);
  for (const s of series) for (let k = 0; k < n; k++) if (Number.isFinite(s.y[k])) any[k] = 1;
  return series.map((s) => {
    const top = new Float64Array(n);
    for (let k = 0; k < n; k++) {
      const v = s.y[k];
      top[k] = any[k] && Number.isFinite(v) ? base[k] + v : NaN;
      if (Number.isFinite(top[k])) base[k] = top[k];
    }
    return top;
  });
}

/** 散布図で (px, py)（画素）にいちばん近い点の位置（radius 画素より遠ければ -1）。xOf・yOf は値 → 画素 */
export function nearestPoint(x: ArrayLike<number>, y: ArrayLike<number>, px: number, py: number,
                             xOf: (v: number) => number, yOf: (v: number) => number, radius = 12): number {
  let best = -1, bestD = radius * radius;
  for (let i = 0; i < x.length; i++) {
    const dx = xOf(x[i]) - px, dy = yOf(y[i]) - py, d = dx * dx + dy * dy;
    if (d <= bestD) {
      best = i;
      bestD = d;
    }
  }
  return best;
}
