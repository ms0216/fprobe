// タイルへの問い合わせ（Worker の中）: 時刻の出来事を拾う（ホバーの説明・クリックで選ぶ）、次 / 前の出来事を探す
// （N / P キー）。Svelte・DOM に依らない。
import { lowerBound, upperBound } from "./decimate";
import { NO_LABEL, type RowHeader, type Tile } from "./tile";

/** 拾った出来事（区間・箱・状態・印は 1 件、値の段はその時刻に保たれている値） */
export interface PickedItem {
  row: string;
  type: RowHeader["type"];
  stream: string;
  kind: string;
  t: number;
  tEnd: number;                        // 終わりの無い出来事は t
  label: string | null;
  value: number | null;
  unit: string | null;
  tone: number;
  depth: number;
  address: number | null;              // ダブルクリックの飛び先の番地
  jump: "memory" | "source" | null;
}

/** 段 row の時刻 at の出来事。区間は at を含むもの（深いものを優先）、無ければ ±tolerance の中でいちばん近いもの */
export function pick(tile: Tile, row: string, at: number, tolerance: number): PickedItem | null {
  const loc = tile.rowAt.get(row);
  if (!loc) return null;
  const lane = tile.lanes[loc.lane];
  if (lane.mode !== "raw") return null;
  const header = lane.rows[loc.row], ev = lane.byRow[loc.row];
  const item = (j: number, tEnd = ev.tEnd[j]): PickedItem => {
    const i = ev.index[j];
    return { row, type: header.type, stream: lane.stream, kind: lane.kinds[lane.kind[i]] ?? "", t: ev.t[j], tEnd,
             label: lane.label[i] === NO_LABEL ? null : lane.labels[lane.label[i]],
             value: Number.isNaN(lane.value[i]) ? null : lane.value[i], unit: header.unit ?? null, tone: lane.tone[i],
             depth: lane.depth[i], address: Number.isNaN(lane.address[i]) ? null : lane.address[i],
             jump: header.jump ?? null };
  };
  const n = ev.t.length;
  if (!n) return null;
  if (header.type === "values" || header.type === "state") {        // その時刻に保たれている値・状態
    const j = upperBound(ev.t, at) - 1;
    if (j < 0) return null;
    const next = j + 1 < n ? ev.t[j + 1] : tile.header.extent?.[1] ?? tile.t1;
    return item(j, header.type === "state" ? next : ev.t[j]);
  }
  if (header.type !== "points") {
    let best = -1;
    for (let j = lowerBound(ev.t, at - ev.maxDuration); j < n && ev.t[j] <= at; j++) {
      if (ev.tEnd[j] > at || ev.tEnd[j] === ev.t[j] && ev.t[j] === at) {
        if (best < 0 || lane.depth[ev.index[j]] >= lane.depth[ev.index[best]]) best = j;
      }
    }
    if (best >= 0) return item(best);
  }
  const j = lowerBound(ev.t, at);
  let near = -1, gap = tolerance;
  for (const c of [j - 1, j]) {
    if (c < 0 || c >= n) continue;
    const d = Math.min(Math.abs(ev.t[c] - at), Math.abs(ev.tEnd[c] - at));
    if (d <= gap) {
      near = c;
      gap = d;
    }
  }
  return near >= 0 ? item(near) : null;
}

/** 段 rows の出来事の始めで、from より後（dir = 1）/ 前（dir = −1）のいちばん近いもの。無ければ null */
export function seek(tile: Tile, rows: string[], from: number, dir: 1 | -1): { t: number; row: string } | null {
  let best: { t: number; row: string } | null = null;
  for (const row of rows) {
    const loc = tile.rowAt.get(row);
    if (!loc) continue;
    const lane = tile.lanes[loc.lane];
    if (lane.mode !== "raw") continue;
    const t = lane.byRow[loc.row].t;
    const j = dir > 0 ? upperBound(t, from) : lowerBound(t, from) - 1;
    if (j < 0 || j >= t.length) continue;
    if (!best || (dir > 0 ? t[j] < best.t : t[j] > best.t)) best = { t: t[j], row };
  }
  return best;
}

/** 画面の縮図の異常の印: 色の意味が bad / warn の出来事の時刻（全部の段。多ければ間引く） */
export function anomalies(tile: Tile, limit = 2000): { t: number; tone: number }[] {
  const out: { t: number; tone: number }[] = [];
  for (const lane of tile.lanes) {
    if (lane.mode !== "raw") continue;
    for (let i = 0; i < lane.t.length; i++) {
      if (lane.tone[i] === 1 || lane.tone[i] === 3) out.push({ t: lane.t[i], tone: lane.tone[i] });
    }
  }
  out.sort((a, b) => a.t - b.t);
  if (out.length <= limit) return out;
  const step = out.length / limit;
  return Array.from({ length: limit }, (_, k) => out[Math.floor(k * step)]);
}
