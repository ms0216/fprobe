// 時間軸のマウスの縦線の値（UI/UX 調査のカーソル 3 種の 2 つ目「マウス位置の縦線（全段の値）」）: 段の名前の列に、
// その時刻に保たれている値（値の段）・状態（状態の段）を出す。波形の段はフレームから（waveform.ts の levelAt）、
// それ以外はタイルへの問い合わせ（Worker の pick）で。Svelte・DOM に依らない。
import { plainNumber } from "../format";
import type { PickedItem } from "./query";
import type { VisibleRow } from "./rows";

/** 縦線の値を問い合わせる段（値・状態。見出し・波形・区間・箱・印は「その時刻の値」を持たない） */
export function cursorRows(rows: VisibleRow[]): string[] {
  return rows.filter((r) => !r.header && (r.def.type === "values" || r.def.type === "state")).map((r) => r.def.id);
}

/** 段の名前の列に出す文字（値は普通の表記と単位、状態はその名前）。無ければ空 */
export function cursorText(item: PickedItem | null | undefined): string {
  if (!item) return "";
  if (item.type === "state" && item.label) return item.label;
  if (item.value === null) return item.label ?? "";
  return `${plainNumber(item.value)}${item.unit ? ` ${item.unit}` : ""}`;
}

export type CursorValues = Record<string, PickedItem>;

/** 縦線の値の問い合わせ: 同時に 1 つ。問い合わせの間に動いたら、終わってから最後の位置で 1 回だけ（途中の位置は
 *  問い合わせない）。at = null（縦線が外れた）は問い合わせずに空を配る */
export class CursorProbe {
  private busy = false;
  private pending: { at: number | null } | null = null;

  constructor(private query: (at: number) => Promise<CursorValues>, private onResult: (v: CursorValues) => void) {}

  ask(at: number | null): void {
    if (this.busy) {
      this.pending = { at };
      return;
    }
    void this.run(at);
  }

  private async run(at: number | null): Promise<void> {
    this.busy = true;
    try {
      let next: { at: number | null } | null = { at };
      while (next) {
        this.pending = null;
        let result: CursorValues = {};
        if (next.at !== null) {
          try {
            result = await this.query(next.at);
          } catch {
            result = {};
          }
        }
        if (!this.pending) this.onResult(result);
        next = this.pending;
      }
    } finally {
      this.busy = false;
    }
  }
}
