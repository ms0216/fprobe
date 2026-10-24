<script lang="ts">
  // Interrupts（SystemView の Context / Runtime・IAR の Interrupt Log Summary・MCUXpresso の SWO Interrupt Trace の型）:
  // 割り込みごとの回数・合計・排他（自分）・最小・最大・平均・負荷と最大の回の時刻。源は記録の割り込みの区間（時刻が
  // タイムベース = 時間軸へ飛べる）か、Code Profile の SWO 例外トレース（profile.exceptions）。選んだ割り込みの時間の
  // 分布図（長い順の上位の棒のダブルクリックでその回へ）。長い順の上位は時間軸にも印を置く
  import { onDestroy } from "svelte";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";
  import {
    isrAt, longestIn, profileHz, recordedIsrs, statsFromProfile, type Histogram, type IsrStat,
  } from "../../lib/interrupts";
  import { parseCount } from "../../lib/system";
  import { load, save } from "../../lib/prefs";
  import { percent } from "../../lib/rtos";
  import { onCalls, onRecording } from "../../lib/notify";
  import { formatDuration } from "../../lib/time";
  import { onTimeline } from "../../lib/timeline/events";
  import Chart from "../charts/Chart.svelte";
  import TimeLink from "./TimeLink.svelte";

  const { app, conn, tl, reveal } = shared();
  const quiet = { quiet: true };

  let source = $state<"recording" | "profile">(load("interrupts.source", "recording") === "profile" ? "profile" : "recording");
  $effect(() => save("interrupts.source", source));
  let recorded = $state<Awaited<ReturnType<typeof recordedIsrs>> | null>(null);
  let profiled = $state<IsrStat[] | null>(null);
  let profiledHz = $state<number | null>(null);        // SWO の例外トレースのコア クロック（分からなければ null）
  // 分布図の棒の数・印を付ける長い順の上位の数は使う人が入れる（空から。暗黙の数を持たない）
  let binsText = $state(load("interrupts.bins", ""));
  let longestText = $state(load("interrupts.longest", ""));
  $effect(() => save("interrupts.bins", binsText));
  $effect(() => save("interrupts.longest", longestText));
  const bins = $derived(parseCount(binsText));
  const longestCount = $derived(parseCount(longestText));
  let selected = $state<number | null>(null);
  let recKey = "";
  let recKeyGen = $state(0);

  const online = $derived($app.link === "open");
  const available = $derived({ recording: !!recorded?.stats.length, profile: !!profiled?.length });
  const active = $derived(available[source] ? source : available.recording ? "recording" : available.profile ? "profile" : null);
  const stats = $derived<IsrStat[]>(active === "recording" ? recorded!.stats : active === "profile" ? profiled! : []);
  const chosen = $derived(stats.find((s) => s.number === selected) ?? stats[0] ?? null);
  // 選んだ割り込みの分布（デーモンが数える recording.interrupts の distribution。棒の数・上位の数は使う人の値のまま）
  let hist = $state<Histogram | null>(null);
  $effect(() => {
    const n = active === "recording" ? chosen?.number ?? null : null, b = bins, k = longestCount;
    void recKeyGen;
    if (n === null || b === null || k === null) {
      hist = null;
      return;
    }
    void recordedIsrs(conn, { number: n, bins: b, longest: k }).then((r) => (hist = r.histogram)).catch(() => (hist = null));
  });
  // 時間の単位（1 秒あたりの数）。分からなければ null: 秒にせず元の単位の数のまま出す
  const hz = $derived(active === "recording" ? recorded?.hz ?? null : active === "profile" ? profiledHz : null);
  // 統計（statsOf・statsFromProfile が hz で割った値）を出す
  const sec = (s: number) => (hz !== null ? formatDuration(s)
    : $i18n(active === "profile" ? "interrupts.rawCycles" : "interrupts.rawTicks", { n: s }));
  // 回の時間（元の単位の数）を出す
  const raw = (n: number) => (hz !== null ? formatDuration(n / hz) : $i18n("interrupts.rawTicks", { n }));

  $effect(() => {
    if (online) void refresh();
  });
  // ほかの利用者（AI も）がプロファイルを始め・止めた、記録を開いた・始めた・止めたら読み直す
  $effect(() => onCalls(conn, ["profile.start", "profile.stop"], () => void refresh()));
  $effect(() => onRecording(conn, () => void refresh()));
  // 選んだ割り込みの長い順の上位を時間軸に印として置く（普通の色: 異常とは言わない）
  $effect(() => {
    tl.setMarks("interrupts", "recording", (hist?.longest ?? []).map((a) => ({
      t: a.t, tEnd: a.tEnd, tone: 0 as const, label: `${chosen?.name ?? ""} ${raw(a.inclusive)}` })));
  });
  onDestroy(() => tl.setMarks("interrupts", "recording", []));
  // 時間軸の 1 回クリック（記録）: その時刻に処理していた割り込みの行を選ぶ
  // （その時刻に実行していた = デーモンが返す at_numbers のいちばん内側）
  $effect(() => onTimeline({
    select: (d) => {
      if (d.source !== "recording" || active !== "recording" || !recorded) return;
      void isrAt(conn, d.t).then((n) => {
        if (n !== null) selected = n;
      }).catch(() => {});
    },
  }));

  async function refresh() {
    try {
      const st = await conn.call("recording.status", {}, quiet);
      const key = st.name ? `${st.name}/${st.events}` : "";
      if (key !== recKey) {
        recKey = key;
        recorded = key ? await recordedIsrs(conn, null) : null;
        recKeyGen++;                                       // 分布も読み直す
      }
    } catch {
      recorded = null;
    }
    try {
      const r = await conn.call("profile.exceptions", {}, quiet);
      profiled = statsFromProfile(r);
      profiledHz = profileHz(r);
    } catch {
      profiled = null;
      profiledHz = null;
    }
  }

  function openLongest(s: IsrStat) {
    if (active === "recording" && s.maxAt !== null) reveal({ source: "recording", t: s.maxAt, tEnd: s.maxEnd ?? s.maxAt });
  }

  function openBar(_x: number, bar: number | null) {
    const a = hist ? longestIn(hist, bar) : null;
    if (a) reveal({ source: "recording", t: a.t, tEnd: a.tEnd });
  }
