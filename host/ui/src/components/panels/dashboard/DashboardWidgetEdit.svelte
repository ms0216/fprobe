<script lang="ts">
  // Dashboard の部品の編集欄（編集中だけ部品の下に出る）: 種類・式・名前・範囲・単位・桁・大きさ・並べ替え・削除。
  // 配置の更新は親（patch・並べ替え・削除）。表示だけ
  import { KINDS, MAX_H, MAX_W, type Widget, type WidgetKind } from "../../../lib/dashboard";
  import { i18n, type Key } from "../../../lib/i18n";

  interface Props {
    w: Widget;
    first: boolean;
    last: boolean;
    patch: (id: string, p: Partial<Omit<Widget, "id">>) => void;
    onmove: (dir: -1 | 1) => void;
    onremove: () => void;
  }
  let { w, first, last, patch, onmove, onremove }: Props = $props();
</script>

<div class="edit">
  <select value={w.kind} aria-label={$i18n("dashboard.addWidget")}
          onchange={(e) => patch(w.id, { kind: e.currentTarget.value as WidgetKind })}>
    {#each KINDS as k (k)}<option value={k}>{$i18n(`dashboard.kind.${k}` as Key)}</option>{/each}
  </select>
  <input class="mono" placeholder={$i18n("dashboard.expression")} aria-label={$i18n("dashboard.expression")}
         value={w.expr} onchange={(e) => patch(w.id, { expr: e.currentTarget.value.trim() })} />
  <input placeholder={$i18n("dashboard.label")} aria-label={$i18n("dashboard.label")} value={w.label}
         onchange={(e) => patch(w.id, { label: e.currentTarget.value })} />
  {#if w.kind !== "button" && w.kind !== "toggle" && w.kind !== "lamp"}
    <label>{$i18n("dashboard.min")}<input type="number" value={w.min}
           onchange={(e) => patch(w.id, { min: Number(e.currentTarget.value) })} /></label>
    <label>{$i18n("dashboard.max")}<input type="number" value={w.max}
           onchange={(e) => { const max = Number(e.currentTarget.value); if (max > w.min) patch(w.id, { max }); }} /></label>
    <label>{$i18n("dashboard.unit")}<input value={w.unit} onchange={(e) => patch(w.id, { unit: e.currentTarget.value })} /></label>
    <label>{$i18n("dashboard.digits")}<input type="number" min="0" max="6" value={w.digits}
           onchange={(e) => patch(w.id, { digits: Number(e.currentTarget.value) })} /></label>
  {/if}
  {#if w.kind === "button"}
    <label title={$i18n("dashboard.valueTip")}>{$i18n("dashboard.value")}<input class="mono" value={w.value}
           onchange={(e) => patch(w.id, { value: e.currentTarget.value })} /></label>
  {/if}
  <label>{$i18n("dashboard.width")}<select value={w.w} onchange={(e) => patch(w.id, { w: Number(e.currentTarget.value) })}>
    {#each Array.from({ length: MAX_W }, (_, k) => k + 1) as n (n)}<option value={n}>{n}</option>{/each}</select></label>
  <label>{$i18n("dashboard.height")}<select value={w.h} onchange={(e) => patch(w.id, { h: Number(e.currentTarget.value) })}>
    {#each Array.from({ length: MAX_H }, (_, k) => k + 1) as n (n)}<option value={n}>{n}</option>{/each}</select></label>
  <span class="ops">
    <button class="small" disabled={first} title={$i18n("dashboard.moveUp")} aria-label={$i18n("dashboard.moveUp")}
            onclick={() => onmove(-1)}>‹</button>
    <button class="small" disabled={last} title={$i18n("dashboard.moveDown")}
            aria-label={$i18n("dashboard.moveDown")} onclick={() => onmove(1)}>›</button>
    <button class="small" title={$i18n("common.remove")} aria-label={$i18n("common.remove")}
            onclick={onremove}>×</button>
  </span>
</div>

<style>
  .small { font-size: 12px; }
  .edit { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; border-top: 1px dashed var(--line); padding-top: 6px;
          font-size: 12px; }
  .edit input, .edit select { font-size: 12px; max-width: 100%; }
  .edit input:not([type]) { flex: 1 1 100px; }
  .edit input[type="number"] { width: 64px; }
  .edit label { display: flex; gap: 3px; align-items: center; }
  .ops { margin-left: auto; display: flex; gap: 3px; }
</style>
