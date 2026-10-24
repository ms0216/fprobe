// 1 フレームの絵の材料: タイルを視点で間引き、描画（WebGL2 / Canvas 2D）がそのまま使う形にする。Svelte・DOM に依らない
// （Worker の中で作り、配列は transfer で画面へ返す）。
//
// 描く段（RowSpec の並び）ごとに、画素ごとの RGBA（4 バイト）を作る。段の描き方（mode）ごとの意味:
//   logic    r = 状態（0 / 1 / 変化 / 無し）
//   density  r = 件数の棒の高さ（対数、1..255。0 = 無し）。header（まとまりの見出し）も同じ
//   intervals / bus / state  r = 色 + 1（0 = 無し）、g = 印（START / END / MIXED）、b = 入れ子の深さ、a = 下の区間の色 + 1
//   points   r = 色 + 1、g = MARK（この画素に印）
//   values   r = 色 + 1（値がある）、b = 最小、a = 最大（0..255 = 段の目盛りの下から上）
// 生の出来事が多すぎる段（見えている件数 > 2W）は density で描く。
// 仕様: docs/superpowers/specs/2026-10-18-timeline-tiles-design.md §4.1・§5
import {
  Pixels, countsOf, decimateWave, laneCounts, summaryEnvelope, summaryRowCounts, valueEnvelope,
  visibleRange,
} from "./decimate";
import type { RowType } from "./rows";
import { NO_LABEL, TONE, type RawLane, type RowEvents, type Tile } from "./tile";

export const MODE = { none: 0, logic: 1, density: 2, intervals: 3, points: 4, values: 5, header: 6, bus: 7, state: 8 };
export const FLAG = { start: 1, end: 2, mixed: 4, mark: 8 };
/** パレット: 0..11 = 段の色、12 = good、13 = bad、14 = warn、15 = 控えめ（layout.ts の PALETTE） */
export const TONE_COLOR: Record<number, number> = {
  [TONE.running]: 12, [TONE.failure]: 13, [TONE.stop]: 14, [TONE.mark]: 14, [TONE.other]: 14 };
export const BOX_LABEL_PX = 24;       // 区間・箱にラベルを付ける最小の幅（デバイスの画素）
export const POINT_LABEL_PX = 48;     // 印にラベルを付ける、次の印までの最小の間（デバイスの画素）
const MAX_LABELS = 4000;

export interface RowSpec {
  id: string;
  type: RowType;
  color: number;                      // パレットの番号（0..11）
}

export interface FrameLabel {
  row: number;                        // 描く段の番号
  x0: number;                         // 画素（デバイス）。印は x0 = x1
  x1: number;
  text: string;
  tone: number;
}

export interface TimelineFrame {
  seq: number;
  v0: number;
  v1: number;
  width: number;
  rows: number;
  ids: string[];                      // 描く段の名前（RowSpec の並び）
  data: Uint8Array;                   // rows × width × 4（RGBA）
  modes: Uint8Array;                  // 段ごとの描き方（MODE）
  visible: number[];                  // 段ごとの見えている件数（波形は変化のある画素の数）
  max: number[];                      // density の段の画素の件数の最大（棒の目盛り）
  scales: ([number, number] | null)[];  // values の段の目盛り [下, 上]
  labels: FrameLabel[];
  unknown: UnknownSpan[];             // 始めの値を探していない所（状態・値の段。無ければ空）
  decimateMs: number;                 // 間引きにかかった時間（Worker の中）
}

/** 状態・値の段で、範囲の始めの値を探していない所（レーンの carry_complete = false）: 見えている始め（記録の始めより
 *  前は除く）から段の最初の出来事まで。探した始めは carryFrom（それより前は探していない。無いのではない）。
 *  画面は判断しない: API の carry_complete・carry_from と出来事の時刻をそのまま画素にしたもの */
export interface UnknownSpan {
  id: string;                         // 段の名前
  x0: number;                         // 画素（デバイス）[x0, x1)
  x1: number;
  carryFrom: number | null;
}

