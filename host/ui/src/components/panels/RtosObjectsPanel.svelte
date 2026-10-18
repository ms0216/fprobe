<script lang="ts">
  // RTOS Objects（Ozone の RTOS 窓・VS Code Embedded Tools の RTOS ビューア・Keil の RTX の型）: カーネルのオブジェクト
  // （キュー・セマフォ・ミューテックス・タイマ・イベントグループ・ストリーム / メッセージバッファ）と待っているタスク、
  // ヒープの残量、優先度の逆転・デッドロックの警告。止まるたびに読み、走っている間は 1 秒ごとに走らせたまま読む
  // （Threads と同じ。止めると壊れるターゲットでも見られる。一貫しない旨を出す）。
  // オブジェクトの名前 → Memory、タイマの関数 → Source・Disassembly
  import { errorText } from "../../lib/api";
  import { isRunning } from "../../lib/commands";
  import { shared } from "../../lib/context";
  import { i18n, type Key } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import { SymbolAddresses } from "../../lib/profile";
  import { waiterText } from "../../lib/rtos";
  import Empty from "./Empty.svelte";
  import TabBar from "./TabBar.svelte";
  import { hex32 } from "./format";

  const { app, conn, showMemory, showAddress } = shared();
  const KINDS = ["all", "queue", "semaphore", "mutex", "timer", "event_group", "buffer", "other"] as const;
  type Kind = (typeof KINDS)[number];
  const symbols = new SymbolAddresses(conn);

  let info = $state<any>(null);
  let error = $state<string | null>(null);
  let kind = $state<Kind>(load("rtosObjects.kind", "all") as Kind);
  $effect(() => save("rtosObjects.kind", kind));

  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");
  const halted = $derived($app.target?.state === "HALTED");
  const running = $derived(isRunning($app.target?.state));
  const key = $derived(halted ? `${$app.target?.pc}/${$app.target?.run}` : "");

  $effect(() => {
    if (key) void read();
  });
  // 走っている間は 1 秒ごとに走らせたまま読む
  $effect(() => {
    if (!running) return;
    void read();
    const timer = setInterval(() => void read(), 1000);
    return () => clearInterval(timer);
  });

  async function read() {
    try {
      info = await conn.call("rtos.objects", { live: !halted }, { quiet: true });
      error = null;
    } catch (e) {
      info = null;
      error = errorText(e);
    }
  }

  const familyOf = (k: string): Kind => (k.endsWith("queue") || k === "k_msgq" ? "queue"
    : k.endsWith("semaphore") || k === "k_sem" ? "semaphore" : k.endsWith("mutex") ? "mutex"
    : k.endsWith("timer") ? "timer" : k.endsWith("event_group") || k === "k_event" ? "event_group"
    : k.endsWith("buffer") ? "buffer" : "other");
  const objects = $derived((info?.objects ?? []).filter((o: any) => kind === "all" || familyOf(o.kind) === kind));
  const counts = $derived(Object.fromEntries(KINDS.map((k) => [k, (info?.objects ?? [])
    .filter((o: any) => k === "all" || familyOf(o.kind) === k).length])));

  function typeLabel(k: string): string {
    const key = `objects.type.${k}` as Key;
    const s = $i18n(key);
    return s === key ? k : s;
  }

  /** 種類ごとの中身の短い表し方 */
  function describe(o: any): string {
    const f = o.fields ?? {};
    const tr = $i18n;
    if ("messages" in f) return f.free === 0 ? `${tr("objects.f.messages", { n: f.messages, length: f.length })} · ${tr("objects.f.full")}`
      : tr("objects.f.messages", { n: f.messages, length: f.length });
    if ("count" in f) return tr("objects.f.count", { n: f.count, max: f.max ?? f.limit ?? "?" });
    if ("holder" in f) {
      if (!f.holder) return tr("objects.f.available");
      const own = tr("objects.f.owner", { task: f.holder.task });
      return f.holder.inherited ? `${own} (${tr("objects.f.inherited", { p: f.holder.priority })})` : own;
    }
    if ("period_ticks" in f) {
      const parts = [tr("objects.f.period", { n: f.period_ticks }), f.auto_reload ? tr("objects.f.autoReload") : tr("objects.f.oneShot"),
                     f.active ? (f.remaining_ticks !== undefined ? tr("objects.f.remaining", { n: f.remaining_ticks }) : "")
                              : tr("objects.f.dormant")];
      return parts.filter(Boolean).join(" · ");
    }
    if ("bits" in f) return tr("objects.f.bits", { bits: (f.bits >>> 0).toString(16).toUpperCase() });
    if ("capacity" in f) return tr("objects.f.bytes", { used: f.used, capacity: f.capacity });
    return Object.entries(f).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
  }

  async function openCallback(name: string) {
    const address = await symbols.of(name);
    if (address !== null) showAddress(address);
  }

