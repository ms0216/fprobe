// 時間軸の重ね（Canvas 2D）: 目盛りと時刻、トリガ、カーソル 3 種（選択・マウス・基準）と差、選んだ出来事の枠、
// 区間・箱・印のラベル、値の段の目盛り、ビューが置いた印（目盛りの帯の下の段）。波形の絵（WebGL2）とは別のキャンバスなので、カーソルを動かしても
// 波形は描き直さない。
import type { View } from "../waveform";
import { plainNumber } from "../format";
import { formatDuration, formatSeconds } from "../time";
import { ticks } from "../waveform";
import type { TimelineFrame } from "./compose";
import { cssColor, type Colors, type Layout } from "./layout";

export interface Cursors {
  selection: number | null;          // 選択カーソル（クリック。全部のパネルをこの時刻に）
  mouse: number | null;              // マウスの位置の縦線
  reference: number | null;          // 基準（R。差を測る）
}

export interface OverlayInput {
  view: View;
  tickS: number;
  origin: number;                    // 時刻 0 の位置（捕捉・記録の始め。lib/time.ts）
  resolution: number;                // 時刻の桁（秒）: 1 画素の時間（と 1 単位）を区別できるだけ
  trigger: number | null;
  cursors: Cursors;
  frame: TimelineFrame | null;
  selected: { row: string; t: number; tEnd: number } | null;
  units: (string | null)[];          // 段ごとの値の単位（values の段の目盛り）
  marks?: OverlayMark[];             // ビューが置いた印（t の昇順。今の源のものだけ）
  pointers?: { t: number; label: string; role: string }[];   // ほかの窓・AI のマウスのカーソル（今の源のものだけ）
}

export interface OverlayMark {
  t: number;
  tEnd?: number;
  label?: string;
  tone?: number;                     // 0 = 普通 / 1 = bad / 2 = good / 3 = warn
}

const MARK_LINES = 50;               // 見えている印がこれ以下なら、印の時刻に縦の点線も引く

const FONT = "11px ui-monospace, Consolas, monospace";
let charWidth = 0;

function fit(g: CanvasRenderingContext2D, text: string, width: number): string | null {
  if (!charWidth) charWidth = g.measureText("M").width || 6.6;
  const n = Math.floor((width - 6) / charWidth);
  if (n < 1) return null;
  return text.length <= n ? text : n > 1 ? `${text.slice(0, n - 1)}…` : text[0];
}

