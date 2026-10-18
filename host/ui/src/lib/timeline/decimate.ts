// 毎フレームの間引き: タイルから、視点 [v0, v1)（整数）を W 画素にした「段 × 画素」の状態を作る。Svelte・DOM に依らない
// （Web Worker で動かす。Worker が無い環境ではそのまま呼ぶ）。
//
// 定義は Python と同じ（試験で突き合わせる。参照の実装は host/tests/timeline_ref.py）:
// - 波形: 画素 px = [v0 + ⌊px·span/W⌋, v0 + ⌈(px+1)·span/W⌉)。かたまりごとに s0 = trunc((p0 − t0)/tps)、
//   s1 = min(length, max(s0+1, ⌈(p1 − t0)/tps⌉))。s0 の値（s0 自身の変化を含む）、(s0, s1) に変化があれば「変化」。
//   かたまりをまたぐ画素は、同じならその値・違えば変化・どこにも入らなければ「無し」
//   （la.Waveform.view・Recording.waveform_view と同じ）。
// - 出来事の件数: 画素 px = [v0 + ⌊px·span/W⌋, v0 + ⌊(px+1)·span/W⌋) に t が入る件数（Recording.density と同じ）。
// - 要約（区間ごとの状態・件数）からは近似: 画素に重なる区間の状態を合わせる / 区間の始めが画素に入る件数の和。
// 仕様: docs/superpowers/specs/2026-10-18-timeline-tiles-design.md §4
import { ST_CHANGE, ST_NONE, type LaneData, type Piece, type RowEvents, type SummaryLane, type Tile } from "./tile";

/** ⌊a / b⌋（b > 0。|a| < 2^53 の整数で正確） */
export function floorDiv(a: number, b: number): number {
  let q = Math.floor(a / b);
  if (q * b > a) q -= 1;
  else if ((q + 1) * b <= a) q += 1;
  return q;
}

export function ceilDiv(a: number, b: number): number {
  return -floorDiv(-a, b);
}

/** ⌈a / tps⌉（tps が整数なら正確。そうでなければ Python の float の // と同じく浮動小数で） */
function ceilBy(a: number, tps: number): number {
  return Number.isInteger(tps) ? ceilDiv(a, tps) : -Math.floor(-a / tps);
}

/** 昇順の a で、a[i] >= x となる最初の i（lo から） */
export function lowerBound(a: ArrayLike<number>, x: number, lo = 0, hi = a.length): number {
  while (lo < hi) {
    const m = (lo + hi) >>> 1;
    if (a[m] < x) lo = m + 1;
    else hi = m;
  }
  return lo;
}

/** 昇順の a で、a[i] > x となる最初の i（lo から） */
export function upperBound(a: ArrayLike<number>, x: number, lo = 0, hi = a.length): number {
  while (lo < hi) {
    const m = (lo + hi) >>> 1;
    if (a[m] <= x) lo = m + 1;
    else hi = m;
  }
  return lo;
}

/** lo から指数的に広げてから二分探索（画素の境は単調に増えるので、前の位置の近くを先に見る）。
 *  strict = false: a[i] >= x の最初、strict = true: a[i] > x の最初 */
function gallop(a: Float64Array, x: number, lo: number, strict: boolean): number {
  const n = a.length;
  if (lo >= n || (strict ? a[lo] > x : a[lo] >= x)) return lo;
  let step = 1;
  while (lo + step < n && (strict ? a[lo + step] <= x : a[lo + step] < x)) step *= 2;
  const from = lo + (step >> 1) + 1, to = Math.min(n, lo + step);
  return strict ? upperBound(a, x, from, to) : lowerBound(a, x, from, to);
}

/** 視点の画素の境（整数）。毎フレーム 1 回作り、段ごとに使い回す */
export class Pixels {
  readonly lo: Float64Array;          // 画素の始め（波形・件数で共通）
  readonly hiWave: Float64Array;      // 波形の画素の終わり（切り上げ）
  readonly hiCount: Float64Array;     // 件数の画素の終わり（切り捨て）
  readonly span: number;

  constructor(readonly v0: number, readonly v1: number, readonly width: number) {
    this.span = v1 - v0;
    this.lo = new Float64Array(width);
    this.hiWave = new Float64Array(width);
    this.hiCount = new Float64Array(width);
    for (let px = 0; px < width; px++) {
      this.lo[px] = v0 + floorDiv(px * this.span, width);
      this.hiWave[px] = v0 + ceilDiv((px + 1) * this.span, width);
      this.hiCount[px] = v0 + floorDiv((px + 1) * this.span, width);
    }
  }
}