</script>

<div class="pane objects">
  {#if info}
    <div class="pane-bar">
      <TabBar tabs={KINDS.filter((k) => k === "all" || counts[k])
                     .map((k) => ({ id: k, label: $i18n(`objects.kind.${k}` as Key), count: counts[k] }))} bind:value={kind} />
      <span class="spacer"></span>
      {#if running}<span class="muted live" title={$i18n("objects.liveTip")}>● {$i18n("objects.live")}</span>{/if}
      <button class="small" disabled={!connected} onclick={read}>{$i18n("common.refresh")}</button>
    </div>
    {#if !info.consistent}<div class="banner warn">{$i18n("objects.inconsistent")}</div>{/if}
    {#each info.contention?.inversions ?? [] as inv (inv.handle + "/" + inv.waiter.handle)}
      <div class="banner bad">
        ⚠ {$i18n("objects.inversion", { waiter: inv.waiter.task, wp: inv.waiter.priority, mutex: inv.mutex,
                                         holder: inv.holder.task, hp: inv.holder.priority })}
        {#if !inv.inherited}· {$i18n("objects.noInheritance")}{/if}
        {#if inv.interferers.length}· {$i18n("objects.interferers", { tasks: inv.interferers.map((t: any) => t.task).join(", ") })}{/if}
      </div>
    {/each}
    {#each info.contention?.deadlocks ?? [] as cycle, i (i)}
      <div class="banner bad">⚠ {$i18n("objects.deadlock", { cycle: cycle.map((c: any) => `${c.task} → ${c.waits_for} (${c.held_by})`).join(" → ") })}</div>
    {/each}
    {#if info.heap}
      <div class="heap">
        <span>{$i18n("objects.heap", { scheme: info.heap.scheme, free: info.heap.free ?? "?", total: info.heap.total ?? "?",
                                        min: info.heap.min_free ?? "?" })}</span>
        {#if info.heap.total}
          <span class="bar"><span style:width="{(100 * (info.heap.used ?? 0)) / info.heap.total}%"></span>
            <i style:left="{(100 * (info.heap.peak_used ?? 0)) / info.heap.total}%"></i></span>
        {/if}
      </div>
    {/if}
    {#if objects.length}
      <table class="grid">
        <thead><tr><th>{$i18n("objects.name")}</th><th>{$i18n("objects.type")}</th><th>{$i18n("objects.state")}</th>
          <th>{$i18n("objects.waiting")}</th></tr></thead>
        <tbody>
          {#each objects as o (o.handle)}
            <tr>
              <td class="mono">
                <button class="link" title={$i18n("objects.memoryTip")} onclick={() => showMemory(o.handle)}>{o.name}</button>
                <span class="muted" title={$i18n("objects.sourceTip", { source: o.source })}>
                  {o.name.startsWith("0x") ? o.source : hex32(o.handle)}</span>
              </td>
              <td>{typeLabel(o.kind)}</td>
              <td class="mono">{describe(o)}
                {#if o.fields?.callback}
                  · {$i18n("objects.f.callback")}
                  <button class="link" onclick={() => openCallback(o.fields.callback)}>{o.fields.callback}</button>
                {/if}
                {#if o.notes?.length}<span class="muted" title={o.notes.join("\n")}>ⓘ</span>{/if}
              </td>
              <td class="mono">{o.waiters.map((w: any) => waiterText(w, $i18n)).join(", ")}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  {:else if connected && error}
    <div class="empty"><strong>{$i18n("objects.none")}</strong><span>{$i18n("objects.noneHint")}</span>
      <span class="muted">{error}</span></div>
  {:else}
    <Empty need="connected" />
  {/if}
</div>

<style>
  .live { font-size: 12px; color: var(--ok); }
  .banner { padding: 4px 8px; font-size: 12px; border-bottom: 1px solid var(--line); }
  .banner.warn { color: var(--warn); }
  .banner.bad { color: var(--bad); }
  .heap { display: flex; gap: 10px; align-items: center; padding: 4px 8px; font-size: 12px; color: var(--muted); }
  .bar { position: relative; display: inline-block; width: 120px; height: 6px; background: var(--line); border-radius: 3px; }
  .bar span { display: block; height: 100%; background: var(--accent); border-radius: 3px; }
  .bar i { position: absolute; top: -2px; width: 2px; height: 10px; background: var(--warn); }
</style>
