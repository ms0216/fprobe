// タイル（timeline.tiles の結果）の形と、区画から組み立てた扱いやすい形。Svelte・DOM に依らない（Worker でも使う）。
// 仕様: docs/superpowers/specs/2026-10-18-timeline-tiles-design.md §2
import { sectionsOf, type Section, type SectionInfo } from "./frame";

export const ST_LOW = 0;
export const ST_HIGH = 1;
export const ST_CHANGE = 2;          // 画素の中で変化
export const ST_NONE = 3;            // 波形が無い（記録の隙間・タイルの外）

export const NO_LABEL = 0xffffffff;
export const TONE = { normal: 0, bad: 1, good: 2, warn: 3 } as const;

/** 出来事の段の型（fprobe.timeline_rows の ROW_TYPES） */
export type EventRowType = "intervals" | "bus" | "state" | "points" | "values";

/** デーモンが返す段の見出し（fprobe.timeline_rows.Row.to_dict） */
export interface RowHeader {
  id: string;
  rule: string;
  type: EventRowType;
  title: string | null;
  unit?: string;
  jump?: "memory" | "source";
  hidden?: boolean;
}

export interface PieceHeader {
  t0: number;
  t1: number;
  tps: number;                       // 1 サンプルの単位数（捕捉は 1）
  length: number;
  s0: number;
  s1: number;
  channels: number[];
  levels: number[];
}

export interface LaneHeader {
  stream: string;
  mode: "raw" | "summary";
  count: number;
  kinds?: string[];
  rows?: RowHeader[];                // format 2（無ければ stream 全体を 1 つの区間の段とみなす）
  labels?: string[];
  start?: number;
  bin?: number;
  bins?: number;
}

export interface TileHeader {
  format: number;
  source: "capture" | "recording";
  t0: number;
  t1: number;
  tick_s: number;
  extent: [number, number] | null;
  capture?: Record<string, any>;
  wave: { mode: "raw" | "summary" | "none"; channels: number[]; names?: string[]; pieces?: PieceHeader[];
          start?: number; bin?: number; bins?: number };
  lanes: LaneHeader[];
  sections: Record<string, SectionInfo>;
}

export interface Piece extends PieceHeader {
  edges: Float64Array[];             // channels と同じ並び
}

export type WaveData =
  | { mode: "none"; channels: number[] }
  | { mode: "raw"; channels: number[]; pieces: Piece[] }
  | { mode: "summary"; channels: number[]; start: number; bin: number; bins: number; states: Uint8Array };

/** 生の段の 1 つの段（行）の出来事: 段の中の番号の順（t の昇順）に、レーンの出来事の番号と t・t_end */
export interface RowEvents {
  index: Uint32Array;                // レーンの出来事の番号
  t: Float64Array;
  tEnd: Float64Array;
  value: Float64Array;               // 値（values の段。無ければ NaN）
  maxDuration: number;
  lo: number;                        // 値の最小・最大（values の段。値が無ければ NaN）
  hi: number;
}

export interface RawLane {
  stream: string;
  mode: "raw";
  count: number;
  t: Float64Array;
  tEnd: Float64Array;
  kind: Uint16Array;
  kinds: string[];
  maxDuration: number;
  rows: RowHeader[];
  labels: string[];
  row: Uint16Array;
  label: Uint32Array;
  value: Float64Array;
  depth: Uint8Array;
  tone: Uint8Array;
  address: Float64Array;
  byRow: RowEvents[];
}

export interface SummaryLane {
  stream: string;
  mode: "summary";
  count: number;
  start: number;
  bin: number;
  bins: number;
  counts: Uint32Array;
  rows: RowHeader[];
  rowCounts: Uint32Array;            // 段 × 区間
  rowMin: Float64Array | null;       // values の段の区間の最小・最大（無ければ NaN）
  rowMax: Float64Array | null;
}

export type LaneData = RawLane | SummaryLane;

export interface Tile {
  header: TileHeader;
  t0: number;
  t1: number;
  wave: WaveData;
  lanes: LaneData[];
  rowAt: Map<string, { lane: number; row: number }>;    // 段の名前 → レーンと段の番号
}

/** 結果の JSON と区画の載ったバッファからタイルを組み立てる（複写しない） */
export function buildTile(header: TileHeader, buffer: ArrayBuffer): Tile {
  return fromSections(header, sectionsOf(buffer, header.sections ?? {}));
}

/** format 1 のタイル（段の規則が無い）: stream 全体を 1 つの区間の段に */
function wholeStream(stream: string): RowHeader[] {
  return [{ id: stream, rule: "all", type: "intervals", title: stream }];
}