/** 波形: out[row * W + px] に状態（0 / 1 / 変化 / 無し）。row は tile.wave.channels の並び */
export function decimateWave(tile: Tile, px: Pixels, out: Uint8Array): void {
  const w = tile.wave, W = px.width;
  out.fill(ST_NONE, 0, w.channels.length * W);
  if (w.mode === "raw") {
    const rowOf = new Map(w.channels.map((ch, r) => [ch, r]));
    for (const piece of w.pieces) rawPiece(tile, piece, rowOf, px, out);
  } else if (w.mode === "summary") {
    summaryWave(w.states, w.start, w.bin, w.bins, w.channels.length, px, out);
  }
}

function rawPiece(tile: Tile, piece: Piece, rowOf: Map<number, number>, px: Pixels, out: Uint8Array): void {
  const W = px.width;
  // この かたまりに重なり、タイルの範囲に収まる画素と、そのサンプルの範囲 [a, b)
  const first = upperBound(px.hiWave, Math.max(piece.t0, tile.t0 - 1));     // hiWave > t0
  const a = new Float64Array(W), b = new Float64Array(W);
  let from = -1, to = -1;
  for (let p = first; p < W; p++) {
    const p0 = px.lo[p], p1 = px.hiWave[p];
    if (p0 >= piece.t1) break;
    if (p1 <= piece.t0 || p0 < tile.t0 || p1 > tile.t1) continue;
    const s0 = Math.max(0, Math.trunc((p0 - piece.t0) / piece.tps));
    const s1 = Math.min(piece.length, Math.max(s0 + 1, ceilBy(p1 - piece.t0, piece.tps)));
    a[p] = s0;
    b[p] = Math.max(s0 + 1, s1);
    if (from < 0) from = p;
    to = p + 1;
  }
  if (from < 0) return;
  piece.channels.forEach((ch, k) => {
    const row = rowOf.get(ch);
    if (row === undefined) return;
    const edges = piece.edges[k], level = piece.levels[k], base = row * W;
    let before = 0;
    for (let p = from; p < to; p++) {
      if (b[p] === 0) continue;                      // タイルの外の画素（上の continue）
      before = gallop(edges, a[p], before, true);    // a までの変化の数（a 自身の変化を含む）
      const changed = gallop(edges, b[p], before, false) > before;
      const state = changed ? ST_CHANGE : level ^ (before & 1);
      const have = out[base + p];
      out[base + p] = have === ST_NONE || have === state ? state : ST_CHANGE;
    }
  });
}

function summaryWave(states: Uint8Array, start: number, bin: number, bins: number, rows: number, px: Pixels,
                     out: Uint8Array): void {
  const W = px.width;
  for (let p = 0; p < W; p++) {
    const lo = Math.max(0, floorDiv(px.lo[p] - start, bin));
    const hi = Math.min(bins, ceilDiv(px.hiWave[p] - start, bin));
    if (hi <= lo) continue;
    for (let r = 0; r < rows; r++) {
      let s = ST_NONE;
      const base = r * bins;
      for (let i = lo; i < hi && s !== ST_CHANGE; i++) {
        const v = states[base + i];
        if (v === ST_NONE) continue;
        s = s === ST_NONE || s === v ? v : ST_CHANGE;
      }
      out[r * W + p] = s;
    }
  }
}

/** 出来事の段の画素ごとの件数（out は W 個）。タイルの外の画素は 0 */
export function laneCounts(tile: Tile, lane: LaneData, px: Pixels, out: Uint32Array): void {
  if (lane.mode === "raw") {
    countsOf(tile, lane.t, px, out);
    return;
  }
  binCounts(prefixOf(lane.counts, 0, lane.bins), lane.start, lane.bin, lane.bins, px, out);
}

/** 昇順の t の、画素 [p0, p1) ごとの件数（Recording.density と同じ境）。タイルの外の画素は 0 */
export function countsOf(tile: Tile, t: Float64Array, px: Pixels, out: Uint32Array): void {
  const W = px.width;
  out.fill(0, 0, W);
  let lo = lowerBound(t, px.lo[0]);
  for (let p = 0; p < W; p++) {
    const p0 = px.lo[p], p1 = px.hiCount[p];
    const hi = gallop(t, p1, lo, false);
    if (p0 >= tile.t0 && p1 <= tile.t1) out[p] = hi - lo;
    lo = hi;
  }
}

