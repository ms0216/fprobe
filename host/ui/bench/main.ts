// 時間軸の部品の性能の測定（試作 prototypes/ui-bench と同じデータ・同じ視点の列・同じ測り方）。
// 本体の部品をそのまま使う: 間引き = lib/timeline/decimator（Worker）、描画 = renderer（WebGL2 / Canvas 2D）+ overlay。
// Timeline.svelte の paint() と TimelineState.render() が呼ぶものと同じ。
//   ?r=webgl2 | canvas2d   描画     ?d=worker | inline   間引きの置き場所（inline は試作と同じく描画のスレッドで）
//   ?tile=rows             段の多いタイル（make_rows_tile.py: 波形 16 + タスク 8 + 割り込み 4 + 値 4 + 解読 2 の段）
// 結果は window.__result（run.py が読む）。終わると title = "done"。
import "../src/app.css";
import type { TimelineFrame } from "../src/lib/timeline/compose";
import { InlineDecimator, createDecimator } from "../src/lib/timeline/decimator";
import { decodeFrame } from "../src/lib/timeline/frame";
import { colorsOf, cssHeight, layoutOf } from "../src/lib/timeline/layout";
import { drawOverlay } from "../src/lib/timeline/overlay";
import { createRenderer } from "../src/lib/timeline/renderer";
import { EMPTY_CONFIG, RowRegistry } from "../src/lib/timeline/rows";
import { specsOf } from "../src/lib/timeline/state";

const q = new URLSearchParams(location.search);
const prefer = q.get("r") === "canvas2d" ? "canvas2d" : "webgl2";
const where = q.get("d") === "inline" ? "inline" : "worker";
const tileName = q.get("tile") === "rows" ? "rows-tile.bin" : "tile.bin";
const status = document.getElementById("status")!;

function stats(xs: number[]) {
  const a = [...xs].sort((p, r) => p - r);
  const at = (f: number) => a[Math.min(a.length - 1, Math.floor(f * a.length))];
  return { n: a.length, mean: a.reduce((s, x) => s + x, 0) / a.length, p50: at(0.5), p95: at(0.95), p99: at(0.99),
           max: a[a.length - 1], over_16_7: a.filter((x) => x > 1000 / 60).length / a.length };
}

const started = performance.now();
const [buf, setup] = await Promise.all([
  fetch(`data/${tileName}`).then((r) => r.arrayBuffer()),
  fetch("data/views.json").then((r) => r.json()),
]);
const { data: _data, ...header } = decodeFrame(buf).result;
const decimator = where === "inline" ? new InlineDecimator() : createDecimator();
decimator.addTile(1, header, buf);                        // Worker へは transfer（複写しない）
const W: number = setup.width;
const plot = document.getElementById("plot") as HTMLCanvasElement;
const overlay = document.getElementById("overlay") as HTMLCanvasElement;
const registry = new RowRegistry();
registry.merge(header);
const visible = registry.visible(EMPTY_CONFIG);
const rows = specsOf(visible);
for (const c of [plot, overlay]) c.style.height = `${cssHeight(visible)}px`;
const renderer = createRenderer(plot, prefer);
const layout = layoutOf(W, 1, visible);
const colors = colorsOf(document.body);
const units = visible.map((r) => r.def.unit ?? null);
await decimator.frame({ tile: 1, v0: 0, v1: 1, width: W, rows });   // Worker が tile を組み立て終わるのを待つ
const loadMs = performance.now() - started;
status.textContent = `loaded ${loadMs.toFixed(0)} ms (${renderer.kind}, ${decimator.kind})`;

const views: [number, number][] = setup.views;
const work: number[] = [], render: number[] = [], decimate: number[] = [], roundTrip: number[] = [], intervals: number[] = [];
let i = 0, last = 0;

function paint(f: TimelineFrame | null, v0: number, v1: number): number {
  const t = performance.now();
  renderer.draw(f, layout, colors);
  drawOverlay(overlay, layout, colors, { view: { start: v0, end: v1 }, tickS: header.tick_s, origin: 0, resolution: header.tick_s, trigger: null,
                                         cursors: { selection: null, mouse: null, reference: null }, frame: f,
                                         selected: null, units });
  return performance.now() - t;
}

// inline: 試作と同じく 1 回の描画の機会の中で間引き + 描画（作業 = 両方）
async function serial(now: number) {
  if (i > 0) intervals.push(now - last);
  last = now;
  if (i >= views.length) return finish();
  const [v0, v1] = views[i];
  const t0 = performance.now();
  const f = await decimator.frame({ tile: 1, v0, v1, width: W, rows });
  const t1 = performance.now();
  const r = paint(f, v0, v1);
  roundTrip.push(t1 - t0);
  render.push(r);
  decimate.push(f?.decimateMs ?? 0);
  work.push(t1 - t0 + r);
  i++;
  requestAnimationFrame(serial);
}

// worker: 画面（Timeline.svelte + TimelineState）と同じ 2 段の流れ。描画の機会ごとに、届いた結果があれば描き、
// すぐ次の視点を Worker へ出す（描画のスレッドは間引きを待たない）。間隔 = 新しい視点を描いた機会どうしの間
// spin=1: 新しい結果の無い機会にも前の絵を描き直す。垂直同期を外した Chromium は、絵を出さなかった機会の後の
// 描画の機会を約 18 ms 遅らせる（実際の画面は垂直同期の上で動くので、その測定は --vsync で）。spin は流れの実力を見る
const spin = q.get("spin") === "1";
let shown: { f: TimelineFrame | null; v0: number; v1: number } | null = null;
let ready: { f: TimelineFrame | null; t0: number; t1: number } | null = null;
function post() {
  const [v0, v1] = views[i];
  const t0 = performance.now();
  void decimator.frame({ tile: 1, v0, v1, width: W, rows }).then((f) => (ready = { f, t0, t1: performance.now() }));
}
function pipelined(now: number) {
  if (ready) {
    const { f, t0, t1 } = ready;
    ready = null;
    const [v0, v1] = views[i];
    if (i > 0) intervals.push(now - last);
    last = now;
    const r = paint(f, v0, v1);
    shown = { f, v0, v1 };
    roundTrip.push(t1 - t0);
    render.push(r);
    decimate.push(f?.decimateMs ?? 0);
    work.push(r);                                        // 描画のスレッドが使った時間
    i++;
    if (i >= views.length) return finish();
    post();
  } else if (spin && shown) {
    paint(shown.f, shown.v0, shown.v1);                  // 垂直同期を外した測定: 描画の機会を止めない（下の注）
  }
  requestAnimationFrame(pipelined);
}

function finish() {
  const skip = setup.warmup as number;
  const cut = (xs: number[]) => xs.slice(skip);
  // 間隔は 2 枚目から数える（個数は 1 つ少ない）
  (window as any).__result = {
    impl: `${renderer.kind}-${decimator.kind}`, tile: tileName, rows: rows.length, ua: navigator.userAgent, load_ms: loadMs, frames: views.length - skip,
    work_ms: stats(cut(work)), render_ms: stats(cut(render)), decimate_ms: stats(cut(decimate)),
    round_trip_ms: stats(cut(roundTrip)), interval_ms: stats(cut(intervals)),
    js_heap_mb: (performance as any).memory ? (performance as any).memory.usedJSHeapSize / 2 ** 20 : null,
  };
  status.textContent = "done";
  document.title = "done";
}

if (where === "inline") requestAnimationFrame(serial);
else {
  post();
  requestAnimationFrame(pipelined);
}
