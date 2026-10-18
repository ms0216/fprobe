<script lang="ts">
  // DAP サンプラ（変数を止めずに読む回路。1 つだけ）をほかのパネル・利用者が使っているときの知らせ: どのパネル・誰が
  // 使っているか、そのパネルを開く・止める操作。mine はこのパネルが使う道具（同じ道具なら出さない）
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { onCalls } from "../../lib/notify";
  import { blockedBy, holderOf, readSampler, SAMPLER_USERS, type SamplerStatus, type SamplerUser } from "../../lib/sampler";

  let { mine }: { mine: SamplerUser } = $props();
  const { app, conn, dock } = shared();
  let status = $state<SamplerStatus | null>(null);

  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");
  const holder = $derived(blockedBy(holderOf(status, $app.activity), mine));

  async function refresh() {
    status = await readSampler(conn);
  }

  $effect(() => {
    if (!connected) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 3000);
    return () => clearInterval(timer);
  });
  // サンプラを使い始める・やめる操作が誰か（AI も）から流れたら読み直す
  const STARTS = Object.values(SAMPLER_USERS).map((u) => u.start);
  $effect(() => onCalls(conn, [...STARTS, "watch.stop", "rtos.timeline.stop", "profile.stop", "rtt.stop"],
                        () => void refresh()));

  async function release() {
    if (!holder?.release || !(await allowed(app))) return;
    await app.act(holder.release as any);
    await refresh();
  }
</script>

{#if holder}
  <div class="sampler-busy" role="status">
    <span>⚠ {holder.client
      ? $i18n("sampler.busyBy", { panel: $i18n(holder.title), who: holder.client })
      : $i18n("sampler.busy", { panel: $i18n(holder.title) })}</span>
    <span class="muted">{$i18n("sampler.busyHint")}</span>
    <span class="spacer"></span>
    {#if holder.panel}
      <button class="small" onclick={() => dock.show(holder.panel!)}>{$i18n("sampler.open", { panel: $i18n(holder.title) })}</button>
    {/if}
    {#if holder.release}
      <button class="small" onclick={release}>{$i18n("sampler.stop", { panel: $i18n(holder.title) })}</button>
    {/if}
  </div>
{/if}

<style>
  .sampler-busy { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; padding: 4px 8px; font-size: 12px;
                  color: var(--warn); border-bottom: 1px solid var(--line); }
  .muted { color: var(--muted); }
  .spacer { flex: 1; }
</style>
