<script lang="ts">
  // クロストリガーの経路の表: 行 = 源、列 = 行き先（ロジアナの取り込み・割り込み書き込み・設定済みの出力の端子）と
  // 回数・最後の時刻。セルは機器の事実（xtrig.status の route・xtrig.output_status の sources）で、書き換えた間は
  // 親が持つ下書き。適用・戻すは親（API を呼ぶ）。表示だけ
  import {
    SOURCES, cellBlock, routeChanged, sourceFacts, type Avail, type Block, type Route, type RouteColumn, type Statuses,
    type TriggerSource, type OutputColumn, type Action,
  } from "../../../lib/crosstrigger";
  import { i18n } from "../../../lib/i18n";
  import { ACTION_LABEL, COLUMN_ACTION, ROUTE_COLUMNS, SOURCE_API, SOURCE_LABEL, capText } from "../../../lib/xtlabels";
  import Hint from "../Hint.svelte";
  import XtFact from "./XtFact.svelte";

  interface Props {
    srcOk: Record<TriggerSource, Avail>;
    actOk: Record<Action, Avail>;
    columns: OutputColumn[];
    facts: (Route & { unknown: string[] }) | null;
    xtrigRead: boolean;                          // xtrig.status を読めている（route が無いとき注記を出す）
    outputsRead: boolean;                        // xtrig.output_status を読めている
    draft: Route;
    outDraft: Record<number, TriggerSource[]>;
    statuses: Statuses;
    edited: boolean;
    routeError: string | null;
    readError: string | undefined;
    outChanged: (channel: number, s: TriggerSource) => boolean;
    ontoggle: (column: RouteColumn, s: TriggerSource) => void;
    ontoggleOut: (channel: number, s: TriggerSource) => void;
    onapply: () => void;
    onrevert: () => void;
  }
  let { srcOk, actOk, columns, facts, xtrigRead, outputsRead, draft, outDraft, statuses, edited, routeError, readError,
        outChanged, ontoggle, ontoggleOut, onapply, onrevert }: Props = $props();

  const absent = (a: Avail) => (a.ok ? null : $i18n("xt.absent", { cap: capText(a) }));
  const blockText = (b: Block) => (b.kind === "self" ? $i18n("xt.self")
    : $i18n(b.kind === "source" ? "xt.absentSource" : "xt.absentAction", { cap: capText(b) }));
</script>

<section>
  <h3>{$i18n("xt.route")} <Hint text={$i18n("xt.routeTip")} /></h3>
  <div class="scroll">
    <table class="grid route">
      <thead>
        <tr>
          <th class="left">{$i18n("xt.source")}</th>
          {#each ROUTE_COLUMNS as c (c)}
            {@const a = actOk[COLUMN_ACTION[c]]}
            <th class:gone={!a.ok} title={absent(a) ?? (c === "to_la" ? $i18n("xt.toLaTip") : $i18n("logic.toInjectTip"))}>
              {$i18n(ACTION_LABEL[COLUMN_ACTION[c]])}<div class="mono small muted">{c}</div></th>
          {/each}
          {#each columns as col (col.channel)}
            <th title={col.editable ? $i18n("xt.outTip") : $i18n("xt.outErr.pulse", { ch: col.channel })}>
              {$i18n("xt.outCh", { ch: col.channel })}</th>
          {/each}
          <th>{$i18n("xt.count")}</th>
          <th>{$i18n("xt.lastTime")}</th>
        </tr>
      </thead>
      <tbody>
        {#each SOURCES as s (s)}
          {@const ok = srcOk[s]}
          {@const f = sourceFacts(s, statuses)}
          <tr class:gone={!ok.ok}>
            <td class="left" title={ok.ok ? `${s} (${SOURCE_API[s]})` : `${s} (${SOURCE_API[s]}) — ${absent(ok)}`}>
              {$i18n(SOURCE_LABEL[s])}
              {#if !ok.ok}<span class="small reason" aria-hidden="true"> ⓘ</span>{/if}
            </td>
            {#each ROUTE_COLUMNS as c (c)}
              {@const b = cellBlock(s, c, srcOk, actOk)}
              <td class:changed={routeChanged(facts, draft, c, s)} title={b ? blockText(b) : ""}>
                {#if b?.kind === "self"}<span class="muted">·</span>{:else}
                <input type="checkbox" checked={draft[c].includes(s)} disabled={!!b || !facts}
                       onchange={() => ontoggle(c, s)} aria-label="{c} {s}" />{/if}
              </td>
            {/each}
            {#each columns as col (col.channel)}
              <td class:changed={outChanged(col.channel, s)}>
                <input type="checkbox" checked={(outDraft[col.channel] ?? []).includes(s)}
                       disabled={!col.editable || !ok.ok} onchange={() => ontoggleOut(col.channel, s)}
                       aria-label="ch{col.channel} {s}" />
              </td>
            {/each}
            <td><XtFact kind="count" value={f.count} /></td>
            <td>{#if f.laTimes}
              {#if f.laTimes.internal === null && f.laTimes.deep === null}<span class="muted">—</span>{/if}
              {#if f.laTimes.internal !== null}<span class="small muted">{$i18n("xt.internal")}</span>
                <XtFact kind="time" value={f.laTimes.internal} />{/if}
              {#if f.laTimes.deep !== null}<span class="small muted">{$i18n("xt.deep")}</span>
                <XtFact kind="time" value={f.laTimes.deep} />{/if}
            {:else}<XtFact kind="time" value={f.time} />{/if}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
  {#if xtrigRead && !facts}<div class="muted small">{$i18n("xt.noRouteFacts")}</div>{/if}
  {#if facts?.unknown.length}<div class="muted small">{$i18n("xt.unknownSources", { names: facts.unknown.join(", ") })}</div>{/if}
  {#if actOk.outputs.ok && outputsRead && !columns.length}<div class="muted small">{$i18n("xt.noOutputColumns")}</div>{/if}
  <div class="bar">
    <button class="primary" disabled={!edited} onclick={onapply}>{$i18n("logic.apply")}</button>
    <button disabled={!edited} onclick={onrevert}>{$i18n("xt.revert")}</button>
    {#if edited}<span class="muted small">{$i18n("xt.editing")}</span>{/if}
    {#if routeError}<span class="error">{routeError}</span>{/if}
    {#if readError}<span class="error">{readError}</span>{/if}
  </div>
</section>

<style>
  section { min-width: 0; display: flex; flex-direction: column; gap: 6px; }
  h3 { font-size: 12px; color: var(--muted); font-weight: 600; margin: 2px 0 0; display: flex; gap: 8px; align-items: baseline; }
  .scroll { overflow-x: auto; }
  .route { width: auto; }
  .route td, .route th { text-align: center; }
  .route .left { text-align: left; }
  td.changed { background: color-mix(in srgb, var(--accent) 18%, transparent); }
  .gone { opacity: 0.55; }
  .reason { color: var(--muted); font-style: italic; }
  .bar { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .small { font-size: 11.5px; }
  .error { color: var(--bad); font-size: 12px; }
</style>
