// 描画の口: WebGL2 を使い、使えなければ Canvas 2D（遅いことを画面に出す）。文字・カーソルの重ねは overlay.ts。
import type { TimelineFrame } from "./compose";
import type { Colors, Layout } from "./layout";
import { CanvasRenderer } from "./render-2d";
import { GlRenderer } from "./render-gl";

export interface TimelineRenderer {
  readonly kind: "webgl2" | "canvas2d";
  draw(frame: TimelineFrame | null, layout: Layout, colors: Colors): void;
  dispose(): void;
}

/** prefer = "canvas2d" で Canvas 2D を強いる（比べる・試すため） */
export function createRenderer(canvas: HTMLCanvasElement, prefer: "webgl2" | "canvas2d" = "webgl2"): TimelineRenderer {
  if (prefer === "webgl2") {
    const gl = GlRenderer.create(canvas);
    if (gl) return gl;
  }
  return new CanvasRenderer(canvas);
}
