// 波形の表示の共通の計算（視点の拡大縮小・移動、目盛り、捕捉の設定の選択肢）。Svelte に依らない。時刻の書き方は lib/time.ts。
// 状態（タイル・間引き）は lib/timeline/state.ts の TimelineState。
import { t } from "./i18n";
import { formatTick } from "./time";
import { ST_NONE } from "./timeline/tile";
import { MODE, type TimelineFrame } from "./timeline/compose";

export interface View {
  start: number;            // 整数（捕捉はサンプル番号、記録はタイムベース）
  end: number;
}

export interface CaptureInfo {
  id: number;
  length: number;
  sample_period_ns: number;
  trigger_index: number;
  channels: number;
  source: string;
}

export interface CaptureRequest {
  rate_hz: number | null;
  pretrigger: number;
  rise?: number;
  fall?: number;
  deep: boolean;
  beforeBeats?: number;      // 深いバッファで読むトリガ前・後の拍（1 拍 = RAW 8 サンプル / RLE 4 変化点）
  afterBeats?: number;
}

/** 拡大縮小: factor < 1 で拡大。anchor（0..1）の位置は動かない。範囲 extent の外へは出ない。最小 minSpan */
export function zoomView(v: View, factor: number, anchor: number, extent: [number, number], minSpan: number): View {
  const total = extent[1] - extent[0];
  const span = Math.min(total, Math.max(Math.min(minSpan, total), (v.end - v.start) * factor));
  const at = v.start + anchor * (v.end - v.start);
  const start = Math.min(Math.max(extent[0], at - anchor * span), extent[1] - span);
  return { start: Math.round(start), end: Math.round(start + span) };
}

/** 移動: delta だけずらす。端で止まる */
export function panView(v: View, delta: number, extent: [number, number]): View {
  const span = v.end - v.start;
  const start = Math.min(Math.max(extent[0], Math.round(v.start + delta)), extent[1] - span);
  return { start, end: start + span };
}

/** 速度の選択肢: ボードの最高速度（ケイパビリティ la_sample_hz）を整数で割った値（分周は整数なので） */
export function rateChoices(maxHz: number): number[] {
  if (!(maxHz > 0)) return [];
  return [1, 2, 4, 5, 10, 20, 50, 100, 200, 500, 1000].map((n) => maxHz / n);
}

/** ホイールの量に比例した拡大率（トラックパッドの細かい量では少しだけ。横向きのスクロール = 0 では変えない） */
export function wheelFactor(deltaY: number): number {
  return Math.exp(deltaY * 0.002);
}

/** ロジアナの ch 1 本（probe.info の capabilities.la_layout の要素。U22）。kind: pin = 拡張ヘッダのピン、
 *  internal = 内部の信号（0 = SWDIO 駆動、1 = DAP 処理中）、input = 専用の入力、none = 無し */
export interface LaChannel {
  channel: number;
  kind: string;
  index: number;
}

/** 申告の無い古いデバイス（U22 の前）の並び: ch 0〜7 = ピン、8・9 = 内部 */
function legacyLayout(): LaChannel[] {
  return Array.from({ length: 10 }, (_, ch) =>
    ch < 8 ? { channel: ch, kind: "pin", index: ch } : { channel: ch, kind: "internal", index: ch - 8 });
}

// 今つないでいるデバイスの並び（state が probe.info を受けたら setChannelLayout。無ければ古い並び）
let currentLayout: LaChannel[] | null = null;

export function setChannelLayout(layout: LaChannel[] | null | undefined): void {
  currentLayout = layout && layout.length ? layout : null;
}

export function channelName(ch: number): string {
  const c = (currentLayout ?? legacyLayout()).find((x) => x.channel === ch);
  if (c?.kind === "internal") return c.index === 0 ? t("timeline.swdioDrive") : c.index === 1 ? t("timeline.dapBusy") : `ch${ch}`;
  if (c?.kind === "input") return `in${c.index}`;
  return `ch${ch}`;
}

/** 選べる ch（何かがつながっている ch。申告の la_layout から、無ければ古い並び） */
export function channelChoices(caps: { la_channels?: number; la_layout?: LaChannel[] }): number[] {
  const layout = caps.la_layout?.length ? caps.la_layout : legacyLayout();
  return layout.filter((c) => c.kind !== "none").map((c) => c.channel);
}

/** 間引いたフレームから、時刻 at の波形の段 row（段の名前 "wave:<ch>"）の値を引く（'0' / '1' / 'x' / 無しなら
 *  undefined。表示の外・波形でない段も undefined） */
export function levelAt(frame: TimelineFrame | null, row: string, at: number): string | undefined {
  const r = frame ? frame.ids.indexOf(row) : -1;
  if (!frame || r < 0 || frame.modes[r] !== MODE.logic || at < frame.v0 || at >= frame.v1) return undefined;
  const px = Math.min(frame.width - 1, Math.floor(((at - frame.v0) / (frame.v1 - frame.v0)) * frame.width));
  const s = frame.data[(r * frame.width + px) * 4];
  return s === ST_NONE ? undefined : "01x"[s];
}

/** 目盛りの時刻: 単位は値の大きさで、桁は間隔 step を区別できるだけ（拡大しても隣どうしが同じ文字にならない） */
/** 時間の目盛り: 表示の区間をおよそ count 個に分ける 1・2・5 の切りのよい間隔。時刻は origin を 0 とする */
export function ticks(v: View, periodS: number, origin: number, count: number): { sample: number; label: string }[] {
  const span = (v.end - v.start) * periodS;
  if (!(span > 0) || count < 1) return [];
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw * (1 - 1e-9))!;
  const out = [];
  const last = Math.floor(((v.end - origin) * periodS) / step + 1e-9);
  for (let k = Math.ceil(((v.start - origin) * periodS) / step - 1e-9); k <= last; k++) {
    const t = k * step;
    out.push({ sample: origin + t / periodS, label: k === 0 ? "0" : formatTick(t, step) });
  }
  return out;
}
