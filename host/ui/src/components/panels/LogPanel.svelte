<script lang="ts">
  // Log（段階 B §3 の 8）: 解読したログ（defmt / trice / 文字 / compact / memfault）を RTT・SWO・UART から受けて、記録の表で
  // 並べる（log.start / log.stop / log.read）。重要度・文字で絞る。値は API の記録のまま
  import { errorText } from "../../lib/api";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { LogView, locationText, startParams, type LogRecord, type StartForm } from "../../lib/logview";
  import { load, save } from "../../lib/prefs";

  const POLL_MS = 500;
  const LEVELS = ["", "trace", "debug", "info", "warn", "error"] as const;
  const { app, conn } = shared();

  let form = $state<StartForm>(restore());
  let level = $state("");
  let contains = $state("");
  let records = $state<LogRecord[]>([]);
  let dropped = $state(0);
  let running = $state(false);
  let error = $state<string | null>(null);
  const view = new LogView(conn);

  function restore(): StartForm {
    try {
      const v = JSON.parse(load("log.form", ""));
      if (v && typeof v.format === "string") return v;
    } catch { /* 既定 */ }
    return { format: "defmt", source: "rtt", channel: "", elf: "", til: "" };
  }

  async function pull() {
    try {
      await view.more({ level, contains });
      records = view.records;
      dropped = view.dropped;
      running = view.running;
      error = null;
    } catch (e) {
      error = errorText(e);
      running = false;
    }
  }

  // 動いている間は読み足す。絞り込みを変えたら最初から読み直す
  $effect(() => {
    level;
    contains;
    view.reset();
    void pull();
  });
  // だれが（AI も）開始・停止しても読み直す
  $effect(() => conn.on("activity", (e: { method: string }) => {
    if (e.method === "log.start" || e.method === "log.stop") {
      if (e.method === "log.start") view.reset();
      void pull();
    }
  }));
  $effect(() => {
    if (!running) return;
    const timer = setInterval(() => void pull(), POLL_MS);
    return () => clearInterval(timer);
  });

  async function start() {
    if (!(await allowed(app))) return;
    save("log.form", JSON.stringify(form));
    if ((await app.act("log.start", startParams(form) as never)) === undefined) return;
    view.reset();
    await pull();
  }

  async function stop() {
    if (!(await allowed(app))) return;
    await app.act("log.stop", {});
    await pull();
  }
</script>

<div class="pane log">
  <form class="pane-bar" onsubmit={(e) => { e.preventDefault(); void start(); }}>
    <select bind:value={form.format} aria-label={$i18n("log.format")}>
      {#each ["defmt", "trice", "text", "compact", "memfault"] as f (f)}<option value={f}>{f}</option>{/each}
    </select>
    <select bind:value={form.source} aria-label={$i18n("log.source")}>
      <option value="rtt">RTT</option><option value="swo">SWO</option><option value="uart">UART</option>
    </select>
    <input class="mono num" bind:value={form.channel} placeholder={$i18n("log.channel")} aria-label={$i18n("log.channel")} />
    <input class="mono path" bind:value={form.elf} placeholder={$i18n("log.elf")} aria-label={$i18n("log.elf")} />
    {#if form.format === "trice"}
      <input class="mono path" bind:value={form.til} placeholder="til.json" aria-label="til.json" />
    {/if}
    <button class="primary small" disabled={running}>{$i18n("log.start")}</button>
    <button type="button" class="small" disabled={!running} onclick={() => void stop()}>{$i18n("log.stop")}</button>
    <span class="spacer"></span>
    <select bind:value={level} aria-label={$i18n("log.level")}>
      {#each LEVELS as l (l)}<option value={l}>{l || $i18n("log.allLevels")}</option>{/each}
    </select>
    <input bind:value={contains} placeholder={$i18n("log.contains")} aria-label={$i18n("log.contains")} />
  </form>
  {#if error}<div class="bad pad">{error}</div>{/if}
  {#if dropped}<div class="muted pad">{$i18n("log.dropped", { n: dropped })}</div>{/if}
  <div class="rows mono">
    {#each records as r (r.seq)}
      <div class="row" class:bad={!!r.error} class:warn={r.level === "warn"} class:err={r.level === "error"}>
        <span class="muted">{r.timestamp_text ?? ""}</span>
        <span class="lvl">{r.level ?? ""}</span>
        <span class="text">{r.error ?? r.text}</span>
        <span class="muted">{locationText(r.location)}</span>
      </div>
    {:else}
      <div class="muted pad">{$i18n("log.none")}</div>
    {/each}
  </div>
</div>

<style>
  .log { display: flex; flex-direction: column; }
  .num { width: 5em; }
  .path { width: 12em; }
  .rows { flex: 1; overflow: auto; font-size: 12px; padding: 2px 6px; }
  .row { display: grid; grid-template-columns: 90px 52px 1fr auto; gap: 8px; }
  .row .text { white-space: pre-wrap; word-break: break-word; }
  .warn .lvl { color: var(--warn); }
  .err .lvl, .bad { color: var(--bad); }
  .pad { padding: 4px 8px; }
</style>
