// 間引きの置き場所の抽象: ブラウザでは Web Worker（WorkerDecimator）、Worker の無い環境（試験）では同じ関数を
// その場で呼ぶ（InlineDecimator）。タイルのバッファは Worker へ transfer する（画面のスレッドには見出しだけ残る）。
// 間引き（frame）のほか、タイルへの問い合わせ（pick・seek・anomalies）も同じ所で答える。
import { composeFrame, valueRanges, type RowSpec, type TimelineFrame } from "./compose";
import { anomalies, pick, seek, type PickedItem } from "./query";
import { buildTile, type Tile, type TileHeader } from "./tile";

export interface FrameRequest { tile: number; v0: number; v1: number; width: number; rows: RowSpec[] }
export interface Anomaly { t: number; tone: number }

export type WorkerRequest =
  | { type: "tile"; id: number; header: TileHeader; buffer: ArrayBuffer }
  | { type: "drop"; id: number }
  | { type: "reset" }
  | ({ type: "frame"; seq: number } & FrameRequest)
  | { type: "pick"; seq: number; tiles: number[]; row: string; at: number; tolerance: number }
  | { type: "seek"; seq: number; tiles: number[]; rows: string[]; from: number; dir: 1 | -1 }
  | { type: "anomalies"; seq: number; tile: number };

export type WorkerReply = { seq: number; result: unknown };

export interface Decimator {
  readonly kind: "worker" | "inline";
  /** タイルを渡す（buffer は transfer されて使えなくなる） */
  addTile(id: number, header: TileHeader, buffer: ArrayBuffer): void;
  dropTile(id: number): void;
  /** すべてのタイルと覚えた値の目盛りを捨てる（源・記録が変わった） */
  reset(): void;
  /** タイル id で視点 [v0, v1) を width 画素に。タイルが無ければ null */
  frame(r: FrameRequest): Promise<TimelineFrame | null>;
  /** 段 row の時刻 at の出来事（tiles を順に見て、最初に見つかったもの） */
  pick(tiles: number[], row: string, at: number, tolerance: number): Promise<PickedItem | null>;
  /** 段 rows の、from の次（dir = 1）/ 前（dir = −1）の出来事の時刻 */
  seek(tiles: number[], rows: string[], from: number, dir: 1 | -1): Promise<{ t: number; row: string } | null>;
  /** 縮図の異常の印 */
  anomalies(tile: number): Promise<Anomaly[]>;
  dispose(): void;
}

/** タイルと問い合わせの中身（Worker とその場で同じもの） */
export class TileStore {
  private tiles = new Map<number, Tile>();
  private ranges = new Map<string, [number, number]>();    // 値の段の目盛り（見たタイルの和。跳ねないように）

  add(id: number, header: TileHeader, buffer: ArrayBuffer): void {
    const tile = buildTile(header, buffer);
    this.tiles.set(id, tile);
    for (const [row, [lo, hi]] of valueRanges(tile)) {
      const have = this.ranges.get(row);
      this.ranges.set(row, have ? [Math.min(have[0], lo), Math.max(have[1], hi)] : [lo, hi]);
    }
  }

  drop(id: number): void {
    this.tiles.delete(id);
  }

  reset(): void {
    this.tiles.clear();
    this.ranges.clear();
  }

  frame(r: FrameRequest, seq: number): TimelineFrame | null {
    const tile = this.tiles.get(r.tile);
    return tile ? composeFrame(tile, r.v0, r.v1, r.width, r.rows, seq, this.ranges) : null;
  }

  pick(tiles: number[], row: string, at: number, tolerance: number): PickedItem | null {
    for (const id of tiles) {
      const tile = this.tiles.get(id);
      const found = tile && at >= tile.t0 && at < tile.t1 ? pick(tile, row, at, tolerance) : null;
      if (found) return found;
    }
    return null;
  }

  seek(tiles: number[], rows: string[], from: number, dir: 1 | -1): { t: number; row: string } | null {
    for (const id of tiles) {
      const tile = this.tiles.get(id);
      const found = tile ? seek(tile, rows, from, dir) : null;
      if (found) return found;
    }
    return null;
  }

