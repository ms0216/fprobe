<script lang="ts">
  // 解読器の設定の欄（la.decoders の params から）: チャンネルの設定は ch の選択、ほかは文字（空 = 既定）。
  // 積み重ねる解読器（Modbus・SPI Flash 等）は下の解読の選択と、その欄を入れ子に（このコンポーネント自身）
  import { i18n } from "../../lib/i18n";
  import { isChannelParam, newForm, type DecoderForm, type DecoderInfo } from "../../lib/logic";
  import { channelName } from "../../lib/waveform";
  import Self from "./DecoderFields.svelte";

  let { form = $bindable(), decoders, channels }: { form: DecoderForm; decoders: DecoderInfo[]; channels: number[] } = $props();
  const info = $derived(decoders.find((d) => d.name === form.protocol));
  const shown = (v: unknown) => (v === null || v === undefined ? "" : String(v));
</script>

<span class="fields">
  {#each info?.params ?? [] as p (p.name)}
    <label title={p.required ? $i18n("logic.required") : $i18n("logic.defaultValue", { v: shown(p.default) || "—" })}>
      <span class:req={p.required}>{p.name}</span>
      {#if isChannelParam(p.name)}
        <select bind:value={form.values[p.name]}>
          {#if !p.required}<option value="">—</option>{/if}
          {#each channels as ch (ch)}<option value={String(ch)}>{channelName(ch)}</option>{/each}
        </select>
      {:else}
        <input class="mono" bind:value={form.values[p.name]} placeholder={shown(p.default)} size="8" />
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
  .req { font-weight: 600; }
  .lower { display: inline-flex; gap: 6px; flex-wrap: wrap; align-items: center; padding-left: 6px;
           border-left: 2px solid var(--line); }
</style>