/** フレームの段 id の画素 x（デバイス）が、始めの値を探していない所なら、その区間（ホバーの説明に） */
export function unknownAt(frame: TimelineFrame, id: string, x: number): UnknownSpan | null {
  return frame.unknown.find((u) => u.id === id && u.x0 <= x && x < u.x1) ?? null;
}

/** 視点 [v0, v1)（整数）を width 画素に。ranges は値の段の目盛り（無ければタイルの値の範囲）。seq は要求の番号 */
export function composeFrame(tile: Tile, v0: number, v1: number, width: number, specs: RowSpec[], seq = 0,
                             ranges?: Map<string, [number, number]>): TimelineFrame {
  const started = performance.now();
  const px = new Pixels(v0, v1, width);
  const n = specs.length, W = width;
  const data = new Uint8Array(n * W * 4), modes = new Uint8Array(n);
  const visible = new Array<number>(n).fill(0), max = new Array<number>(n).fill(0);
  const scales: ([number, number] | null)[] = new Array(n).fill(null);
  const labels: FrameLabel[] = [];
  const unknown: UnknownSpan[] = [];
  const counts = new Uint32Array(W), low = new Float64Array(W), high = new Float64Array(W);
  // 波形はまとめて 1 回（チャンネル × 画素の状態）
  const wanted = specs.some((s) => s.type === "logic");
  const waveRows = tile.wave.channels.length;
  const wave = wanted ? new Uint8Array(waveRows * W) : null;
  if (wave) decimateWave(tile, px, wave);
  const channelRow = new Map(tile.wave.channels.map((ch, r) => [ch, r]));

  const density = (r: number, mode: number) => {
    let top = 1, total = 0;
    for (let p = 0; p < W; p++) {
      total += counts[p];
      if (counts[p] > top) top = counts[p];
    }
    const scale = 255 / Math.log2(1 + top);
    const base = r * W * 4;
    for (let p = 0; p < W; p++) if (counts[p]) data[base + p * 4] = Math.max(1, Math.round(Math.log2(1 + counts[p]) * scale));
    modes[r] = mode;
    max[r] = top;
    visible[r] = total;
  };

  specs.forEach((spec, r) => {
    if (spec.type === "logic") {
      const row = channelRow.get(Number(spec.id.slice(5)));
      modes[r] = MODE.logic;
      const base = r * W * 4;
      if (row === undefined || !wave) {
        for (let p = 0; p < W; p++) data[base + p * 4] = 3;
        return;
      }
      let changes = 0;
      for (let p = 0; p < W; p++) {
        const s = wave[row * W + p];
        data[base + p * 4] = s;
        if (s === 2) changes++;
      }
      visible[r] = changes;
      return;
    }
    if (spec.type === "group") {
      const lane = tile.lanes.find((l) => `group:${l.stream}` === spec.id);
      if (!lane) {
        modes[r] = MODE.header;
        return;
      }
      laneCounts(tile, lane, px, counts);
      density(r, MODE.header);
      return;
    }
    const at = tile.rowAt.get(spec.id);
    if (!at) {
      modes[r] = spec.type === "values" ? MODE.values : MODE.none;
      if (spec.type === "values") scales[r] = ranges?.get(spec.id) ?? null;
      return;
    }
    const lane = tile.lanes[at.lane];
    if (lane.mode === "summary") {
      if (spec.type === "values") {
        summaryEnvelope(lane, at.row, px, low, high);
        scales[r] = ranges?.get(spec.id) ?? summaryRange(lane, at.row);
        values(data, r, W, low, high, scales[r]!, spec.color, modes);
        summaryRowCounts(lane, at.row, px, counts);
        visible[r] = counts.reduce((a, b) => a + b, 0);
        return;
      }
      summaryRowCounts(lane, at.row, px, counts);
      density(r, MODE.density);
      return;
    }
    const ev = lane.byRow[at.row];
    if ((spec.type === "state" || spec.type === "values") && !lane.carryComplete) {
      const span = unknownSpan(tile, ev, px, spec.id, lane.carryFrom);
      if (span) unknown.push(span);
    }
    if (spec.type === "values") {
      valueEnvelope(tile, ev, px, low, high);
      scales[r] = ranges?.get(spec.id) ?? [ev.lo, ev.hi];
      values(data, r, W, low, high, scales[r]!, spec.color, modes);
      const [i0, i1] = visibleRange(ev, v0, v1);
      visible[r] = i1 - i0;
      return;
    }
    let [i0, i1] = visibleRange(ev, v0, v1);
    visible[r] = i1 - i0;
    if (spec.type === "state" && i0 > 0 && (i0 === ev.t.length || ev.t[i0] > v0)) i0--;   // 視点の始めの状態
    if (i1 - i0 > 2 * W) {
      countsOf(tile, ev.t, px, counts);
      density(r, MODE.density);
      visible[r] = i1 - i0;
      return;
    }
    if (spec.type === "points") points(lane, ev, i0, i1, px, r, data, spec.color, labels);
    else boxes(tile, lane, ev, i0, i1, px, r, data, spec, labels);
    modes[r] = spec.type === "points" ? MODE.points : spec.type === "bus" ? MODE.bus
      : spec.type === "state" ? MODE.state : MODE.intervals;
  });
  return { seq, v0, v1, width, rows: n, ids: specs.map((s) => s.id), data, modes, visible, max, scales, labels, unknown,
           decimateMs: performance.now() - started };
}

