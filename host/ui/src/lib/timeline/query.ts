// タイルへの問い合わせ（Worker の中）: 時刻の出来事を拾う（ホバーの説明・クリックで選ぶ）、次 / 前の出来事を探す
// （N / P キー）。Svelte・DOM に依らない。
import { lowerBound, upperBound } from "./decimate";
import { NO_LABEL, TONE, type RowHeader, type Tile } from "./tile";

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

/** 画面の縮図の異常の印: 色の意味が bad / warn の出来事の時刻（全部の段）。タイルの範囲を bins 個の等しい時間の箱に
 *  分け、箱ごとに bad・warn のそれぞれ最初の 1 つを残す（箱は縮図の画素より細かくする: 縮図で見える「その辺りに誤り・
 *  注意がある」は欠けない。等間隔に間引いて離れた 1 つを落とさない） */
export function anomalies(tile: Tile, bins: number): { t: number; tone: number }[] {
  const span = Math.max(1, tile.t1 - tile.t0);
  const kept = new Map<number, { t: number; tone: number }>();          // 箱 × 2 + (warn ? 1 : 0)
  for (const lane of tile.lanes) {
    if (lane.mode !== "raw") continue;
    for (let i = 0; i < lane.t.length; i++) {
      // 失敗（failure）は 1、止める・印・その他（stop / mark / other）は 3 の色で縮図に出す（色の見せ方）
      const raw = lane.tone[i];
      const tone = raw === TONE.failure ? 1 : raw === TONE.stop || raw === TONE.mark || raw === TONE.other ? 3 : 0;
      if (tone === 0) continue;
      const t = lane.t[i];
      const box = Math.min(bins - 1, Math.max(0, Math.floor(((t - tile.t0) / span) * bins)));
      const key = box * 2 + (tone === 3 ? 1 : 0);
      const had = kept.get(key);
      if (!had || t < had.t) kept.set(key, { t, tone });
    }
  }
  return [...kept.values()].sort((a, b) => a.t - b.t);
}
