// 時間軸の段の並びと色。描画（WebGL2 / Canvas 2D / 文字の重ね）と名前の列（DOM）が同じ寸法を使う。
// 寸法は CSS の画素で決め、描画はデバイスの画素（× dpr）で行う。段の高さは段の型ごと（rows.ts の ROW_HEIGHT）。
import { rowTops, type VisibleRow } from "./rows";

export const RULER = 22;             // 目盛りの帯

export interface Layout {
  dpr: number;
  width: number;                     // デバイスの画素
  height: number;
  ruler: number;
  tops: number[];                    // 段の上端（デバイスの画素、キャンバスの上から）
  heights: number[];
  ids: string[];                     // 段の名前（フレームの段をこの並びに合わせて描く）
  colors: number[];                  // 段の色（パレットの番号）
}

export function layoutOf(cssWidth: number, dpr: number, rows: VisibleRow[]): Layout {
  const { tops, heights, total } = rowTops(rows);
  return { dpr, width: Math.max(1, Math.round(cssWidth * dpr)), height: Math.round((RULER + total + 4) * dpr),
           ruler: RULER * dpr, tops: tops.map((y) => Math.round((RULER + y) * dpr)),
           heights: heights.map((h) => Math.round(h * dpr)), ids: rows.map((r) => r.def.id),
           colors: rows.map((r) => r.def.color) };
}

export function cssHeight(rows: VisibleRow[]): number {
  return RULER + rowTops(rows).total + 4;
}

/** 時間軸の y（CSS の画素）にある段の番号（無ければ -1） */
export function rowAtY(rows: VisibleRow[], y: number): number {
  const { tops, heights } = rowTops(rows);
  const yy = y - RULER;
  for (let i = 0; i < tops.length; i++) if (yy >= tops[i] && yy < tops[i] + heights[i]) return i;
  return -1;
}

export type Rgba = [number, number, number, number];      // 0..1

export const PALETTE_COLORS = 16;    // 0..11 = 段の色、12 = good、13 = bad、14 = warn、15 = 控えめ

export interface Colors {
  line: Rgba;                        // 波形の線
  fill: Rgba;                        // 変化の塊（半透明）
  bar: Rgba;                         // 件数の棒（まとまりの見出し）
  head: Rgba;                        // まとまりの見出しの段の地
  panel: Rgba;                       // パネルの地（文字の下に敷く）
  text: Rgba;
  muted: Rgba;
  grid: Rgba;
  trigger: Rgba;
  cursor: Rgba;                      // マウスの縦線
  selection: Rgba;                   // 選択カーソル
  reference: Rgba;                   // 基準のカーソル
  palette: Rgba[];                   // PALETTE_COLORS 色
}

/** "#rgb" / "#rrggbb" / "rgb(…)" / "rgba(…)" → 0..1 の RGBA */
export function parseColor(css: string, alpha = 1): Rgba {
  const s = css.trim();
  let m = /^#([0-9a-f]{3})$/i.exec(s);
  if (m) return [...[...m[1]].map((c) => parseInt(c + c, 16) / 255), alpha] as Rgba;
  m = /^#([0-9a-f]{6})$/i.exec(s);
  if (m) return [0, 2, 4].map((i) => parseInt(m![1].slice(i, i + 2), 16) / 255).concat(alpha) as Rgba;
  m = /^rgba?\(([^)]+)\)$/i.exec(s);
  if (m) {
    const [r, g, b, a = "1"] = m[1].split(/[,\s/]+/).filter(Boolean);
    return [+r / 255, +g / 255, +b / 255, +a * alpha];
  }
  return [0.5, 0.5, 0.5, alpha];
}

export function cssColor([r, g, b, a]: Rgba): string {
  return `rgba(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}, ${a})`;
}

/** 段の色（タスク・割り込みを見分ける。明るい・暗いの両方で読める中くらいの明るさ） */
export const CATEGORICAL = ["#4e79a7", "#f28e2b", "#59a14f", "#b07aa1", "#76b7b2", "#edc948", "#e15759", "#9c755f",
                     "#ff9da7", "#86bcb6", "#8cd17d", "#a0cbe8"];

/** 要素の CSS 変数（app.css の色）から描画の色を作る（明るい・暗いの切り替えに従う） */
export function colorsOf(el: Element): Colors {
  const css = getComputedStyle(el);
  const v = (name: string, alpha = 1, fallback = "#888") => parseColor(css.getPropertyValue(name) || fallback, alpha);
  const palette = CATEGORICAL.map((c) => parseColor(c));
  palette.push(v("--ok", 1, "#15803d"), v("--bad", 1, "#d64545"), v("--warn", 1, "#d79a1d"), v("--muted"));
  return { line: v("--accent"), fill: v("--accent", 0.35), bar: v("--muted", 0.6), head: v("--line", 0.35), panel: v("--panel", 1, "#fff"),
           text: v("--text"), muted: v("--muted"), grid: v("--line"), trigger: v("--warn"), cursor: v("--muted"),
           selection: v("--accent"), reference: v("--warn"), palette };
}

/** フレームの段を layout の並びに合わせる（段の構成を変えた直後、前のフレームを描き直すとき）。合わなければ空の段 */
export function alignFrame<F extends { ids: string[]; rows: number; width: number; data: Uint8Array; modes: Uint8Array }>(
  frame: F, ids: string[]): F {
  if (frame.ids.length === ids.length && frame.ids.every((id, i) => id === ids[i])) return frame;
  const W = frame.width, data = new Uint8Array(ids.length * W * 4), modes = new Uint8Array(ids.length);
  const at = new Map(frame.ids.map((id, i) => [id, i]));
  ids.forEach((id, r) => {
    const i = at.get(id);
    if (i === undefined) return;
    data.set(frame.data.subarray(i * W * 4, (i + 1) * W * 4), r * W * 4);
    modes[r] = frame.modes[i];
  });
  return { ...frame, ids, rows: ids.length, data, modes };
}