/** 段の始めの値を探していない所: [見えている始め（記録の始めより前は除く）, 段の最初の出来事（無ければ記録の終わり）) */
function unknownSpan(tile: Tile, ev: RowEvents, px: Pixels, id: string, carryFrom: number | null): UnknownSpan | null {
  const [start, end] = tile.header.extent ?? [tile.t0, tile.t1];
  const a = Math.max(px.v0, start), b = Math.min(px.v1, ev.t.length ? ev.t[0] : end, end);
  if (!(b > a)) return null;
  const k = px.width / px.span;
  const x0 = Math.max(0, Math.floor((a - px.v0) * k)), x1 = Math.min(px.width, Math.max(x0 + 1, Math.ceil((b - px.v0) * k)));
  return { id, x0, x1, carryFrom };
}

function colorOf(lane: RawLane, i: number, row: number): number {
  return TONE_COLOR[lane.tone[i]] ?? row;
}

/** 区間・箱・状態: 画素を塗る（後に始まるもの = 入れ子の内側を上に）。状態は次の変化まで（最後はデータの終わりまで） */
function boxes(tile: Tile, lane: RawLane, ev: RowEvents, i0: number, i1: number, px: Pixels, r: number,
               data: Uint8Array, spec: RowSpec, labels: FrameLabel[]): void {
  const W = px.width, k = W / px.span, base = r * W * 4;
  const state = spec.type === "state";
  const end = tile.header.extent?.[1] ?? tile.t1;
  let labelled = -Infinity;                                       // ラベルを重ねない（入れ子の内側は外側に任せる）
  for (let j = i0; j < i1; j++) {
    const i = ev.index[j];
    const t = ev.t[j], tEnd = state ? (j + 1 < ev.t.length ? ev.t[j + 1] : end) : ev.tEnd[j];
    if (tEnd < px.v0 && t < px.v0) continue;
    const a = (t - px.v0) * k, b = (tEnd - px.v0) * k;
    const x0 = Math.max(0, Math.floor(a)), x1 = Math.min(W, Math.max(x0 + 1, Math.ceil(b)));
    if (x0 >= W) continue;
    const color = colorOf(lane, i, spec.color) + 1, depth = lane.depth[i];
    for (let x = x0; x < x1; x++) {
      const o = base + x * 4;
      const under = data[o];
      if (under && under !== color && x === x0 && depth <= data[o + 2]) data[o + 1] |= 4;     // 1 画素に 2 つの区間
      data[o + 3] = depth > 0 ? under || data[o + 3] : 0;                                // 入れ子の外側の色
      data[o] = color;
      data[o + 2] = depth;
    }
    if (a >= 0) data[base + x0 * 4 + 1] |= 1;
    if (b <= W) data[base + (x1 - 1) * 4 + 1] |= 2;
    if (labels.length < MAX_LABELS && Math.min(b, W) - Math.max(a, 0) >= BOX_LABEL_PX && lane.label[i] !== NO_LABEL
        && Math.max(a, 0) >= labelled) {
      labels.push({ row: r, x0: Math.max(a, 0), x1: Math.min(b, W), text: lane.labels[lane.label[i]], tone: lane.tone[i] });
      labelled = Math.min(b, W);
    }
  }
}