export function drawOverlay(canvas: HTMLCanvasElement, layout: Layout, colors: Colors, input: OverlayInput): void {
  if (canvas.width !== layout.width || canvas.height !== layout.height) {
    canvas.width = layout.width;
    canvas.height = layout.height;
  }
  const g = canvas.getContext("2d")!;
  const dpr = layout.dpr, cssW = layout.width / dpr, cssH = layout.height / dpr;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, cssW, cssH);
  const { view } = input;
  const span = view.end - view.start;
  if (!(span > 0) || !(input.tickS > 0)) return;
  const xOf = (t: number) => ((t - view.start) / span) * cssW;
  const ruler = layout.ruler / dpr;
  const rowIndex = new Map(layout.ids.map((id, i) => [id, i]));
  g.font = FONT;
  g.textBaseline = "alphabetic";
  g.lineWidth = 1;
  g.strokeStyle = cssColor([...colors.grid.slice(0, 3), 0.6] as typeof colors.grid);
  g.fillStyle = cssColor(colors.muted);
  g.beginPath();
  for (const t of ticks(view, input.tickS, input.origin, Math.max(2, Math.floor(cssW / 110)))) {
    const x = Math.round(xOf(t.sample)) + 0.5;
    g.moveTo(x, ruler - 6);
    g.lineTo(x, cssH);
    g.fillText(t.label, x + 3, 12);
  }
  g.stroke();

  const f = input.frame;
  if (f) {
    // 区間・箱・印のラベル（Worker が幅の足りるものを選んだ）
    const k = cssW / f.width;
    g.textBaseline = "middle";
    for (const label of f.labels) {
      const r = rowIndex.get(f.ids[label.row]);
      if (r === undefined) continue;
      const top = layout.tops[r] / dpr, h = layout.heights[r] / dpr;
      if (label.x0 === label.x1) {                                   // 印: 印の右に
        const text = fit(g, label.text, 200);
        if (!text) continue;
        g.fillStyle = cssColor(label.tone === 1 ? colors.palette[13] : colors.text);
        g.fillText(text, label.x0 * k + 7, top + h / 2 + 2);
        continue;
      }
      const text = fit(g, label.text, (label.x1 - label.x0) * k);
      if (!text) continue;
      g.fillStyle = cssColor(colors.text);
      g.fillText(text, label.x0 * k + 3, top + h / 2 + 1);
    }
    // 値の段の目盛り（上と下の値。左端に、地を敷いて）
    f.ids.forEach((id, i) => {
      const scale = f.scales[i], r = rowIndex.get(id);
      if (!scale || r === undefined || !(scale[0] <= scale[1])) return;
      const top = layout.tops[r] / dpr, h = layout.heights[r] / dpr, unit = input.units[r] ? ` ${input.units[r]}` : "";
      for (const [text, y, base] of [[`${number(scale[1])}${unit}`, top + 2, "top"],
                                     [`${number(scale[0])}${unit}`, top + h - 3, "bottom"]] as const) {
        const w = g.measureText(text).width + 6;
        g.fillStyle = cssColor([...colors.panel.slice(0, 3), 0.8] as typeof colors.panel);
        g.fillRect(2, base === "top" ? y - 1 : y - 12, w, 13);
        g.fillStyle = cssColor(colors.muted);
        g.textBaseline = base;
        g.fillText(text, 5, y);
      }
    });
  }

  if (input.marks?.length) drawMarks(g, input.marks, view, cssW, cssH, ruler, colors);

  // 選んだ出来事の枠
  const sel = input.selected;
  const r = sel ? rowIndex.get(sel.row) : undefined;
  if (sel && r !== undefined) {
    const top = layout.tops[r] / dpr, h = layout.heights[r] / dpr;
    const x0 = xOf(sel.t), x1 = Math.max(x0 + 2, xOf(sel.tEnd));
    if (x1 >= 0 && x0 <= cssW) {
      g.strokeStyle = cssColor(colors.selection);
      g.lineWidth = 2;
      g.strokeRect(Math.max(-2, x0) - 1, top + 1, Math.min(cssW + 4, x1) - Math.max(-2, x0) + 2, h - 3);
      g.lineWidth = 1;
    }
  }

  const line = (t: number, color: string, dash: number[], width = 1) => {
    const x = Math.round(xOf(t)) + 0.5;
    if (x < 0 || x > cssW) return null;
    g.strokeStyle = color;
    g.lineWidth = width;
    g.setLineDash(dash);
    g.beginPath();
    g.moveTo(x, ruler - 4);
    g.lineTo(x, cssH);
    g.stroke();
    g.setLineDash([]);
    g.lineWidth = 1;
    return x;
  };
  const { selection, mouse, reference } = input.cursors;
  if (input.trigger !== null) line(input.trigger, cssColor(colors.trigger), [4, 3]);
  const flag = (x: number | null, text: string, color: string) => {
    if (x === null) return;
    g.font = FONT;
    const w = g.measureText(text).width + 8;
    const left = Math.min(Math.max(0, x - w / 2), cssW - w);
    g.fillStyle = color;
    g.fillRect(left, 0, w, 15);
    g.fillStyle = "#fff";
    g.textBaseline = "middle";
    g.fillText(text, left + 4, 8);
  };
  const time = (t: number) => formatSeconds((t - input.origin) * input.tickS, input.resolution);
  if (reference !== null) flag(line(reference, cssColor(colors.reference), [6, 3], 1.5), `R ${time(reference)}`,
                               cssColor(colors.reference));
  if (selection !== null) {
    const x = line(selection, cssColor(colors.selection), [], 1.5);
    const delta = reference !== null ? `  Δ ${formatDuration((selection - reference) * input.tickS)}` : "";
    flag(x, `${time(selection)}${delta}`, cssColor(colors.selection));
  }
  if (mouse !== null) line(mouse, cssColor([...colors.cursor.slice(0, 3), 0.7] as typeof colors.cursor), [2, 2]);
  // ほかの窓・AI のマウスのカーソル: 役割の色の点線と、目盛りの帯のすぐ下に名前
  for (const p of input.pointers ?? []) {
    const color = cssColor(p.role === "ai" ? colors.palette[3] : p.role === "human" ? colors.palette[12] : colors.palette[15]);
    const x = line(p.t, color, [5, 3], 1.5);
    if (x === null) continue;
    g.font = "10px ui-monospace, Consolas, monospace";
    const text = fit(g, p.label, 120) ?? "";
    const w = g.measureText(text).width + 6;
    const left = Math.min(Math.max(0, x + 2), cssW - w);
    g.fillStyle = color;
    g.fillRect(left, ruler + 1, w, 12);
    g.fillStyle = "#fff";
    g.textBaseline = "middle";
    g.fillText(text, left + 3, ruler + 7.5);
  }
  g.font = FONT;
}

