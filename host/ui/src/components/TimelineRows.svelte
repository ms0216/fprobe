<script lang="ts">
  import type { LaChannel } from "../lib/waveform";
  import { shared } from "../lib/context";
  // 時間軸の段の名前の列: まとまりの見出し（クリックで折りたたみ・展開、件数）と段の名前（マウスの縦線の値: 波形は
  // フレームから、値・状態の段はタイルへの問い合わせで。lib/timeline/cursor.ts）。
  // 名前をドラッグして並べ替え（段はまとまりの中、見出しはまとまりごと）、× で段を隠す。高さは描画と同じ（rows.ts）。
  import { i18n } from "../lib/i18n";
  import { levelAt } from "../lib/waveform";
  import { CursorProbe, cursorText, type CursorValues } from "../lib/timeline/cursor";
  import { CATEGORICAL, RULER } from "../lib/timeline/layout";
  import { ROW_HEIGHT, type VisibleRow } from "../lib/timeline/rows";
  import type { TimelineSnapshot, TimelineState } from "../lib/timeline/state";
  import { groupTitle, rowTitle } from "../lib/timeline/titles";

  let { tl, t, cursor }: { tl: TimelineState; t: TimelineSnapshot; cursor: number | null } = $props();

  let dragging = $state<VisibleRow | null>(null);
  let over = $state<string | null>(null);

  const visibleCount = (id: string) => {
    const i = t.frame?.ids.indexOf(id) ?? -1;
    return i >= 0 ? (t.frame!.visible[i] ?? 0) : 0;
  };
  // 値・状態の段の縦線の値（問い合わせは同時に 1 つ、最後の位置で）。段・タイルが変わったら問い合わせ直す
  let values = $state<CursorValues>({});
  const probe = new CursorProbe((at) => tl.valuesAt(at), (v) => (values = v));
  $effect(() => {
    void [t.rows, t.header];
    probe.ask(cursor);
  });
  const value = (r: VisibleRow) => {
    if (cursor === null) return "";
    return r.def.type === "logic" ? (levelAt(t.frame, r.def.id, cursor) ?? "") : cursorText(values[r.def.id]);
  };

  function drop(target: VisibleRow) {
    const from = dragging;
    dragging = null;
    over = null;
    if (!from || from === target) return;
    if (from.header) {
      tl.moveGroup(from.def.group, target.def.group);
    } else if (!target.header && target.def.group === from.def.group) {
      tl.moveRow(from.def.id, target.def.id);
    } else if (target.header && target.def.group === from.def.group) {
      const first = t.groups.find((g) => g.id === from.def.group)?.rows[0]?.id ?? null;
      tl.moveRow(from.def.id, first);
    }
  }
  const { app } = shared();
  const laLayout = $derived((($app.probe?.capabilities ?? {}) as { la_layout?: LaChannel[] }).la_layout ?? null);
</script>

<div class="names" style:padding-top="{RULER}px">
  {#each t.rows as r (r.def.id)}
    <div class="name" class:header={r.header} class:over={over === r.def.id}
         class:selected={t.selected?.row === r.def.id} style:height="{ROW_HEIGHT[r.def.type]}px"
         draggable="true" role="listitem"
         ondragstart={(e) => { dragging = r; e.dataTransfer?.setData("text/plain", r.def.id); }}
         ondragover={(e) => { if (dragging) { e.preventDefault(); over = r.def.id; } }}
         ondragleave={() => (over = over === r.def.id ? null : over)}
         ondrop={(e) => { e.preventDefault(); drop(r); }}
         ondragend={() => { dragging = null; over = null; }}>
      {#if r.header}
        <button class="fold" title={r.folded ? $i18n("timeline.unfold") : $i18n("timeline.fold")}
                aria-expanded={!r.folded} onclick={() => tl.toggleFold(r.def.group)}>
          <span class="chev">{r.folded ? "▸" : "▾"}</span>{groupTitle(r.def.group, $i18n)}</button>
        <span class="mono muted">{r.def.group === "logic" ? r.count : visibleCount(r.def.id).toLocaleString()}</span>
      {:else}
        <span class="swatch" style:background={CATEGORICAL[r.def.color]} class:none={r.def.type === "logic"}></span>
        <span class="title" title={`${rowTitle(r.def, $i18n, laLayout)} (${r.def.id})`}>{rowTitle(r.def, $i18n, laLayout)}</span>
        <span class="mono v" title={value(r)}>{value(r)}</span>
        <button class="hide" title={$i18n("timeline.hideRow")} aria-label={$i18n("timeline.hideRow")}
                onclick={() => tl.setRowShown(r.def.id, false)}>×</button>
      {/if}
    </div>
  {/each}
</div>

<style>
  .names { border-right: 1px solid var(--line); user-select: none; }
  .name { display: flex; align-items: center; gap: 5px; padding: 0 6px 0 14px; font-size: 12px; box-sizing: border-box;
          border-bottom: 1px solid color-mix(in srgb, var(--line) 60%, transparent); cursor: grab; }
  .name.header { padding-left: 2px; background: color-mix(in srgb, var(--line) 35%, transparent); font-weight: 600; }
  .name.over { box-shadow: inset 0 2px 0 var(--accent); }
  .name.selected { background: var(--select); }
  .fold { all: unset; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
          cursor: pointer; font-size: 12px; }
  .chev { display: inline-block; width: 14px; color: var(--muted); }
  .swatch { flex: none; width: 8px; height: 8px; border-radius: 2px; }
  .swatch.none { visibility: hidden; }
  .title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .v { color: var(--warn); max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hide { all: unset; visibility: hidden; color: var(--muted); cursor: pointer; padding: 0 2px; }
  .name:hover .hide { visibility: visible; }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size: 11px; }
  .muted { color: var(--muted); font-weight: normal; }
</style>
