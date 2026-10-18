// 時間軸から他のパネルへの知らせ（画面の中の出来事。window の CustomEvent）。時間軸は知らせるだけで、受けるのは
// 各パネル（ソース・メモリ・逆アセンブル等）。UI/UX の調査の規則: 1 回クリック = 選ぶ（時刻をそろえる）、
// ダブルクリック = その出来事の元（ソース・メモリ）へ。
import type { PickedItem } from "./query";

/** 選択カーソルが動いた（クリック・次 / 前の出来事）。detail: TimelineSelectDetail */
export const TIMELINE_SELECT = "fprobe:timeline-select";
/** 出来事の元を開く（ダブルクリック・Enter）。detail: TimelineOpenDetail */
export const TIMELINE_OPEN = "fprobe:timeline-open";

export interface TimelineSelectDetail {
  t: number;                           // 時刻（捕捉はサンプル番号、記録はタイムベース）
  seconds: number;                     // 時刻 0（捕捉はトリガ、記録は始め）からの秒
  source: "capture" | "recording";
  item: PickedItem | null;
}

export interface TimelineOpenDetail {
  kind: "memory" | "source";
  address: number;                     // memory: 番地、source: PC
  t: number;
  item: PickedItem;
}

export function announceSelect(detail: TimelineSelectDetail): void {
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(TIMELINE_SELECT, { detail }));
}

/** 出来事に飛び先があれば知らせて true */
export function announceOpen(item: PickedItem | null, t: number): boolean {
  if (!item || item.address === null || !item.jump) return false;
  const detail: TimelineOpenDetail = { kind: item.jump, address: item.address, t, item };
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(TIMELINE_OPEN, { detail }));
  return true;
}

/** 時間軸の知らせを受ける（各パネル: 1 回クリック = select でその時刻の行を選ぶ、ダブルクリック = open で元を開く）。
 *  やめる関数を返す（Svelte の $effect からそのまま返せる） */
export function onTimeline(handlers: { select?: (d: TimelineSelectDetail) => void;
                                       open?: (d: TimelineOpenDetail) => void }): () => void {
  if (typeof window === "undefined") return () => {};
  const select = (e: Event) => handlers.select?.((e as CustomEvent<TimelineSelectDetail>).detail);
  const open = (e: Event) => handlers.open?.((e as CustomEvent<TimelineOpenDetail>).detail);
  if (handlers.select) window.addEventListener(TIMELINE_SELECT, select);
  if (handlers.open) window.addEventListener(TIMELINE_OPEN, open);
  return () => {
    window.removeEventListener(TIMELINE_SELECT, select);
    window.removeEventListener(TIMELINE_OPEN, open);
  };
}

/** 昇順の時刻の並び times で at 以下のいちばん後の位置（無ければ -1）。時間軸で選んだ時刻に近い行を選ぶのに使う */
export function nearestAtOrBefore(times: ArrayLike<number>, at: number): number {
  let lo = 0, hi = times.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (times[mid] <= at) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}
