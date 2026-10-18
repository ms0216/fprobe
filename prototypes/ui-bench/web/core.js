// 共通の間引き（README の「共通のアルゴリズム」）。描画に依らない。Rust 版・numpy 版と同じ定義。
export const LABEL_W = 100, W = 1500, AXIS_H = 30, ROW_H = 26, LANE_H = 40, CH = 16, LANES = 8;
export const HEIGHT = AXIS_H + CH * ROW_H + LANES * LANE_H + 4;
export const LABEL_MIN_PX = 60;
export const ST_X = 2;

/** 昇順の a で、a[i] >= x となる最初の i（lo から探す） */
export function lowerBound(a, x, lo = 0, hi = a.length) {
  while (lo < hi) { const m = (lo + hi) >>> 1; if (a[m] < x) lo = m + 1; else hi = m; }
  return lo;
}
/** 昇順の a で、a[i] > x となる最初の i */
export function upperBound(a, x, lo = 0, hi = a.length) {
  while (lo < hi) { const m = (lo + hi) >>> 1; if (a[m] <= x) lo = m + 1; else hi = m; }
  return lo;
}

export async function loadData(base = "../data/") {
  const t0 = performance.now();
  const [meta, views, edges, ev0, ev1] = await Promise.all([
    fetch(base + "meta.json").then((r) => r.json()),
    fetch(base + "views.json").then((r) => r.json()),
    fetch(base + "edges.f64").then((r) => r.arrayBuffer()),
    fetch(base + "ev_t0.f64").then((r) => r.arrayBuffer()),
    fetch(base + "ev_t1.f64").then((r) => r.arrayBuffer()),
  ]);
  const all = new Float64Array(edges), t0s = new Float64Array(ev0), t1s = new Float64Array(ev1);
  const o = meta.edge_offsets, e = meta.event_offsets;
  const channels = [...Array(meta.channels).keys()].map((c) => all.subarray(o[c], o[c + 1]));
  const lanes = meta.lanes.map((name, l) => ({
    name, t0: t0s.subarray(e[l], e[l + 1]), t1: t1s.subarray(e[l], e[l + 1]), maxDur: meta.lane_max_duration[l],
  }));
  return { meta, views, channels, lanes, loadMs: performance.now() - t0 };
}

/** lo から指数的に広げてから二分探索（画素ごとの境界は単調に増えるので、近くを先に見る） */
function gallop(a, x, lo, strict) {
  const n = a.length, less = strict ? (v) => v <= x : (v) => v < x;
  if (lo >= n || !less(a[lo])) return lo;
  let step = 1;
  while (lo + step < n && less(a[lo + step])) step *= 2;
  return strict ? upperBound(a, x, lo + (step >> 1) + 1, Math.min(n, lo + step))
                : lowerBound(a, x, lo + (step >> 1) + 1, Math.min(n, lo + step));
}

/** 波形: 画素ごとに 0 / 1 / ST_X。out は CH * W の Uint8Array */
export function decimateWave(channels, v0, v1, out) {
  const span = v1 - v0;
  for (let c = 0; c < channels.length; c++) {
    const e = channels[c], row = c * W;
    let s = v0, lo = upperBound(e, s);
    for (let p = 0; p < W; p++) {
      const en = v0 + ((p + 1) * span) / W;
      const before = lo;                                   // s までの変化の数
      const hiL = gallop(e, en, before, false);
      out[row + p] = before < hiL ? ST_X : before & 1;
      lo = gallop(e, en, hiL, true);
      s = en;
    }
  }
}

/** 出来事の段: 疎なら区間の一覧 {sparse: true, items: [x0, x1, index]...}、密なら画素ごとの件数 */
export function decimateLane(lane, v0, v1, counts) {
  const i0 = lowerBound(lane.t0, v0 - lane.maxDur), i1 = lowerBound(lane.t0, v1, i0);
  const k = W / (v1 - v0);
  if (i1 - i0 <= 2 * W) {
    const items = [];
    for (let i = i0; i < i1; i++) {
      const x1 = (lane.t1[i] - v0) * k;
      if (x1 < 0) continue;
      const x0 = Math.max(0, (lane.t0[i] - v0) * k);
      items.push(x0, Math.max(x0 + 1, Math.min(W, x1)), i);
    }
    return { sparse: true, items };
  }
  let prev = lowerBound(lane.t0, v0, i0), max = 1;
  for (let p = 0; p < W; p++) {
    const next = lowerBound(lane.t0, v0 + ((p + 1) * (v1 - v0)) / W, prev);
    counts[p] = next - prev; if (counts[p] > max) max = counts[p];
    prev = next;
  }
  return { sparse: false, max };
}

export function axisTicks(v0, v1, tickS) {
  const span = (v1 - v0) * tickS, raw = span / 10, p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * p).find((s) => s >= raw) / tickS;
  const out = [];
  for (let t = Math.ceil(v0 / step) * step; t < v1; t += step) out.push(t);
  return out;
}

export function fmtTime(s) {
  const a = Math.abs(s);
  const [k, u] = a < 1e-6 ? [1e9, "ns"] : a < 1e-3 ? [1e6, "us"] : a < 1 ? [1e3, "ms"] : [1, "s"];
  return `${+(s * k).toPrecision(4)} ${u}`;
}

export function stats(xs) {
  const a = [...xs].sort((p, q) => p - q), q = (f) => a[Math.min(a.length - 1, Math.floor(f * a.length))];
  return { n: a.length, mean: a.reduce((s, x) => s + x, 0) / a.length, p50: q(0.5), p95: q(0.95), p99: q(0.99), max: a[a.length - 1] };
}
