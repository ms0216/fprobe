// 間引きだけの時間（描画なし）を Node（V8）で測る: node web/decim_node.mjs
import { readFileSync } from "node:fs";
import { W, CH, LANES, decimateWave, decimateLane, stats } from "./core.js";

const dir = new URL("../data/", import.meta.url);
const f64 = (n) => { const b = readFileSync(new URL(n, dir)); return new Float64Array(b.buffer, b.byteOffset, b.byteLength / 8); };
const meta = JSON.parse(readFileSync(new URL("meta.json", dir)));
const views = JSON.parse(readFileSync(new URL("views.json", dir)));
const all = f64("edges.f64"), t0s = f64("ev_t0.f64"), t1s = f64("ev_t1.f64");
const o = meta.edge_offsets, e = meta.event_offsets;
const channels = [...Array(CH).keys()].map((c) => all.subarray(o[c], o[c + 1]));
const lanes = meta.lanes.map((name, l) => ({ name, t0: t0s.subarray(e[l], e[l + 1]), t1: t1s.subarray(e[l], e[l + 1]), maxDur: meta.lane_max_duration[l] }));
const buf = new Uint8Array(CH * W), counts = [...Array(LANES)].map(() => new Uint32Array(W));
const wave = [], lane = [], check = {};
const CHECK = [60, 360, 660, 959];
for (const [i, [v0, v1]] of views.entries()) {
  let t = performance.now(); decimateWave(channels, v0, v1, buf); const a = performance.now() - t;
  t = performance.now(); lanes.forEach((ln, l) => decimateLane(ln, v0, v1, counts[l])); const b = performance.now() - t;
  if (i >= meta.warmup) { wave.push(a); lane.push(b); }
  if (CHECK.includes(i)) {                     // 実装どうしの突き合わせ用の和
    let sum = 0; for (let k = 0; k < buf.length; k++) sum += buf[k] * ((k % 251) + 1);
    check[i] = sum;
  }
}
console.log(JSON.stringify({ impl: "node-decimate", wave_ms: stats(wave), lanes_ms: stats(lane), check }, null, 1));
