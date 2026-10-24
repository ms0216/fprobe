<script lang="ts">
  // ビューのグラフ（Canvas 2D）。型（type）:
  // - line: 時系列の線（複数・階段・右の目盛り）
  // - stacked: 積み上げ（系列を下から足した面。CPU Load のタスク別の負荷。系列は同じ x の並び）
  // - scatter: 散布図（点。新しいものほど濃く、最新の点を強調。Data Sampling の XY）
  // 棒（分布図・窓ごとの負荷）は bars で渡す。目盛りは自動、ホバーで縦線と値（散布図はいちばん近い点）。
  // xFormat(x, step) の step は目盛りの間隔・1 画素の幅（時刻の桁を決める。lib/time.ts の axisTime）、
  // クリック = onpick（ビューの中で選ぶ。3 つ目の引数で修飾キー）、ダブルクリック = onopen（時間軸のその時刻へ等）。
  // marks = 名前付きの縦の破線（Data Sampling のカーソル A・B）。
  // 色は時間軸の段と同じ 12 色。計算は lib/chart.ts
  import {
    extentOf, nearestIndex, nearestPoint, niceDomain, niceTicks, seriesColor, shortNumber, stackTops, type Bar,
    type ChartType, type Series,
  } from "../../lib/chart";
  import { i18n } from "../../lib/i18n";

  let {
    type = "line", series = [], bars = [], xDomain, yDomain, includeZero = false, height = 160, cursor = null,
    legend = true, xFormat = shortNumber, yFormat = shortNumber, xLabel = "", yLabel = "", maxPoints = 3000, onpick,
    onopen, label = "", marks = [],
  }: {
    type?: ChartType; series?: Series[]; bars?: Bar[]; xDomain?: [number, number] | null;
    yDomain?: [number, number] | null; includeZero?: boolean; height?: number; cursor?: number | null; legend?: boolean;
    xFormat?: (x: number, step: number) => string; yFormat?: (y: number) => string; xLabel?: string; yLabel?: string;
    maxPoints?: number;
    onpick?: (x: number, bar: number | null, e: MouseEvent) => void;
    onopen?: (x: number, bar: number | null, e: MouseEvent) => void; label?: string;
    marks?: { x: number; label: string }[];
  } = $props();

  let box: HTMLDivElement;
  let canvas: HTMLCanvasElement;
  let width = $state(0);
  let dpr = $state(typeof window === "undefined" ? 1 : window.devicePixelRatio || 1);
  let hover = $state<{ x: number; px: number; py: number } | null>(null);
  let queued = 0;

  // 散布図は新しい maxPoints 点だけ（古いものから捨てる）
  const shown = $derived(type === "scatter"
    ? series.map((s) => (s.x.length > maxPoints ? { ...s, x: Array.from(s.x).slice(-maxPoints),
                                                     y: Array.from(s.y).slice(-maxPoints) } : s))
    : series);
  // 散布図で捨てた点（黙って捨てない: 描いた数・全部の数・上限を出す）
  const scatterTotal = $derived(type === "scatter" ? Math.max(0, ...series.map((s) => s.x.length)) : 0);
  const tops = $derived(type === "stacked" ? stackTops(shown) : null);
  const left = $derived(shown.some((s) => s.axis !== "right") || bars.length ? 52 : 8);
  const right = $derived(shown.some((s) => s.axis === "right") ? 52 : 10);
  const TOP = $derived(yLabel ? 18 : 8);
  const bottom = $derived(xLabel ? 30 : 20);

  const xd = $derived.by((): [number, number] => {
    if (xDomain) return xDomain;
    if (type === "scatter") return niceDomain(extentOf(shown.map((s) => s.x)));
    const e = bars.length ? extentOf([bars.map((b) => b.x0), bars.map((b) => b.x1)]) : extentOf(shown.map((s) => s.x));
    return e && e[1] > e[0] ? e : e ? [e[0] - 1, e[1] + 1] : [0, 1];
  });
  const yl = $derived(yDomain ?? niceDomain(bars.length ? extentOf([bars.map((b) => b.y)])
    : tops ? extentOf(tops) : extentOf(shown.filter((s) => s.axis !== "right").map((s) => s.y)),
                                            includeZero || bars.length > 0 || type === "stacked"));
  const yr = $derived(niceDomain(extentOf(shown.filter((s) => s.axis === "right").map((s) => s.y)), includeZero));

  $effect(() => {
    const ro = new ResizeObserver(([e]) => (width = Math.floor(e.contentRect.width)));
    ro.observe(box);
    const onResize = () => (dpr = window.devicePixelRatio || 1);
    window.addEventListener("resize", onResize);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", onResize);
    };
  });

  $effect(() => {
    void [shown, bars, tops, xd, yl, yr, width, height, dpr, cursor, hover, left, right, bottom, TOP, type, marks];
    if (!queued) queued = requestAnimationFrame(paint);
    return () => {
      cancelAnimationFrame(queued);
      queued = 0;
    };
  });

  const plotW = () => Math.max(1, width - left - right);
  const plotH = () => height - TOP - bottom;
  const xOf = (x: number) => left + ((x - xd[0]) / (xd[1] - xd[0])) * plotW();
  const xAt = (px: number) => xd[0] + ((px - left) / plotW()) * (xd[1] - xd[0]);
  const yOf = (y: number, d: [number, number] = yl) => TOP + (1 - (y - d[0]) / (d[1] - d[0])) * plotH();

  function paint() {
    queued = 0;
    if (!canvas || !width) return;
    const css = getComputedStyle(box);
    const color = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
    const muted = color("--muted", "#888"), line = color("--line", "#ddd");
    const accent = color("--accent", "#2563eb"), warn = color("--warn", "#b45309");
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const g = canvas.getContext("2d")!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, width, height);
    g.font = "11px ui-monospace, Consolas, monospace";
    const h = plotH();

    // 目盛り（横線と左右の値、横軸。散布図は縦線も）
    g.strokeStyle = line;
    g.fillStyle = muted;
    g.lineWidth = 1;
    g.textBaseline = "middle";
    const yCount = Math.max(2, Math.floor(h / 32));
    if (left > 10) {
      g.textAlign = "right";
      for (const v of niceTicks(yl[0], yl[1], yCount)) {
        const y = Math.round(yOf(v)) + 0.5;
        g.beginPath();
        g.moveTo(left, y);
        g.lineTo(width - right, y);
        g.stroke();
        g.fillText(yFormat(v), left - 4, y);
      }
    }
    if (right > 10) {
      g.textAlign = "left";
      for (const v of niceTicks(yr[0], yr[1], yCount)) g.fillText(yFormat(v), width - right + 4, yOf(v, yr));
    }
    g.textAlign = "center";
    g.textBaseline = "top";
    const xTicks = niceTicks(xd[0], xd[1], Math.max(2, Math.floor(plotW() / 90)));
    const xStep = xTicks.length > 1 ? xTicks[1] - xTicks[0] : xd[1] - xd[0];
    for (const v of xTicks) {
      const x = Math.round(xOf(v)) + 0.5;
      g.beginPath();
      g.moveTo(x, type === "scatter" ? TOP : TOP + h);
      g.lineTo(x, TOP + h + 4);
      g.stroke();
      g.fillText(xFormat(v, xStep), x, TOP + h + 6);
    }
    if (xLabel) {
      g.textAlign = "right";
      g.fillText(xLabel, width - right, height - 12);
    }
    if (yLabel) {
      g.textAlign = "left";
      g.fillText(yLabel, 4, 0);
    }

    g.save();
    g.beginPath();
    g.rect(left, TOP - 1, plotW(), h + 2);
    g.clip();
    // 棒
    for (const b of bars) {
      const x0 = xOf(b.x0), x1 = Math.max(x0 + 1, xOf(b.x1) - 1), y = yOf(b.y), y0 = yOf(Math.max(yl[0], 0));
      g.fillStyle = b.color ?? (b.mark ? warn : accent);
      g.globalAlpha = b.mark ? 0.95 : 0.7;
      g.fillRect(x0, Math.min(y, y0), x1 - x0, Math.abs(y0 - y));
    }
    g.globalAlpha = 1;
    if (type === "stacked" && tops) paintStacked(g);
    else if (type === "scatter") paintScatter(g, warn);
    else paintLines(g);
    g.restore();

    // カーソル（時間軸で選んだ時刻等）とホバー
    const vline = (x: number, c: string, dash: number[]) => {
      if (x < left || x > width - right) return;
      g.strokeStyle = c;
      g.setLineDash(dash);
      g.beginPath();
      g.moveTo(Math.round(x) + 0.5, TOP);
      g.lineTo(Math.round(x) + 0.5, TOP + h);
      g.stroke();
      g.setLineDash([]);
    };
    if (cursor !== null && Number.isFinite(cursor)) vline(xOf(cursor), accent, []);
    g.textAlign = "left";
    g.textBaseline = "top";
    for (const m of marks) {
      const x = xOf(m.x);
      if (!Number.isFinite(x) || x < left || x > width - right) continue;
      vline(x, warn, [4, 3]);
      g.fillStyle = warn;
      g.fillText(m.label, Math.round(x) + 3, TOP);
    }
    if (hover && type !== "scatter") vline(hover.px, muted, [2, 2]);
  }

  /** 線（ビューの系列は数千点まで: そのまま結ぶ。多い時系列は時間軸の段で見る） */
  function paintLines(g: CanvasRenderingContext2D) {
    shown.forEach((s, i) => {
      const d = s.axis === "right" ? yr : yl;
      g.strokeStyle = seriesColor(s, i);
      g.globalAlpha = s.dim ? 0.3 : 1;
      g.lineWidth = 1.5;
      g.beginPath();
      let pen = false, lastY = NaN;
      for (let k = 0; k < s.x.length; k++) {
        const v = s.y[k];
        if (!Number.isFinite(v)) {
          pen = false;
          continue;
        }
        const px = xOf(s.x[k]), py = yOf(v, d);
        if (!pen) g.moveTo(px, py);
        else {
          if (s.step) g.lineTo(px, lastY);
          g.lineTo(px, py);
        }
        pen = true;
        lastY = py;
      }
      g.stroke();
    });
    g.globalAlpha = 1;
  }

  /** 積み上げ: 下の系列の上端から自分の上端までの面（値の無い所で切る）。階段の系列は次の点まで保つ */
  function paintStacked(g: CanvasRenderingContext2D) {
    const n = shown.length ? shown[0].x.length : 0;
    const xs = shown[0]?.x ?? [];
    const step = shown.some((s) => s.step);
    const xEnd = (k: number) => (k + 1 < n ? xOf(xs[k + 1]) : left + plotW());
    shown.forEach((s, i) => {
      const top = tops![i], below = i > 0 ? tops![i - 1] : null;
      const color = seriesColor(s, i);
      // 値のある点の続き（run）ごとに 1 つの面
      let k = 0;
      while (k < n) {
        while (k < n && !Number.isFinite(top[k])) k++;
        const start = k;
        while (k < n && Number.isFinite(top[k])) k++;
        if (k === start) continue;
        const baseAt = (j: number) => yOf(below && Number.isFinite(below[j]) ? below[j] : 0);
        g.beginPath();
        for (let j = start; j < k; j++) {
          const x = xOf(xs[j]), y = yOf(top[j]);
          if (j === start) g.moveTo(x, y);
          else g.lineTo(x, y);
          if (step) g.lineTo(xEnd(j), y);
        }
        for (let j = k - 1; j >= start; j--) {
          const y = baseAt(j);
          if (step) g.lineTo(xEnd(j), y);
          g.lineTo(xOf(xs[j]), y);
        }
        g.closePath();
        g.fillStyle = color;
        g.globalAlpha = s.dim ? 0.12 : 0.5;
        g.fill();
        g.globalAlpha = s.dim ? 0.3 : 1;
        g.strokeStyle = color;
        g.lineWidth = 1.2;
        g.beginPath();
        for (let j = start; j < k; j++) {
          const x = xOf(xs[j]), y = yOf(top[j]);
          if (j === start) g.moveTo(x, y);
          else g.lineTo(x, y);
          if (step) g.lineTo(xEnd(j), y);
        }
        g.stroke();
      }
    });
    g.globalAlpha = 1;
  }

  /** 散布図: 新しい点ほど濃く、最新の点を大きく（警告の色） */
  function paintScatter(g: CanvasRenderingContext2D, warn: string) {
    shown.forEach((s, i) => {
      const n = s.x.length;
      g.fillStyle = seriesColor(s, i);
      for (let k = 0; k < n; k++) {
        if (!Number.isFinite(s.x[k]) || !Number.isFinite(s.y[k])) continue;
        const last = k === n - 1;
        g.globalAlpha = last ? 1 : 0.25 + (0.75 * (k + 1)) / n;
        if (last) g.fillStyle = warn;
        g.beginPath();
        g.arc(xOf(s.x[k]), yOf(s.y[k]), last ? 4 : 2, 0, 2 * Math.PI);
        g.fill();
      }
    });
    g.globalAlpha = 1;
  }

  /** ホバーの位置の値: 線・積み上げは各系列（階段は直前の点、線はいちばん近い点。積み上げは上から。値は使った点の
   *  時刻とともに出す = ホバーの時刻に測った値ではない）、棒はその棒、散布図はいちばん近い点 */
  const readout = $derived.by(() => {
    if (!hover) return null;
    const x = hover.x;
    const pixel = (xd[1] - xd[0]) / plotW();          // ホバーの時刻の桁: 1 画素の幅が見えるだけ
    if (bars.length) {
      const i = bars.findIndex((b) => x >= b.x0 && x < b.x1);
      return i < 0 ? null : { head: `${xFormat(bars[i].x0, pixel)} – ${xFormat(bars[i].x1, pixel)}`, rows: [{ name: "", value: shortNumber(bars[i].y), color: "" }] };
    }
    if (type === "scatter") {
      for (const [i, s] of shown.entries()) {
        const k = nearestPoint(s.x, s.y, hover.px, hover.py, xOf, (v) => yOf(v));
        if (k < 0) continue;
        return { head: `${xLabel ? `${xLabel} = ` : ""}${xFormat(s.x[k], pixel)}`,
                 rows: [{ name: yLabel || s.name, value: yFormat(s.y[k]), color: seriesColor(s, i) }] };
      }
      return null;
    }
    const rows = shown.map((s, i) => {
      let k = nearestIndex(s.x, x);
      if (s.step) while (k > 0 && s.x[k] > x) k--;
      const v = k >= 0 ? s.y[k] : NaN;
      return { name: s.name, value: Number.isFinite(v) ? `${yFormat(v)}${s.unit ? ` ${s.unit}` : ""} @ ${xFormat(s.x[k], pixel)}` : "—",
               color: seriesColor(s, i) };
    });
    return { head: xFormat(x, pixel), rows: type === "stacked" ? rows.reverse() : rows };
  });

  function barAt(x: number): number | null {
    const i = bars.findIndex((b) => x >= b.x0 && x < b.x1);
    return i < 0 ? null : i;
  }

  function onMove(e: PointerEvent) {
    if (e.offsetX < left || e.offsetX > width - right) {
      hover = null;
      return;
    }
    hover = { x: xAt(e.offsetX), px: e.offsetX, py: e.offsetY };
  }

  // 凡例: 積み上げは上の系列から（面の並びと同じ）
  const legendItems = $derived((type === "stacked" ? shown.map((s, i) => ({ s, i })).reverse() : shown.map((s, i) => ({ s, i }))));
