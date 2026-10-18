<script lang="ts">
  // 時間軸の部品: 段（波形・区間・箱・状態・印・値・密度）を WebGL2（無ければ Canvas 2D）で描き、目盛り・カーソル 3 種・
  // ラベルを Canvas 2D で重ねる。拡大（ホイール）・移動（ドラッグ・Shift + ホイール）・クリックで選ぶ・Shift + クリックで
  // 基準・ダブルクリックで出来事の元へ（無ければ全体）・ホバーの説明・キーボード。
  import { onDestroy } from "svelte";
  import { plainNumber } from "../lib/format";
  import { i18n } from "../lib/i18n";
  import { formatDuration, formatSeconds } from "../lib/time";
  import { wheelFactor } from "../lib/waveform";
  import { announceOpen } from "../lib/timeline/events";
  import { colorsOf, cssHeight, layoutOf, rowAtY, type Colors } from "../lib/timeline/layout";
  import { drawOverlay } from "../lib/timeline/overlay";
  import type { PickedItem } from "../lib/timeline/query";
  import { createRenderer, type TimelineRenderer } from "../lib/timeline/renderer";
  import { originOf, type TimelineSnapshot, type TimelineState } from "../lib/timeline/state";
  import { rowTitle } from "../lib/timeline/titles";

  let { tl, t, cursor = $bindable(null), renderer = $bindable("webgl2") }:
    { tl: TimelineState; t: TimelineSnapshot; cursor?: number | null; renderer?: "webgl2" | "canvas2d" } = $props();

  let box: HTMLDivElement;
  let plot: HTMLCanvasElement;
  let overlay: HTMLCanvasElement;
  let draw: TimelineRenderer | null = null;
  let colors: Colors | null = null;
  let cssWidth = $state(0);
  let dpr = $state(window.devicePixelRatio || 1);
  let drag: { x: number; moved: number } | null = null;
  let queued = 0;
  let hover = $state<{ x: number; y: number; item: PickedItem; row: string } | null>(null);
  let asking = false;
  let pending: { x: number; y: number } | null = null;

  const height = $derived(cssHeight(t.rows));
  const origin = $derived(originOf(t));
  const units = $derived(t.rows.map((r) => r.def.unit ?? null));
  const pointers = $derived(t.pointers.filter((p) => p.source === t.source));
  // ビューが置いた印（今の源のものを 1 つの昇順の並びに）
  const marks = $derived.by(() => {
    const sets = t.marks.filter((m) => m.source === t.source);
    return sets.length === 1 ? sets[0].marks : sets.flatMap((m) => m.marks).sort((a, b) => a.t - b.t);
  });
  // 説明の時刻の桁: 1 画素の時間（と 1 単位）を区別できるだけ
  const resolution = $derived(Math.max(t.tickS, ((t.view.end - t.view.start) * t.tickS) / Math.max(1, cssWidth)));

  $effect(() => {
    draw = createRenderer(plot);
    renderer = draw.kind;
    colors = colorsOf(box);
    const scheme = matchMedia("(prefers-color-scheme: dark)");
    const recolor = () => {
      colors = colorsOf(box);
      schedule();
    };
    scheme.addEventListener("change", recolor);
    return () => {
      scheme.removeEventListener("change", recolor);
      draw?.dispose();
    };
  });

  // 枠の幅に合わせて、間引きの幅（デバイスの画素）を決める
  $effect(() => {
    const ro = new ResizeObserver(([e]) => {
      cssWidth = Math.floor(e.contentRect.width);
    });
    ro.observe(box);
    const onResize = () => (dpr = window.devicePixelRatio || 1);    // 別のモニタへ移すと画素の密度が変わる
    window.addEventListener("resize", onResize);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", onResize);
    };
  });
  $effect(() => tl.setWidth(cssWidth * dpr));

  // 描くのは次の描画の機会に 1 回だけ（フレーム・カーソルが続けて変わってもまとめる）
  $effect(() => {
    void [t.frame, t.view, t.rows, t.selection, t.reference, t.selected, cursor, height, cssWidth, dpr, marks, pointers,
          origin];
    schedule();
  });

  function schedule() {
    if (!queued) queued = requestAnimationFrame(paint);
  }

  function paint() {
    queued = 0;
    if (!draw || !colors || !cssWidth) return;
    const layout = layoutOf(cssWidth, dpr, t.rows);
    draw.draw(t.frame, layout, colors);
    drawOverlay(overlay, layout, colors, {
      view: t.view, tickS: t.tickS, origin, resolution, frame: t.frame, units, marks, pointers,
      cursors: { selection: t.selection, mouse: cursor, reference: t.reference },
      selected: t.selected ? { row: t.selected.row, t: t.selected.t, tEnd: t.selected.tEnd } : null,
      trigger: t.source === "capture" ? (t.capture?.trigger_index ?? null) : null,
    });
  }

  onDestroy(() => cancelAnimationFrame(queued));

  const at = (x: number) => t.view.start + (x / cssWidth) * (t.view.end - t.view.start);
  const seconds = (time: number) => (time - origin) * t.tickS;

  /** (x, y) の段と、そこの出来事（logic・まとまりの見出しは null） */
  async function itemAt(x: number, y: number): Promise<{ row: string; item: PickedItem | null } | null> {
    const r = rowAtY(t.rows, y);
    if (r < 0) return null;
    const def = t.rows[r].def;
    if (def.type === "logic" || def.type === "group") return { row: def.id, item: null };
    return { row: def.id, item: await tl.pick(def.id, at(x), 4 * dpr) };
  }

  function onWheel(e: WheelEvent) {
    if (e.deltaY === 0 && e.deltaX === 0) return;
    e.preventDefault();
    if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) tl.panBy((e.deltaX || e.deltaY) * 0.002);
    else tl.zoomAt(e.offsetX / cssWidth, wheelFactor(e.deltaY));
  }

  function onDown(e: PointerEvent) {
    if (e.button !== 0) return;
    drag = { x: e.clientX, moved: 0 };
    box.setPointerCapture(e.pointerId);
    box.focus();
  }

  function onMove(e: PointerEvent) {
    cursor = Math.floor(at(e.offsetX));
    if (drag && cssWidth) {
      drag.moved += Math.abs(drag.x - e.clientX);
      tl.panBy((drag.x - e.clientX) / cssWidth);
      drag.x = e.clientX;
      hover = null;
      return;
    }
    ask(e.offsetX, e.offsetY);
  }

  /** ホバーの説明: 問い合わせは同時に 1 つ（動き続けても最後の位置で） */
  async function ask(x: number, y: number) {
    if (asking) {
      pending = { x, y };
      return;
    }
    asking = true;
    try {
      let next: { x: number; y: number } | null = { x, y };
      while (next) {
        pending = null;
        const found = await itemAt(next.x, next.y);
        hover = found?.item ? { x: next.x, y: next.y, item: found.item, row: found.row } : null;
        next = pending;
      }
    } finally {
      asking = false;
    }
  }

  async function onUp(e: PointerEvent) {
    const click = drag !== null && drag.moved < 4;
    drag = null;
    if (!click) return;
    const time = Math.floor(at(e.offsetX));
    if (e.shiftKey) {
      tl.setReference(time);
      return;
    }
    const found = await itemAt(e.offsetX, e.offsetY);
    const item = found?.item ?? null;
    tl.select(item ? item.t : time, item);
  }

  async function onDouble(e: MouseEvent) {
    const found = await itemAt(e.offsetX, e.offsetY);
    if (!announceOpen(found?.item ?? null, Math.floor(at(e.offsetX))) && !found?.item) tl.fit();
  }

  const step = (dir: 1 | -1) => void tl.seek(dir);

  /** ←/→ 1/10 画面（Shift で 1 画面）、Ctrl+←/→ または N/P 次・前のイベント、↑/+ 拡大、↓/− 縮小、Home/End 端、
   *  0/F 全体、R 基準（選択の位置、無ければマウス）、Enter 出来事の元へ、Esc 選択を外す */
  function onKey(e: KeyboardEvent) {
    const stepBy = e.shiftKey ? 1 : 0.1;
    const plain: Record<string, () => void> = {
      ArrowLeft: () => tl.panBy(-stepBy), ArrowRight: () => tl.panBy(stepBy),
      ArrowUp: () => tl.zoomAt(0.5, 0.5), "+": () => tl.zoomAt(0.5, 0.5), "=": () => tl.zoomAt(0.5, 0.5),
      ArrowDown: () => tl.zoomAt(0.5, 2), "-": () => tl.zoomAt(0.5, 2),
      Home: () => tl.toEdge(-1), End: () => tl.toEdge(1), "0": () => tl.fit(), f: () => tl.fit(), F: () => tl.fit(),
      n: () => step(1), N: () => step(1), p: () => step(-1), P: () => step(-1),
      r: () => tl.setReference(t.reference !== null && t.reference === (t.selection ?? cursor) ? null : (t.selection ?? cursor)),
      R: () => tl.setReference(null),
      Escape: () => tl.select(null),
      Enter: () => void announceOpen(t.selected, t.selection ?? 0),
    };
    const ctrl: Record<string, () => void> = { ArrowLeft: () => step(-1), ArrowRight: () => step(1) };
    const act = e.ctrlKey || e.metaKey ? ctrl[e.key] : e.altKey ? undefined : plain[e.key];
    if (!act) return;
    e.preventDefault();
    act();
  }

  const hoverRow = $derived(hover ? t.rows.find((r) => r.def.id === hover!.row)?.def : undefined);
  const value = (item: PickedItem) =>
    item.value === null ? "" : `${plainNumber(item.value)}${item.unit ? ` ${item.unit}` : ""}`;
