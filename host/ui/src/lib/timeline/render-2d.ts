// Canvas 2D の描画（WebGL2 が使えない端末の代わり）: WebGL2 版と同じ絵を fillRect だけで描く（Path2D に大量の
// 矩形を積むとラスタで止まるので使わない。同じ値の続く画素をまとめる）。縮小した視点では 60 fps を割ることがある。
import { FLAG, MODE, type TimelineFrame } from "./compose";
import { alignFrame, cssColor, type Colors, type Layout, type Rgba } from "./layout";
import type { TimelineRenderer } from "./renderer";
import { ST_CHANGE, ST_NONE } from "./tile";

const scaled = ([r, g, b, a]: Rgba, k: number): string => cssColor([r, g, b, a * k]);

export class CanvasRenderer implements TimelineRenderer {
  readonly kind = "canvas2d";
  private g: CanvasRenderingContext2D;

  constructor(private canvas: HTMLCanvasElement) {
    this.g = canvas.getContext("2d")!;
  }

  draw(given: TimelineFrame | null, layout: Layout, colors: Colors): void {
    const { canvas, g } = this;
    if (canvas.width !== layout.width || canvas.height !== layout.height) {
      canvas.width = layout.width;
      canvas.height = layout.height;
    }
    g.clearRect(0, 0, layout.width, layout.height);
    const dpr = layout.dpr, lw = Math.max(1, Math.round(1.25 * dpr)), pad = 3 * dpr;
    layout.tops.forEach((top, r) => {
      const h = layout.heights[r];
      g.fillStyle = cssColor(colors.grid);
      g.fillRect(0, top + h - 1, layout.width, 1);
    });
    if (!given) return;
    const frame = alignFrame(given, layout.ids);
    const W = frame.width, k = layout.width / W, d = frame.data;
    for (let r = 0; r < frame.rows; r++) {
      const top = layout.tops[r], h = layout.heights[r], mode = frame.modes[r], base = r * W * 4;
      const y0 = top + pad, y1 = top + h - pad, full = y1 - y0;
      const own = (c: number, alpha = 1) => scaled(colors.palette[Math.min(c - 1, colors.palette.length - 1)], alpha);
      /** 同じ値（get）の続く画素ごとに fn */
      const runs = (get: (p: number) => number, fn: (v: number, p: number, e: number) => void) => {
        let p = 0;
        while (p < W) {
          const v = get(p);
          let e = p + 1;
          while (e < W && get(e) === v) e++;
          fn(v, p, e);
          p = e;
        }
      };
      if (mode === MODE.header) {
        g.fillStyle = cssColor(colors.head);
        g.fillRect(0, top, layout.width, h - 1);
      }
      if (mode === MODE.logic) {
        const t0 = y0 + 1;
        runs((p) => d[base + p * 4], (s, p, e) => {
          const x0 = p * k, x1 = e * k;
          if (s === ST_CHANGE) {
            g.fillStyle = cssColor(colors.fill);
            g.fillRect(x0, t0, x1 - x0, y1 - t0);
            g.fillStyle = cssColor(colors.line);
            g.fillRect(x0, t0, x1 - x0, lw);
            g.fillRect(x0, y1 - lw, x1 - x0, lw);
          } else if (s !== ST_NONE) {
            g.fillStyle = cssColor(colors.line);
            g.fillRect(x0, s ? t0 : y1 - lw, x1 - x0, lw);
            const next = e < W ? d[base + e * 4] : ST_NONE;
            if (next < ST_CHANGE && next !== s) g.fillRect(x1, t0, lw, y1 - t0);
          }
        });
      } else if (mode === MODE.density || mode === MODE.header) {
        g.fillStyle = mode === MODE.header ? cssColor(colors.bar) : own(layout.colors[r] + 1, 0.75);
        runs((p) => d[base + p * 4], (v, p, e) => {
          if (v) g.fillRect(p * k, y1 - (full * v) / 255, (e - p) * k, (full * v) / 255);
        });
      } else if (mode === MODE.intervals || mode === MODE.state || mode === MODE.bus) {
        runs((p) => d[base + p * 4] | (d[base + p * 4 + 2] << 8) | (d[base + p * 4 + 3] << 16), (v, p, e) => {
          const c = v & 0xff, depth = (v >> 8) & 0xff, under = v >> 16;
          if (!c) return;
          const x0 = p * k, x1 = e * k, inset = Math.min(depth, 4) * 2 * dpr;
          if (mode === MODE.bus) {
            g.fillStyle = own(c, 0.28);
            g.fillRect(x0, y0, x1 - x0, full);
            g.fillStyle = own(c);
            g.fillRect(x0, y0, x1 - x0, lw);
            g.fillRect(x0, y1 - lw, x1 - x0, lw);
          } else {
            if (inset && under) {
              g.fillStyle = own(under, 0.85);
              g.fillRect(x0, y0, x1 - x0, inset);
            }
            g.fillStyle = own(c, mode === MODE.state ? 0.55 : 0.9);
            g.fillRect(x0, y0 + inset, x1 - x0, full - inset);
          }
        });
        for (let p = 0; p < W; p++) {                       // 区間の始め・終わりの縦線
          const flags = d[base + p * 4 + 1], c = d[base + p * 4];
          if (!c || !(flags & (FLAG.start | FLAG.end))) continue;
          g.fillStyle = mode === MODE.bus ? own(c) : "rgba(0, 0, 0, 0.35)";
          if (flags & FLAG.start) g.fillRect(p * k, y0, lw, full);
          if (flags & FLAG.end && mode === MODE.bus) g.fillRect((p + 1) * k - lw, y0, lw, full);
        }
      } else if (mode === MODE.points) {
        const rr = 4 * dpr, tri = y0 + rr * 1.6;
        for (let p = 0; p < W; p++) {
          if (!(d[base + p * 4 + 1] & FLAG.mark)) continue;
          const cx = (p + 0.5) * k;
          g.fillStyle = own(d[base + p * 4]);
          g.beginPath();
          g.moveTo(cx - rr, y0);
          g.lineTo(cx + rr, y0);
          g.lineTo(cx, tri);
          g.fill();
          g.fillRect(cx - lw / 2, tri, lw, y1 - tri);
        }
      } else if (mode === MODE.values) {
        for (let p = 0; p < W; p++) {
          const c = d[base + p * 4];
          if (!c) continue;
          const yLow = y1 - (full * d[base + p * 4 + 2]) / 255, yHigh = y1 - (full * d[base + p * 4 + 3]) / 255;
          g.fillStyle = own(c, 0.12);
          g.fillRect(p * k, yLow, k, y1 - yLow);
          g.fillStyle = own(c);
          g.fillRect(p * k, yHigh - lw / 2, k, yLow - yHigh + lw);
        }
      }
    }
  }

  dispose(): void {}
}