export function fromSections(header: TileHeader, data: Record<string, Section>): Tile {
  const get = <T extends Section>(name: string) => {
    const s = data[name];
    if (!s) throw new Error(`タイルに区画 ${name} が無い`);
    return s as T;
  };
  const opt = <T extends Section>(name: string) => data[name] as T | undefined;
  const w = header.wave;
  let wave: WaveData;
  if (w.mode === "raw") {
    wave = { mode: "raw", channels: w.channels, pieces: (w.pieces ?? []).map((p, i) => ({
      ...p, edges: p.channels.map((ch) => get<Float64Array>(`wave.${i}.${ch}`)),
    })) };
  } else if (w.mode === "summary") {
    wave = { mode: "summary", channels: w.channels, start: w.start!, bin: w.bin!, bins: w.bins!,
             states: get<Uint8Array>("wave.states") };
  } else {
    wave = { mode: "none", channels: [] };
  }
  const rowAt = new Map<string, { lane: number; row: number }>();
  const lanes: LaneData[] = header.lanes.map((l, i) => {
    const rows = l.rows ?? wholeStream(l.stream);
    rows.forEach((r, k) => rowAt.set(r.id, { lane: i, row: k }));
    if (l.mode === "summary") {
      const counts = get<Uint32Array>(`lane.${i}.counts`);
      return { stream: l.stream, mode: "summary", count: l.count, start: l.start!, bin: l.bin!, bins: l.bins!, counts,
               rows, rowCounts: opt<Uint32Array>(`lane.${i}.row_counts`) ?? counts,
               rowMin: opt<Float64Array>(`lane.${i}.row_min`) ?? null,
               rowMax: opt<Float64Array>(`lane.${i}.row_max`) ?? null } satisfies SummaryLane;
    }
    const t = get<Float64Array>(`lane.${i}.t`), tEnd = get<Float64Array>(`lane.${i}.t_end`);
    const n = t.length;
    let maxDuration = 0;
    for (let k = 0; k < n; k++) maxDuration = Math.max(maxDuration, tEnd[k] - t[k]);
    const row = opt<Uint16Array>(`lane.${i}.row`) ?? new Uint16Array(n);
    const value = opt<Float64Array>(`lane.${i}.value`) ?? new Float64Array(n).fill(NaN);
    return { stream: l.stream, mode: "raw", count: l.count, t, tEnd, kind: get<Uint16Array>(`lane.${i}.kind`),
             kinds: l.kinds ?? [], maxDuration, rows, labels: l.labels ?? [], row,
             label: opt<Uint32Array>(`lane.${i}.label`) ?? new Uint32Array(n).fill(NO_LABEL), value,
             depth: opt<Uint8Array>(`lane.${i}.depth`) ?? new Uint8Array(n),
             tone: opt<Uint8Array>(`lane.${i}.tone`) ?? new Uint8Array(n),
             address: opt<Float64Array>(`lane.${i}.address`) ?? new Float64Array(n).fill(NaN),
             byRow: splitRows(rows.length, row, t, tEnd, value) } satisfies RawLane;
  });
  return { header, t0: header.t0, t1: header.t1, wave, lanes, rowAt };
}

/** レーンの出来事（t の昇順）を段ごとに分ける（段の中も t の昇順） */
function splitRows(rows: number, row: Uint16Array, t: Float64Array, tEnd: Float64Array, value: Float64Array): RowEvents[] {
  const sizes = new Uint32Array(rows);
  for (let k = 0; k < row.length; k++) sizes[row[k]]++;
  const out: RowEvents[] = Array.from({ length: rows }, (_, r) => ({
    index: new Uint32Array(sizes[r]), t: new Float64Array(sizes[r]), tEnd: new Float64Array(sizes[r]),
    value: new Float64Array(sizes[r]), maxDuration: 0, lo: NaN, hi: NaN,
  }));
  const fill = new Uint32Array(rows);
  for (let k = 0; k < row.length; k++) {
    const r = out[row[k]], j = fill[row[k]]++;
    r.index[j] = k;
    r.t[j] = t[k];
    r.tEnd[j] = tEnd[k];
    if (tEnd[k] - t[k] > r.maxDuration) r.maxDuration = tEnd[k] - t[k];
    const v = (r.value[j] = value[k]);
    if (v === v) {
      if (!(v >= r.lo)) r.lo = v;
      if (!(v <= r.hi)) r.hi = v;
    }
  }
  return out;
}

/** タイル（見出しだけで判断）が視点 [v0, v1) を、1 画素 pixelTicks の細かさで描けるか（生データなら細かさは問わない） */
export function tileServes(header: TileHeader, v0: number, v1: number, pixelTicks: number): boolean {
  if (header.t0 > v0 || header.t1 < v1) return false;
  const fine = (bin: number | undefined) => (bin ?? Infinity) <= Math.max(1, pixelTicks);
  if (header.wave.mode === "summary" && !fine(header.wave.bin)) return false;
  return header.lanes.every((l) => l.mode === "raw" || fine(l.bin));
}

/** 視点と重なる長さ（描けるタイルが無いときに、いちばん多く描けるものを選ぶ） */
export function overlapOf(header: TileHeader, v0: number, v1: number): number {
  return Math.max(0, Math.min(header.t1, v1) - Math.max(header.t0, v0));
}