  anomalies(id: number): Anomaly[] {
    const tile = this.tiles.get(id);
    return tile ? anomalies(tile) : [];
  }

  /** 要求に答える（Worker の onmessage とその場の実装が使う） */
  answer(m: WorkerRequest): unknown {
    switch (m.type) {
      case "tile": return this.add(m.id, m.header, m.buffer);
      case "drop": return this.drop(m.id);
      case "reset": return this.reset();
      case "frame": return this.frame(m, m.seq);
      case "pick": return this.pick(m.tiles, m.row, m.at, m.tolerance);
      case "seek": return this.seek(m.tiles, m.rows, m.from, m.dir);
      case "anomalies": return this.anomalies(m.tile);
    }
  }
}

export class InlineDecimator implements Decimator {
  readonly kind = "inline";
  private store = new TileStore();
  private seq = 0;

  addTile(id: number, header: TileHeader, buffer: ArrayBuffer): void {
    this.store.add(id, header, buffer);
  }

  dropTile(id: number): void {
    this.store.drop(id);
  }

  reset(): void {
    this.store.reset();
  }

  async frame(r: FrameRequest): Promise<TimelineFrame | null> {
    return this.store.frame(r, ++this.seq);
  }

  async pick(tiles: number[], row: string, at: number, tolerance: number): Promise<PickedItem | null> {
    return this.store.pick(tiles, row, at, tolerance);
  }

  async seek(tiles: number[], rows: string[], from: number, dir: 1 | -1) {
    return this.store.seek(tiles, rows, from, dir);
  }

  async anomalies(tile: number): Promise<Anomaly[]> {
    return this.store.anomalies(tile);
  }

  dispose(): void {
    this.store.reset();
  }
}

type Asked = { type: "frame" | "pick" | "seek" | "anomalies" };

export class WorkerDecimator implements Decimator {
  readonly kind = "worker";
  private seq = 0;
  private waiting = new Map<number, (result: any) => void>();

  constructor(private worker: Worker) {
    worker.onmessage = (e: MessageEvent<WorkerReply>) => {
      const done = this.waiting.get(e.data.seq);
      this.waiting.delete(e.data.seq);
      done?.(e.data.result);
    };
  }

  addTile(id: number, header: TileHeader, buffer: ArrayBuffer): void {
    this.worker.postMessage({ type: "tile", id, header, buffer } satisfies WorkerRequest, [buffer]);
  }

  dropTile(id: number): void {
    this.worker.postMessage({ type: "drop", id } satisfies WorkerRequest);
  }

  reset(): void {
    this.worker.postMessage({ type: "reset" } satisfies WorkerRequest);
  }

  frame(r: FrameRequest): Promise<TimelineFrame | null> {
    return this.ask({ type: "frame", ...r });
  }

  pick(tiles: number[], row: string, at: number, tolerance: number): Promise<PickedItem | null> {
    return this.ask({ type: "pick", tiles, row, at, tolerance });
  }

  seek(tiles: number[], rows: string[], from: number, dir: 1 | -1): Promise<{ t: number; row: string } | null> {
    return this.ask({ type: "seek", tiles, rows, from, dir });
  }

  anomalies(tile: number): Promise<Anomaly[]> {
    return this.ask({ type: "anomalies", tile });
  }

  dispose(): void {
    this.worker.terminate();
    this.waiting.forEach((done) => done(null));
    this.waiting.clear();
  }

  private ask<T>(m: Asked & Record<string, unknown>): Promise<T> {
    const seq = ++this.seq;
    return new Promise((resolve) => {
      this.waiting.set(seq, resolve);
      this.worker.postMessage({ ...m, seq });
    });
  }
}

/** ブラウザなら Worker、無ければその場で */
export function createDecimator(): Decimator {
  if (typeof Worker === "undefined") return new InlineDecimator();
  return new WorkerDecimator(new Worker(new URL("./worker.ts", import.meta.url), { type: "module" }));
}
