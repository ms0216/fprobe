<script lang="ts">
  // API の引数の欄を 1 行に並べる（仕様の fieldsOf から。DecoderFields と同じ形）: 必須の欄は空から始め、名前に赤い
  // アスタリスク。省ける欄は空 = 送らない（既定はデーモンが決める。既定の値は薄い字で見せるだけ）。真偽・列挙は選択
  import type { Field, FieldError, FieldValues } from "../../lib/apiform";
  import { defaultText } from "../../lib/crosstrigger";
  import { i18n, type Key } from "../../lib/i18n";

  let { fields, values = $bindable(), errors = {}, disabled = false }:
    { fields: Field[]; values: FieldValues; errors?: Record<string, FieldError>; disabled?: boolean } = $props();
  const options = (f: Field): unknown[] => (f.kind === "boolean" ? [true, false] : (f.options ?? []));
  const hint = (f: Field) => defaultText(f);
</script>

<span class="fields">
  {#each fields as f (f.name)}
    <label title={[f.required ? $i18n("logic.required") : $i18n("logic.defaultValue", { v: hint(f) ?? "—" }), f.description]
                    .filter(Boolean).join("\n")}>
      <span class="mono">{f.name}{#if f.required}<span class="ast" aria-hidden="true">*</span>{/if}</span>
      {#if f.kind === "boolean" || f.kind === "enum"}
        <select bind:value={values[f.name]} aria-required={f.required} aria-invalid={!!errors[f.name]} {disabled}>
          <option value="">{f.required ? "" : $i18n("api.defaultIs", { value: hint(f) ?? "—" })}</option>
          {#each options(f) as o (String(o))}<option value={String(o)}>{String(o)}</option>{/each}
        </select>
      {:else}
        <input class="mono" bind:value={values[f.name]} size={f.kind === "string" ? 22 : 11} aria-required={f.required}
               aria-invalid={!!errors[f.name]} placeholder={f.required ? "" : hint(f) ?? ""} {disabled} />
      {/if}
      {#if errors[f.name]}<span class="err" role="alert">{$i18n(`api.err.${errors[f.name].code}` as Key,
                                                                 { limit: String(errors[f.name].limit ?? "") })}</span>{/if}
    </label>
  {/each}
</span>

<style>
  .fields { display: inline-flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  label { display: inline-flex; gap: 3px; align-items: center; font-size: 12.5px; }
  .ast { color: var(--bad); margin-left: 1px; }
  .err { color: var(--bad); font-size: 11.5px; }
</style>
