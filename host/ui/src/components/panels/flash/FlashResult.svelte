<script lang="ts">
  // Flash の結果の欄（書き込みの段ごとの時間・速度・消去・読み出し・ベリファイ）。応答の値をそのまま描く
  import { i18n, type Key } from "../../../lib/i18n";
  import { baseName, eraseMethodKey, programStages, seconds, size, speed, type FlashResultState, type Stage } from "../../../lib/flash";
  import { hex32 } from "../format";

  let { result, path }: { result: FlashResultState; path: string } = $props();

  const stages = $derived<Stage[]>(result.kind === "program" ? programStages(result.data) : []);
  const verify = $derived(result.kind === "program" ? result.data?.verify : result.kind === "verify" ? result.data : null);

  /** 消去の方法の名前（知らない方法はそのまま） */
  const methodText = (m: string | undefined) => {
    const k = eraseMethodKey(m);
    return k ? $i18n(k) : m ?? "";
  };
  /** 消したセクタの数（セクタごとの消去なら数だけ、チップ全体等なら方法も） */
  const sectorsNote = (n: number, method: string | undefined) => !method || method === "sector"
    ? $i18n("flash.sectorsOnly", { n }) : $i18n("flash.sectorsNote", { n, method: methodText(method) });
  const stageNote = (s: Stage) => s.sectors !== undefined ? sectorsNote(s.sectors, s.method)
    : s.skipped ? $i18n("flash.skippedNote", { size: size(s.skipped) }) : "";
</script>

<section class="result">
  {#if result.kind === "program"}
    <div class="line mono">{baseName(result.data.image?.source ?? path)} · {result.data.image?.format?.toUpperCase()} ·
      {size(result.data.image?.bytes)}</div>
    <table class="grid">
      <thead><tr><th>{$i18n("flash.stage")}</th><th>{$i18n("flash.size")}</th><th>{$i18n("flash.time")}</th>
        <th>{$i18n("flash.speed")}</th><th></th></tr></thead>
      <tbody>
        {#each stages as s (s.stage)}
          <tr class:total={s.stage === "total"}>
            <td>{$i18n(`flash.stage.${s.stage}` as Key)}</td>
            <td class="mono">{size(s.bytes)}</td><td class="mono">{s.stage === "ram" ? "" : seconds(s.seconds)}</td>
            <td class="mono">{speed(s.bps)}</td><td class="muted">{stageNote(s)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else if result.kind === "erase"}
    <div class="line">
      {#if result.data.start !== undefined}
        {$i18n("flash.erasedRange", { start: hex32(result.data.start), end: hex32(result.data.end) })}
        · {sectorsNote(result.data.sectors, "sector")}
      {:else}
        {$i18n("flash.erasedAll", { method: methodText(result.data.method) })}
      {/if}
      {#if result.data.rate}· <span class="mono">{seconds(result.data.rate.seconds)} · {speed(result.data.rate.bytes_per_s)}</span>{/if}
      {#if result.data.seconds !== undefined}· <span class="mono">{seconds(result.data.seconds)}</span>{/if}
    </div>
  {:else if result.kind === "read"}
    <div class="line">{$i18n("flash.readDone", { path: result.data.path, size: size(result.data.bytes),
                                                  crc: hex32(result.data.crc32) })}
      · <span class="mono">{hex32(result.data.address)} · {seconds(result.data.read?.seconds)} · {speed(result.data.read?.bytes_per_s)}</span></div>
  {/if}
  {#if verify}
    {#if verify.match}
      <div class="line ok">{$i18n("flash.verifyOk")} · <span class="mono">{size(verify.bytes)} · {speed(verify.read?.bytes_per_s)}</span></div>
    {:else}
      <div class="line bad">{$i18n("flash.verifyBad", { n: verify.differing_bytes })}</div>
      <table class="grid">
        <thead><tr><th>{$i18n("common.address")}</th><th>{$i18n("flash.expected")}</th><th>{$i18n("flash.actual")}</th></tr></thead>
        <tbody>
          {#each verify.differences as d (d.address)}
            <tr><td class="mono">{hex32(d.address)}</td><td class="mono hexbytes">{d.expected}</td>
              <td class="mono hexbytes bad">{d.actual}</td></tr>
          {/each}
        </tbody>
      </table>
      {#if verify.more_differences}
        <div class="line muted">{$i18n("flash.more", { n: verify.more_differences })}</div>
      {/if}
    {/if}
  {/if}
</section>

<style>
  .line { padding: 4px 10px; }
  .result { border-bottom: 1px solid var(--line); padding-bottom: 4px; }
  tr.total td { font-weight: 600; }
  .ok { color: var(--ok); }
  .bad { color: var(--bad); }
  .hexbytes { word-break: break-all; }
</style>