/** 印: 画素に印（重なれば後のもの）。次の印まで POINT_LABEL_PX 以上あればラベル */
function points(lane: RawLane, ev: RowEvents, i0: number, i1: number, px: Pixels, r: number, data: Uint8Array,
               color: number, labels: FrameLabel[]): void {
  const W = px.width, k = W / px.span, base = r * W * 4;
  for (let j = i0; j < i1; j++) {
    const t = ev.t[j];
    if (t < px.v0) continue;
    const i = ev.index[j];
    const x = Math.min(W - 1, Math.floor((t - px.v0) * k));
    const o = base + x * 4;
    if (data[o + 1] & FLAG.mark) data[o + 1] |= FLAG.mixed;
    const c = colorOf(lane, i, color) + 1;
    if (!data[o] || c - 1 !== color) data[o] = c;               // 色の意味（誤り等）のある印を優先
    data[o + 1] |= FLAG.mark;
    const next = j + 1 < i1 ? (ev.t[j + 1] - px.v0) * k : Infinity;
    if (labels.length < MAX_LABELS && next - (t - px.v0) * k >= POINT_LABEL_PX && lane.label[i] !== NO_LABEL) {
      labels.push({ row: r, x0: x, x1: x, text: lane.labels[lane.label[i]], tone: lane.tone[i] });
    }
  }
}

/** 値の範囲 → 段の中の高さ（0..255）。目盛りが 1 点なら真ん中 */
function values(data: Uint8Array, r: number, W: number, low: Float64Array, high: Float64Array, scale: [number, number],
                color: number, modes: Uint8Array): void {
  const [lo, hi] = scale, base = r * W * 4;
  const y = (v: number) => (hi > lo ? Math.max(0, Math.min(255, Math.round(((v - lo) / (hi - lo)) * 255))) : 128);
  for (let p = 0; p < W; p++) {
    if (!(low[p] <= high[p])) continue;
    const o = base + p * 4;
    data[o] = color + 1;
    data[o + 2] = y(low[p]);
    data[o + 3] = y(high[p]);
  }
  modes[r] = MODE.values;
}

function summaryRange(lane: { rowMin: Float64Array | null; rowMax: Float64Array | null; bins: number },
                      row: number): [number, number] {
  let lo = Infinity, hi = -Infinity;
  if (lane.rowMin && lane.rowMax) {
    for (let i = row * lane.bins; i < (row + 1) * lane.bins; i++) {
      if (lane.rowMin[i] < lo) lo = lane.rowMin[i];
      if (lane.rowMax[i] > hi) hi = lane.rowMax[i];
    }
  }
  return lo <= hi ? [lo, hi] : [NaN, NaN];
}

/** タイルの値の段の範囲（Worker が段ごとに覚え、タイルが変わっても目盛りが跳ねないようにする） */
export function valueRanges(tile: Tile): Map<string, [number, number]> {
  const out = new Map<string, [number, number]>();
  for (const lane of tile.lanes) {
    lane.rows.forEach((row, k) => {
      if (row.type !== "values") return;
      const range = lane.mode === "raw" ? [lane.byRow[k].lo, lane.byRow[k].hi] as [number, number] : summaryRange(lane, k);
      if (range[0] <= range[1]) out.set(row.id, range);
    });
  }
  return out;
}