</script>

<div class="timeline" bind:this={box} style:height="{height}px" tabindex="0" role="slider"
     aria-label={$i18n("timeline.keys")} aria-valuenow={t.view.start}
     aria-valuemin={t.extent?.[0] ?? 0} aria-valuemax={t.extent?.[1] ?? 0}
     aria-valuetext="{t.view.start}〜{t.view.end}"
     onwheel={onWheel} onpointerdown={onDown} onpointermove={onMove} onpointerup={onUp}
     onpointerleave={() => { cursor = null; drag = null; hover = null; }}
     ondblclick={onDouble} onkeydown={onKey}>
  <canvas class="plot" bind:this={plot} style:height="{height}px"></canvas>
  <canvas class="overlay" bind:this={overlay} style:height="{height}px"></canvas>
  {#if hover && hoverRow}
    {@const item = hover.item}
    <div class="tip" style:left="{Math.min(hover.x + 14, cssWidth - 260)}px" style:top="{hover.y + 16}px">
      <div class="title">{rowTitle(hoverRow, $i18n)}{item.label && item.label !== rowTitle(hoverRow, $i18n) ? ` · ${item.label}` : ""}</div>
      <div class="grid">
        <span>{$i18n("timeline.kind")}</span><span>{item.stream}.{item.kind}</span>
        <span>{$i18n("timeline.start")}</span><span>{formatSeconds(seconds(item.t), resolution)}</span>
        {#if item.tEnd > item.t}<span>{$i18n("timeline.duration")}</span><span>{formatDuration((item.tEnd - item.t) * t.tickS)}</span>{/if}
        {#if item.value !== null}<span>{$i18n("timeline.value")}</span><span>{value(item)}</span>{/if}
        {#if t.reference !== null}<span>{$i18n("timeline.reference")}</span><span>{$i18n("timeline.delta", { d: formatDuration((item.t - t.reference) * t.tickS) })}</span>{/if}
      </div>
      {#if item.jump && item.address !== null}
        <div class="hint">{$i18n("timeline.openHint", { where: item.jump === "source" ? $i18n("timeline.goToSource") : $i18n("timeline.showMemory") })}
          <span class="mono">0x{item.address.toString(16).toUpperCase().padStart(8, "0")}</span></div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .timeline { position: relative; min-width: 0; cursor: crosshair; touch-action: none; outline: none; }
  .timeline:focus-visible { box-shadow: inset 0 0 0 2px var(--accent); }
  canvas { display: block; width: 100%; position: absolute; inset: 0; }
  .tip { position: absolute; z-index: 3; pointer-events: none; min-width: 180px; max-width: 260px; padding: 6px 8px;
         background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 4px;
         box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18); font-size: 12px; }
  .title { font-weight: 600; margin-bottom: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .grid { display: grid; grid-template-columns: auto 1fr; gap: 1px 10px; }
  .grid span:nth-child(odd) { color: var(--muted); }
  .hint { margin-top: 4px; color: var(--muted); }
  .mono { font-family: ui-monospace, Consolas, monospace; }
</style>