/** ビューの印: 目盛りの帯の下の段に三角（区間は下の線）、間があればラベル。少なければ縦の点線も */
function drawMarks(g: CanvasRenderingContext2D, marks: OverlayMark[], view: View, cssW: number, cssH: number,
                   ruler: number, colors: Colors): void {
  const span = view.end - view.start;
  const xOf = (t: number) => ((t - view.start) / span) * cssW;
  // 見えている範囲（区間は始まりが前でも終わりが見えていれば）: 始まりの二分探索で、前の 64 件まで区間を見る
  let lo = 0, hi = marks.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (marks[mid].t < view.start) lo = mid + 1;
    else hi = mid;
  }
  const first = Math.max(0, lo - 64);
  const visible: OverlayMark[] = [];
  for (let i = first; i < marks.length && marks[i].t < view.end; i++) {
    const m = marks[i];
    if ((m.tEnd ?? m.t) >= view.start) visible.push(m);
  }
  if (!visible.length) return;
  const tone = (m: OverlayMark) => cssColor(m.tone === 1 ? colors.palette[13] : m.tone === 2 ? colors.palette[12]
    : m.tone === 3 ? colors.palette[14] : colors.selection);
  const y = ruler - 9;
  if (visible.length <= MARK_LINES) {
    g.setLineDash([1, 3]);
    for (const m of visible) {
      const x = Math.round(xOf(m.t)) + 0.5;
      g.strokeStyle = tone(m);
      g.globalAlpha = 0.55;
      g.beginPath();
      g.moveTo(x, ruler);
      g.lineTo(x, cssH);
      g.stroke();
    }
    g.setLineDash([]);
    g.globalAlpha = 1;
  }
  g.font = "10px ui-monospace, Consolas, monospace";
  g.textBaseline = "middle";
  let lastLabelEnd = -Infinity;
  visible.forEach((m, i) => {
    const x = xOf(m.t);
    g.fillStyle = tone(m);
    if (m.tEnd !== undefined && m.tEnd > m.t) g.fillRect(Math.max(0, x), ruler - 3, Math.max(1, Math.min(cssW, xOf(m.tEnd)) - Math.max(0, x)), 2);
    g.beginPath();
    g.moveTo(x - 4, y);
    g.lineTo(x + 4, y);
    g.lineTo(x, y + 6);
    g.closePath();
    g.fill();
    if (!m.label) return;
    const next = i + 1 < visible.length ? xOf(visible[i + 1].t) : cssW;
    const room = next - x - 10;
    if (room < 36 || x + 6 < lastLabelEnd) return;
    const text = fit(g, m.label, room);
    if (!text) return;
    const w = g.measureText(text).width + 4;
    g.fillStyle = cssColor([...colors.panel.slice(0, 3), 0.85] as typeof colors.panel);
    g.fillRect(x + 5, y - 2, w, 11);
    g.fillStyle = tone(m);
    g.fillText(text, x + 7, y + 3.5);
    lastLabelEnd = x + 5 + w;
  });
  g.font = FONT;
}

/** 値の目盛りの数（普通の表記。整数はそのまま。変数のサンプルの番地・カウンタを指数で丸めない） */
function number(v: number): string {
  return plainNumber(v);
}