/** 要約: 区間の始めが画素に入るものの和（累積和で O(W)） */
function binCounts(prefix: Float64Array, start: number, bin: number, bins: number, px: Pixels, out: Uint32Array): void {
  for (let p = 0; p < px.width; p++) {
    const lo = Math.min(bins, Math.max(0, ceilDiv(px.lo[p] - start, bin)));
    const hi = Math.min(bins, Math.max(0, ceilDiv(px.hiCount[p] - start, bin)));
    out[p] = prefix[hi] - prefix[lo];
  }
}

const prefixes = new WeakMap<Uint32Array, Map<number, Float64Array>>();
/** counts[offset .. offset + bins) の累積和（区画ごと・段ごとに 1 回だけ作る） */
function prefixOf(counts: Uint32Array, offset: number, bins: number): Float64Array {
  let byOffset = prefixes.get(counts);
  if (!byOffset) prefixes.set(counts, (byOffset = new Map()));
  let prefix = byOffset.get(offset);
  if (!prefix) {
    prefix = new Float64Array(bins + 1);
    for (let i = 0; i < bins; i++) prefix[i + 1] = prefix[i] + counts[offset + i];
    byOffset.set(offset, prefix);
  }
  return prefix;
}

// --- 段（行）ごとの間引き（記録の段の規則で分けた段） ---------------------------------------------------------------
// 仕様: docs/superpowers/specs/2026-10-18-timeline-tiles-design.md §4.1

/** 要約の段の 1 つの段 row の、画素ごとの件数 */
export function summaryRowCounts(lane: SummaryLane, row: number, px: Pixels, out: Uint32Array): void {
  binCounts(prefixOf(lane.rowCounts, row * lane.bins, lane.bins), lane.start, lane.bin, lane.bins, px, out);
}

/** 値の段（生）の画素ごとの範囲: 画素 [p0, p1)（p1 は波形と同じ切り上げ）に入るサンプルの値と、その前の最後の
 *  サンプルの値（保たれている値）の最小・最大。どちらも無い・タイルの外の画素は NaN */
export function valueEnvelope(tile: Tile, ev: RowEvents, px: Pixels, low: Float64Array, high: Float64Array): void {
  const W = px.width, t = ev.t, v = ev.value;
  low.fill(NaN, 0, W);
  high.fill(NaN, 0, W);
  let j = lowerBound(t, px.lo[0]);
  for (let p = 0; p < W; p++) {
    const p0 = px.lo[p], p1 = px.hiWave[p];
    j = gallop(t, p0, j, false);
    const k = gallop(t, p1, j, false);
    if (p0 < tile.t0 || p1 > tile.t1) continue;
    let lo = Infinity, hi = -Infinity;
    for (let i = j > 0 ? j - 1 : j; i < k; i++) {
      const x = v[i];
      if (x < lo) lo = x;
      if (x > hi) hi = x;
    }
    if (lo <= hi) {
      low[p] = lo;
      high[p] = hi;
    }
  }
}

/** 値の段（要約）の画素ごとの範囲: 画素に重なる区間の最小・最大（保たれている値は分からないので含めない） */
export function summaryEnvelope(lane: SummaryLane, row: number, px: Pixels, low: Float64Array, high: Float64Array): void {
  const W = px.width;
  low.fill(NaN, 0, W);
  high.fill(NaN, 0, W);
  if (!lane.rowMin || !lane.rowMax) return;
  const base = row * lane.bins;
  for (let p = 0; p < W; p++) {
    const a = Math.max(0, floorDiv(px.lo[p] - lane.start, lane.bin));
    const b = Math.min(lane.bins, ceilDiv(px.hiWave[p] - lane.start, lane.bin));
    let lo = Infinity, hi = -Infinity;
    for (let i = a; i < b; i++) {
      const x = lane.rowMin[base + i], y = lane.rowMax[base + i];
      if (x < lo) lo = x;
      if (y > hi) hi = y;
    }
    if (lo <= hi) {
      low[p] = lo;
      high[p] = hi;
    }
  }
}

/** 段の中で視点 [v0, v1) に見えている出来事の番号の範囲 [i0, i1)（始めの前から続く区間も含む。i0 の前は無い） */
export function visibleRange(ev: RowEvents, v0: number, v1: number): [number, number] {
  let i0 = lowerBound(ev.t, v0 - ev.maxDuration);
  const i1 = lowerBound(ev.t, v1, i0);
  while (i0 < i1 && ev.tEnd[i0] < v0 && ev.t[i0] < v0) i0++;
  return [i0, i1];
}
