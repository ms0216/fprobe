// 時間軸が持っているタイルの管理: 視点の前後のタイル（新しい順に MAX_TILES - 1 まで）と、全体のタイル（縮図・範囲の外の
// 問い合わせ用に持ち続ける）。描くのに足りるタイルを選ぶ・問い合わせに使うタイルの順を返す・間引き（Worker）へ渡す・捨てる。
// Svelte にも TimelineState にも依らない。
import type { View } from "../waveform";
import type { Decimator } from "./decimator";
import { overlapOf, tileServes, type TileHeader } from "./tile";

const MAX_TILES = 4;

export interface HeldTile { id: number; header: TileHeader }

export class TileCache {
  private tiles: HeldTile[] = [];
  whole: HeldTile | null = null;          // 全体のタイル（縮図・範囲の外の問い合わせ）
  private nextTile = 1;

  constructor(private decimator: Decimator) {}

  private all(): HeldTile[] {
    return [...this.tiles, ...(this.whole ? [this.whole] : [])];
  }

  /** 視点を足りる細かさで覆うタイル */
  serving(view: View, width: number): HeldTile | undefined {
    const pixel = (view.end - view.start) / Math.max(1, width);
    return this.all().find((t) => tileServes(t.header, view.start, view.end, pixel));
  }

  /** 描くのに使うタイル: 足りるもの、無ければ視点といちばん重なるもの */
  best(view: View, width: number): HeldTile | undefined {
    return this.serving(view, width) ?? this.all().reduce<HeldTile | undefined>(
      (a, t) => (!a || overlapOf(t.header, view.start, view.end) > overlapOf(a.header, view.start, view.end) ? t : a),
      undefined);
  }

  /** 問い合わせに使うタイルの順（視点を描いているもの、ほかの持っているもの、全体） */
  order(view: View, width: number): number[] {
    const ids = [this.best(view, width)?.id, ...this.tiles.map((t) => t.id), this.whole?.id]
      .filter((x): x is number => x !== undefined);
    return [...new Set(ids)];
  }

  /** デーモンの応答のタイルを間引き（Worker）へ渡して持つ。whole = 全体のタイル（前の全体は捨てる） */
  keep(result: any, whole: boolean): HeldTile {
    const { data, ...header } = result;
    const buffer = (Object.values(data ?? {})[0] as ArrayBufferView | undefined)?.buffer as ArrayBuffer | undefined;
    const id = this.nextTile++;
    this.decimator.addTile(id, header as TileHeader, buffer ?? new ArrayBuffer(0));
    const held = { id, header: header as TileHeader };
    if (whole) {
      if (this.whole) this.decimator.dropTile(this.whole.id);
      this.whole = held;
    } else {
      this.tiles.unshift(held);
      for (const old of this.tiles.splice(MAX_TILES - 1)) this.decimator.dropTile(old.id);
    }
    return held;
  }

  /** 視点のタイルを捨てる。all なら全体も捨てて間引きを初期化する */
  drop(all: boolean): void {
    for (const t of this.tiles) this.decimator.dropTile(t.id);
    this.tiles = [];
    if (all) {
      this.whole = null;
      this.decimator.reset();
    }
  }
}
