//! 共通の間引き（README の「共通のアルゴリズム」、web/core.js と同じ定義）。描画に依らない。

pub const LABEL_W: f32 = 100.0;
pub const W: usize = 1500;
pub const AXIS_H: f32 = 30.0;
pub const ROW_H: f32 = 26.0;
pub const LANE_H: f32 = 40.0;
pub const CH: usize = 16;
pub const LANES: usize = 8;
pub const HEIGHT: f32 = AXIS_H + CH as f32 * ROW_H + LANES as f32 * LANE_H + 4.0;
pub const LABEL_MIN_PX: f32 = 60.0;
pub const ST_X: u8 = 2;

pub struct Lane {
    pub name: String,
    pub t0: Vec<f64>,
    pub t1: Vec<f64>,
    pub max_dur: f64,
}

pub struct Data {
    pub span: f64,
    pub tick_s: f64,
    pub warmup: usize,
    pub channels: Vec<Vec<f64>>,
    pub lanes: Vec<Lane>,
    pub views: Vec<(f64, f64)>,
}

fn f64s(b: &[u8]) -> Vec<f64> {
    b.chunks_exact(8).map(|c| f64::from_le_bytes(c.try_into().unwrap())).collect()
}

/// data/ の 5 つのファイルの名前（読む順）
pub const FILES: [&str; 5] = ["meta.json", "views.json", "edges.f64", "ev_t0.f64", "ev_t1.f64"];

impl Data {
    #[cfg(not(target_arch = "wasm32"))]
    pub fn load(dir: &std::path::Path) -> Data {
        let b: Vec<Vec<u8>> = FILES.iter().map(|f| std::fs::read(dir.join(f)).expect("data file")).collect();
        Data::from_bytes(&b)
    }

    /// FILES の順のバイト列から
    pub fn from_bytes(b: &[Vec<u8>]) -> Data {
        let meta: serde_json::Value = serde_json::from_slice(&b[0]).unwrap();
        let views: Vec<(f64, f64)> = serde_json::from_slice::<Vec<[f64; 2]>>(&b[1])
            .unwrap().into_iter().map(|[a, b]| (a, b)).collect();
        let edges = f64s(&b[2]);
        let (e0, e1) = (f64s(&b[3]), f64s(&b[4]));
        let off = |k: &str| -> Vec<usize> { meta[k].as_array().unwrap().iter().map(|v| v.as_u64().unwrap() as usize).collect() };
        let (eo, lo) = (off("edge_offsets"), off("event_offsets"));
        let channels = (0..CH).map(|c| edges[eo[c]..eo[c + 1]].to_vec()).collect();
        let lanes = (0..LANES).map(|l| Lane {
            name: meta["lanes"][l].as_str().unwrap().to_string(),
            t0: e0[lo[l]..lo[l + 1]].to_vec(),
            t1: e1[lo[l]..lo[l + 1]].to_vec(),
            max_dur: meta["lane_max_duration"][l].as_f64().unwrap(),
        }).collect();
        Data {
            span: meta["span"].as_f64().unwrap(), tick_s: meta["tick_s"].as_f64().unwrap(),
            warmup: meta["warmup"].as_u64().unwrap() as usize, channels, lanes, views,
        }
    }
}

/// a[i] >= x となる最初の i（lo..hi で）
fn lower_bound(a: &[f64], x: f64, mut lo: usize, mut hi: usize) -> usize {
    while lo < hi { let m = (lo + hi) / 2; if a[m] < x { lo = m + 1 } else { hi = m } }
    lo
}
/// a[i] > x となる最初の i（lo..hi で）
fn upper_bound(a: &[f64], x: f64, mut lo: usize, mut hi: usize) -> usize {
    while lo < hi { let m = (lo + hi) / 2; if a[m] <= x { lo = m + 1 } else { hi = m } }
    lo
}
/// lo から指数的に広げてから二分探索
fn gallop(a: &[f64], x: f64, lo: usize, strict: bool) -> usize {
    let n = a.len();
    let less = |v: f64| if strict { v <= x } else { v < x };
    if lo >= n || !less(a[lo]) { return lo; }
    let mut step = 1;
    while lo + step < n && less(a[lo + step]) { step *= 2; }
    let (s, e) = (lo + (step >> 1) + 1, n.min(lo + step));
    if strict { upper_bound(a, x, s, e) } else { lower_bound(a, x, s, e) }
}