</script>

<div class="pane irq">
  <div class="pane-bar">
    <label>{$i18n("interrupts.source")}
      <select bind:value={source}>
        <option value="recording" disabled={!available.recording}>{$i18n("interrupts.fromRecording")}</option>
        <option value="profile" disabled={!available.profile}>{$i18n("interrupts.fromProfile")}</option>
      </select></label>
    <span class="spacer"></span>
    <button class="small" disabled={!online} onclick={() => { recKey = ""; void refresh(); }}>{$i18n("common.refresh")}</button>
  </div>
  {#if active}
    <table class="grid">
      <thead><tr><th>{$i18n("interrupts.name")}</th><th>{$i18n("interrupts.count")}</th><th>{$i18n("interrupts.total")}</th>
        <th title={$i18n("interrupts.selfTip")}>{$i18n("interrupts.self")}</th><th>{$i18n("interrupts.min")}</th>
        <th>{$i18n("interrupts.max")}</th><th>{$i18n("interrupts.mean")}</th><th>{$i18n("interrupts.load")}</th>
        <th>{$i18n("interrupts.maxAt")}</th></tr></thead>
      <tbody>
        {#each stats as s (s.number)}
          <tr class="row" class:sel={chosen?.number === s.number} title={$i18n("interrupts.rowTip")}
              onclick={() => (selected = s.number)} ondblclick={() => openLongest(s)}>
            <td class="mono">{s.name} <span class="muted">#{s.number}</span></td>
            <td class="mono">{s.count}</td>
            <td class="mono">{sec(s.total)}</td>
            <td class="mono">{sec(s.self)}</td>
            <td class="mono">{sec(s.min)}</td>
            <td class="mono">{sec(s.max)}</td>
            <td class="mono">{sec(s.mean)}</td>
            <td class="mono">{percent(s.load)}</td>
            <td>
              {#if active === "recording" && s.maxAt !== null}
                <TimeLink to={{ source: "recording", t: s.maxAt, tEnd: s.maxEnd ?? s.maxAt }} />
              {:else if s.maxAt !== null}
                <span class="mono muted">{$i18n("interrupts.cycles", { n: s.maxAt })}</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    {#if chosen}
      <div class="dist">
        <div class="head">
          <span>{$i18n("interrupts.distribution", { name: chosen.name })}</span>
          {#if active === "recording"}
            <label>{$i18n("interrupts.bins")}<input class="num" bind:value={binsText} inputmode="numeric" size="4"
              aria-required="true" /><span class="req" aria-hidden="true">*</span></label>
            <label title={$i18n("interrupts.longestTip")}>{$i18n("interrupts.longest")}<input class="num"
              bind:value={longestText} inputmode="numeric" size="4" aria-required="true" /><span class="req"
              aria-hidden="true">*</span></label>
          {/if}
        </div>
        {#if active === "recording" && hist}
          <div title={$i18n("interrupts.histTip", { n: longestCount ?? 0 })}>
            <Chart bars={hist.bars} height={120} xFormat={raw} onopen={openBar}
                   label={$i18n("interrupts.distribution", { name: chosen.name })} />
          </div>
        {:else if active === "recording"}
          <span class="muted">{$i18n("interrupts.needBins")}</span>
        {:else}
          <span class="muted">{$i18n("interrupts.noInstances")}</span>
        {/if}
      </div>
    {/if}
  {:else}
    <div class="empty"><strong>{$i18n("interrupts.none")}</strong><span>{$i18n("interrupts.noneHint")}</span></div>
  {/if}
</div>

<style>
  .irq label { display: inline-flex; gap: 4px; align-items: center; font-size: 12px; color: var(--muted); }
  .irq select { font-size: 12px; padding: 1px 4px; }
  .row { cursor: pointer; }
  .row:hover td, .row.sel td { background: var(--select); }
  .dist { padding: 6px 8px; }
  .head { display: flex; gap: 10px; font-size: 12px; color: var(--muted); margin-bottom: 4px; align-items: center; }
</style>