</script>

<div class="chart" bind:this={box} style:height="{height}px">
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <canvas bind:this={canvas} style:height="{height}px" aria-label={label}
          onpointermove={onMove} onpointerleave={() => (hover = null)}
          onclick={(e) => { if (onpick && e.offsetX >= left) { const x = xAt(e.offsetX); onpick(x, barAt(x), e); } }}
          ondblclick={(e) => { if (onopen && e.offsetX >= left) { const x = xAt(e.offsetX); onopen(x, barAt(x), e); } }}></canvas>
  {#if legend && shown.length > 1}
    <div class="legend">
      {#each legendItems as { s, i } (s.name)}<span class:dim={s.dim}><i style:background={seriesColor(s, i)}></i>{s.name}</span>{/each}
    </div>
  {/if}
  {#if scatterTotal > maxPoints}
    <div class="cut">{$i18n("chart.scatterTruncated", { shown: maxPoints, total: scatterTotal, limit: maxPoints })}</div>
  {/if}
  {#if hover && readout}
    <div class="tip mono" style:left="{Math.min(hover.px + 12, width - 180)}px">
      <div class="head">{readout.head}</div>
      {#each readout.rows as r (r.name)}
        <div>{#if r.color}<i style:background={r.color}></i>{/if}{r.name ? `${r.name}: ` : ""}{r.value}</div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .chart { position: relative; width: 100%; min-width: 0; }
  canvas { display: block; width: 100%; cursor: crosshair; }
  .legend { position: absolute; top: 2px; right: 12px; display: flex; gap: 10px; font-size: 11px; color: var(--muted);
            background: color-mix(in srgb, var(--panel) 80%, transparent); padding: 0 4px; border-radius: 3px;
            flex-wrap: wrap; justify-content: flex-end; max-width: 70%; }
  .legend .dim { opacity: 0.5; }
  i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
  .tip { position: absolute; top: 10px; pointer-events: none; background: var(--panel); border: 1px solid var(--line);
         border-radius: 4px; padding: 4px 6px; font-size: 11.5px; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15); z-index: 2;
         max-width: 220px; }
  .head { color: var(--muted); margin-bottom: 2px; }
  .cut { font-size: 11px; color: var(--muted); padding: 2px 4px; }
</style>