/// 波形: 画素ごとに 0 / 1 / ST_X。out は CH * W
pub fn decimate_wave(channels: &[Vec<f64>], v0: f64, v1: f64, out: &mut [u8]) {
    let span = v1 - v0;
    for (c, e) in channels.iter().enumerate() {
        let row = c * W;
        let mut lo = upper_bound(e, v0, 0, e.len());
        for p in 0..W {
            let en = v0 + ((p + 1) as f64 * span) / W as f64;
            let before = lo;
            let hi_l = gallop(e, en, before, false);
            out[row + p] = if before < hi_l { ST_X } else { (before & 1) as u8 };
            lo = gallop(e, en, hi_l, true);
        }
    }
}

pub enum LaneOut {
    /// (x0, x1, 番号)
    Sparse(Vec<(f32, f32, usize)>),
    /// 画素ごとの件数（counts）の最大
    Dense(u32),
}

pub fn decimate_lane(lane: &Lane, v0: f64, v1: f64, counts: &mut [u32]) -> LaneOut {
    let n = lane.t0.len();
    let i0 = lower_bound(&lane.t0, v0 - lane.max_dur, 0, n);
    let i1 = lower_bound(&lane.t0, v1, i0, n);
    let k = W as f64 / (v1 - v0);
    if i1 - i0 <= 2 * W {
        let mut items = Vec::with_capacity(i1 - i0);
        for i in i0..i1 {
            let x1 = (lane.t1[i] - v0) * k;
            if x1 < 0.0 { continue; }
            let x0 = ((lane.t0[i] - v0) * k).max(0.0);
            items.push((x0 as f32, (x0 + 1.0).max(x1.min(W as f64)) as f32, i));
        }
        return LaneOut::Sparse(items);
    }
    let mut prev = lower_bound(&lane.t0, v0, i0, n);
    let mut max = 1;
    for p in 0..W {
        let next = lower_bound(&lane.t0, v0 + ((p + 1) as f64 * (v1 - v0)) / W as f64, prev, n);
        counts[p] = (next - prev) as u32;
        max = max.max(counts[p]);
        prev = next;
    }
    LaneOut::Dense(max)
}

pub fn axis_ticks(v0: f64, v1: f64, tick_s: f64) -> Vec<f64> {
    let span = (v1 - v0) * tick_s;
    let raw = span / 10.0;
    let p = 10f64.powf(raw.log10().floor());
    let step = [1.0, 2.0, 5.0, 10.0].iter().map(|m| m * p).find(|s| *s >= raw).unwrap() / tick_s;
    let mut out = vec![];
    let mut t = (v0 / step).ceil() * step;
    while t < v1 { out.push(t); t += step; }
    out
}

pub fn fmt_time(s: f64) -> String {
    let a = s.abs();
    let (k, u) = if a < 1e-6 { (1e9, "ns") } else if a < 1e-3 { (1e6, "us") } else if a < 1.0 { (1e3, "ms") } else { (1.0, "s") };
    format!("{} {}", (s * k * 1000.0).round() / 1000.0, u)
}

pub struct Stats { pub n: usize, pub mean: f64, pub p50: f64, pub p95: f64, pub p99: f64, pub max: f64 }

pub fn stats(xs: &[f64]) -> Stats {
    let mut a = xs.to_vec();
    a.sort_by(|p, q| p.partial_cmp(q).unwrap());
    let q = |f: f64| a[((f * a.len() as f64) as usize).min(a.len() - 1)];
    Stats { n: a.len(), mean: a.iter().sum::<f64>() / a.len() as f64, p50: q(0.5), p95: q(0.95), p99: q(0.99), max: *a.last().unwrap() }
}

impl Stats {
    pub fn json(&self) -> serde_json::Value {
        serde_json::json!({"n": self.n, "mean": self.mean, "p50": self.p50, "p95": self.p95, "p99": self.p99, "max": self.max})
    }
}
