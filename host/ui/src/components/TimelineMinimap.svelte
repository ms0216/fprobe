<script lang="ts">
  // 時間軸の縮図（ミニマップ）: 全体のタイルの密度（stream ごとの細い帯）と異常の印（赤 = 誤り、黄 = マーカー・停止）、
  // いま見ている範囲の枠。クリック・ドラッグでその位置へ移動する。Canvas 2D（小さく、全体のタイルが変わるときだけ描く）。
  import { i18n } from "../lib/i18n";
  import { MODE } from "../lib/timeline/compose";
  import { colorsOf, cssColor } from "../lib/timeline/layout";
  import type { TimelineSnapshot, TimelineState } from "../lib/timeline/state";

  let { tl, t }: { tl: TimelineState; t: TimelineSnapshot } = $props();

  const HEIGHT = 34;
  let canvas: HTMLCanvasElement;
  let box: HTMLDivElement;
  let cssWidth = $state(0);
  let dpr = $state(window.devicePixelRatio || 1);
  let dragging = false;

  $effect(() => {
    const ro = new ResizeObserver(([e]) => (cssWidth = Math.floor(e.contentRect.width)));
    ro.observe(box);
    return () => ro.disconnect();
  });
  $effect(() => tl.setOverviewWidth(cssWidth * dpr));

  $effect(() => {
    void [t.overview, t.anomalies, t.view, t.extent, t.marks, cssWidth, dpr];
    paint();
  });

  function paint() {
    if (!canvas || !cssWidth) return;
    const W = Math.round(cssWidth * dpr), H = Math.round(HEIGHT * dpr);
    if (canvas.width !== W || canvas.height !== H) {
      canvas.width = W;
      canvas.height = H;
    }
    const g = canvas.getContext("2d")!;
    g.clearRect(0, 0, W, H);
    const colors = colorsOf(box);
    const f = t.overview, e = t.extent;
    if (f && f.rows) {
      const band = (H - 6 * dpr) / f.rows, k = W / f.width;
      for (let r = 0; r < f.rows; r++) {
        const y = 3 * dpr + r * band, base = r * f.width * 4;
        for (let p = 0; p < f.width; p++) {
          const v = f.data[base + p * 4];
          if (!v) continue;
          const logic = f.modes[r] === MODE.logic;
          if (logic && v !== 2) continue;                     // 波形は変化のある画素だけ
          g.fillStyle = cssColor([...colors.line.slice(0, 3), logic ? 0.6 : 0.15 + 0.85 * (v / 255)] as typeof colors.line);
          g.fillRect(p * k, y, Math.max(1, k), Math.max(1, band - 1));
        }
      }
    }
    if (e && e[1] > e[0]) {
      const xOf = (time: number) => ((time - e[0]) / (e[1] - e[0])) * W;
      for (const a of t.anomalies) {
        g.fillStyle = cssColor(a.tone === 1 ? colors.palette[13] : colors.palette[14]);
        g.fillRect(Math.round(xOf(a.t)), 0, Math.max(1, dpr), H);
      }
      // ビューが置いた印（検索の結果等）: 下の端に短い線
      g.fillStyle = cssColor(colors.selection);
      for (const set of t.marks) {
        if (set.source !== t.source) continue;
        for (const m of set.marks) g.fillRect(Math.round(xOf(m.t)), H - 5 * dpr, Math.max(1, dpr), 5 * dpr);
      }
      const x0 = xOf(t.view.start), x1 = Math.max(x0 + 2 * dpr, xOf(t.view.end));
      g.fillStyle = cssColor([...colors.selection.slice(0, 3), 0.12] as typeof colors.selection);
      g.fillRect(x0, 0, x1 - x0, H);
      g.strokeStyle = cssColor(colors.selection);
      g.lineWidth = dpr;
      g.strokeRect(x0 + dpr / 2, dpr / 2, x1 - x0 - dpr, H - dpr);
      if (t.selection !== null) {
        g.fillStyle = cssColor(colors.selection);
        g.fillRect(Math.round(xOf(t.selection)), 0, dpr, H);
      }
    }
  }

  function moveTo(x: number) {
    const e = t.extent;
    if (!e || !cssWidth) return;
    tl.centerOn(e[0] + (x / cssWidth) * (e[1] - e[0]));
  }
</script>

<div class="minimap" bind:this={box} title={$i18n("timeline.overviewTip")} role="scrollbar" tabindex="-1"
     aria-controls="timeline" aria-orientation="horizontal" aria-valuenow={t.view.start}
     aria-label={$i18n("timeline.overview")}
     onpointerdown={(e) => { dragging = true; box.setPointerCapture(e.pointerId); moveTo(e.offsetX); }}
     onpointermove={(e) => { if (dragging) moveTo(e.offsetX); }}
     onpointerup={() => (dragging = false)}>
  <canvas bind:this={canvas} style:height="{HEIGHT}px"></canvas>
</div>

<style>
  .minimap { margin-left: 160px; border-bottom: 1px solid var(--line); cursor: pointer; touch-action: none; }
  canvas { display: block; width: 100%; }
</style>
