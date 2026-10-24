<script lang="ts">
  import type { LaChannel } from "../../lib/waveform";
  import { shared } from "../../lib/context";
  // 解読器の設定の欄（la.decoders の params から）: チャンネルの設定は ch の選択、ほかは文字。必須の欄（通信の設定は
  // 既定を持たない）は空から始め、使う人が埋める（空なら解読を呼べない）。null を渡せる線は「つながない」を選べる。
  // 省ける欄は空 = 解読器の既定。
  // 積み重ねる解読器（Modbus・SPI Flash 等）は下の解読の選択と、その欄を入れ子に（このコンポーネント自身）
  import { i18n } from "../../lib/i18n";
  import { isChannelParam, newForm, type DecoderForm, type DecoderInfo } from "../../lib/logic";
  import { channelName } from "../../lib/waveform";
  import Self from "./DecoderFields.svelte";

  let { form = $bindable(), decoders, channels }: { form: DecoderForm; decoders: DecoderInfo[]; channels: number[] } = $props();
  const info = $derived(decoders.find((d) => d.name === form.protocol));
  const shown = (v: unknown) => (v === null || v === undefined ? "" : String(v));
  const { app } = shared();
  const laLayout = $derived((($app.probe?.capabilities ?? {}) as { la_layout?: LaChannel[] }).la_layout ?? null);
</script>

<span class="fields">
  {#each info?.params ?? [] as p (p.name)}
    <label title={p.required ? $i18n("logic.required") : $i18n("logic.defaultValue", { v: shown(p.default) || "—" })}>
      <span>{p.name}{#if p.required}<span class="ast" aria-hidden="true">*</span>{/if}</span>
      {#if isChannelParam(p)}
        <select bind:value={form.values[p.name]} aria-required={p.required}>
          <option value=""></option>
          {#if p.required && p.nullable}<option value="null">{$i18n("logic.notConnected")}</option>{/if}
          {#each channels as ch (ch)}<option value={String(ch)}>{channelName(ch, laLayout)}</option>{/each}
        </select>
      {:else}
        <input class="mono" bind:value={form.values[p.name]} size="8" aria-required={p.required}
               placeholder={p.required ? "" : shown(p.default)} />
      {/if}
    </label>
  {/each}
  {#if info?.lower.length && form.lower}
    <span class="lower">
      <label>{$i18n("logic.lower")}
        <select value={form.lower.protocol}
                onchange={(e) => (form.lower = newForm((e.currentTarget as HTMLSelectElement).value, decoders))}>
          {#each info.lower as l (l)}<option value={l}>{l}</option>{/each}
        </select>
      </label>
      <Self bind:form={form.lower} {decoders} {channels} />
    </span>
  {/if}
</span>

<style>
  .fields { display: inline-flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  label { display: inline-flex; gap: 3px; align-items: center; font-size: 12.5px; }
  .ast { color: var(--bad); margin-left: 1px; }
  .lower { display: inline-flex; gap: 6px; flex-wrap: wrap; align-items: center; padding-left: 6px;
           border-left: 2px solid var(--line); }
</style>
