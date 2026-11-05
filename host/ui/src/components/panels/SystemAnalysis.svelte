<script lang="ts">
  // システム解析の「記録の解析」: 結果の直前に多い出来事（recording.correlate）・値が続いた時間（recording.durations）・
  // 区間ごとの集計（recording.transactions）。どれもデーモンの答えをそのまま描く（画面では数えない・推さない）。
  // 条件の表（FPGA の観測の条件）は SystemPanel が持ち、ここへは bind で渡す（読む・「条件に足す」で足す）。
  import { errorText, type Result } from "../../lib/api";
  import { shared } from "../../lib/context";
  import { i18n, type Key } from "../../lib/i18n";
  import { suggestedOf } from "../../lib/apispec";
  import { load, save } from "../../lib/prefs";
  import { correlateParams, exampleView, parseCount, samplingRows, type Condition } from "../../lib/system";
  import Hint from "./Hint.svelte";

  let { recName, linkOpen, conditions = $bindable() }: {
    recName: string | null | undefined; linkOpen: boolean; conditions: Condition[] } = $props();
  const { conn, dock, tl } = shared();

  // 結果の直前に多い出来事（recording.correlate）: 結果の条件の直前に、FPGA の条件の表と追加の候補のどれが来ているか
  interface Cause { condition: string; fpga_only: boolean;   // 項の変数がすべて FPGA の信号（デーモンが記録で解いた事実）
                    count: number; judged?: number; preceded: number | null; coverage: number | null; followed: number | null;
                    delay_s: { median: number } | null; examples: { cause_t: number; effect_t: number }[];
                    same?: string[] }   // 同じ時刻に成立する候補（自動の候補の重複をまとめた）
  // causes_total・truncated・limit: 自動の候補を limit 個に切ったときの切る前の数と印（切ったことを黙らない）
  interface Correlation { effect: { count: number }; causes: Cause[]; timebase_hz: number; auto?: boolean;
                          causes_total?: number; truncated?: boolean; limit?: number | null;
                          sampling: Record<string, number | null>; fpga_hidden: number;
                          warnings?: string[]; no_values?: string[]; range: [number, number]; gaps?: Gap[] }
  // データの欠け（その間の結果・候補は数えられていない）。黙って数え落とさないために一覧にし、時間軸で見られるように
  interface Gap { reason: string; t0: number; t1: number; variable?: string; count?: number }
  const GAPS_SHOWN = 5;
  let effectExpr = $state(load("system.effect", "") || "");
  let extraCauses = $state(load("system.extraCauses", "") || "");
  // 窓は使う人が入れる（preceded・coverage を左右するので、既定の値で決めない）。null = 入れていない
  const savedWindow = parseFloat(load("system.windowMs", ""));
  let windowMs = $state<number | null>(Number.isFinite(savedWindow) && savedWindow > 0 ? savedWindow : null);
  const windowOk = $derived(windowMs !== null && Number.isFinite(windowMs) && windowMs > 0);
  $effect(() => save("system.effect", effectExpr));
  $effect(() => save("system.extraCauses", extraCauses));
  $effect(() => save("system.windowMs", windowOk ? String(windowMs) : ""));
  // 自動の候補で返す数（上位の数）は使う人が入れる（API の既定を暗黙に使わない）
  let autoLimit = $state(load("system.autoLimit", "") || String(suggestedOf("recording.correlate", "limit") ?? ""));   // 最初の値は仕様の既定
  $effect(() => save("system.autoLimit", autoLimit));
  const autoLimitN = $derived(parseCount(autoLimit));
  let correlation = $state<Correlation | null>(null);
  let correlating = $state(false);
  let correlateError = $state<string | null>(null);
  // 結果の条件は使う人が入れたものだけ（表の先頭の変数等から暗黙に決めない。空なら解析を押せない）
  const effectText = $derived(effectExpr.trim());
  const causeList = $derived([...conditions.map((c) => c.expr),
                              ...extraCauses.split(/[\n,]/).map((x) => x.trim()).filter(Boolean)]);

  // auto = 候補を書かずに、記録の変数から自動で作って調べる（信号の変化・列挙の値・その組・ピン・ほかのメモリ）
  async function investigate(auto = false) {
    correlating = true;
    correlateError = null;
    try {
      const params = correlateParams(effectExpr, causeList, auto, windowMs, autoLimitN);
      if (!params) return;
      correlation = await conn.call("recording.correlate", params);
    } catch (e) {
      correlation = null;
      correlateError = errorText(e);
    } finally {
      correlating = false;
    }
  }
  async function showExample(ex: { cause_t: number; effect_t: number }) {
    tl.setSource("recording");
    if (!correlation) return;
    await conn.call("ui.state.set", exampleView(ex, correlation.timebase_hz), { quiet: true });
    dock.show("timeline");
  }
  // 値が続いた時間（recording.durations）: 例 FPGA の state が BUSY の長さと MCU の待ち時間を比べる
  interface Durations { values: Record<string, { count: number; share: number | null;
                                                 duration_s: { min: number; median: number; p90: number; max: number } | null }>;
                        excluded?: number; gaps?: Gap[]; values_total?: number; truncated?: boolean; limit?: number }
  let durationVar = $state(load("system.durationVar", "") || "");
  $effect(() => save("system.durationVar", durationVar));
  let durationResult = $state<Durations | null>(null);
  let durationError = $state<string | null>(null);
  async function measureDurations() {
    durationError = null;
    try {
      durationResult = await conn.call("recording.durations", { variable: durationVar.trim() });
    } catch (e) {
      durationResult = null;
      durationError = errorText(e);
    }
  }
  const ms = (s: number) => (s * 1000).toFixed(s < 0.01 ? 3 : 2);

  // 区間ごとの集計（recording.transactions）: 記録をリクエスト等の区間に切り、開始時の値ごとに結果の割合・条件の回数
  interface TxGroup { by: string | null; count: number; effect_rate: number | null; counts: Record<string, number> }
  let txStart = $state(load("system.txStart", "") || "");
  let txBy = $state(load("system.txBy", "") || "");
  let txCounts = $state(load("system.txCounts", "") || "");
  let txEffect = $state(load("system.txEffect", "") || "");
  let txAtEnd = $state(load("system.txAtEnd", "0") === "1");
  let txLagMs = $state(Number(load("system.txLagMs", "0")) || 0);   // 結果を前の区間に寄せる（前の要求の結果が次に読まれるとき）
  $effect(() => save("system.txLagMs", String(txLagMs)));
  $effect(() => save("system.txAtEnd", txAtEnd ? "1" : "0"));
  $effect(() => { save("system.txStart", txStart); save("system.txBy", txBy); save("system.txCounts", txCounts); save("system.txEffect", txEffect); });
  let txResult = $state<{ transactions: number; groups: TxGroup[]; effect: string | null; excluded?: number; gaps?: Gap[]; excluded_by?: Record<string, number>;
                                 length_s?: { median: number } | null; effect_offset_s?: { median: number } | null;
                                 by_effect?: Record<"with" | "without", { count: number; counts: Record<string, number | null> }> | null } | null>(null);
  let txError = $state<string | null>(null);
  async function runTransactions() {
    txError = null;
    try {
      txResult = await conn.call("recording.transactions", {
        start: txStart.trim(), by: txBy.trim() || null, effect: txEffect.trim() || null, rows: 0,
        by_at: txAtEnd ? "end" : "start", effect_lag_s: Math.max(0, txLagMs) / 1000,
        counts: txCounts.split(",").map((x) => x.trim()).filter(Boolean) });
    } catch (e) {
      txResult = null;
      txError = errorText(e);
    }
  }

  // 結果の直前に多い出来事のうち FPGA の信号だけの条件（デーモンの fpga_only）は、FPGA の条件の表に足して毎秒の回数を見張れる
  const addAsCondition = (expr: string) => {
    if (!conditions.some((c) => c.expr === expr)) conditions = [...conditions, { name: expr, expr }];
  };

  const pct = (x: number | null) => (x === null ? "—" : `${Math.round(x * 100)} %`);
  // 時間分解能: recording.correlate の sampling（変数ごとの読みの周期）をそのまま並べる（最大を取って 1 つにしない）
  const samplingText = $derived(samplingRows(correlation?.sampling).map(({ name, ms }) =>
    `${name} ${ms === null ? "—" : `${ms.toFixed(ms < 1 ? 3 : 1)} ms`}`).join(", ") || "—");
</script>

  <!-- 結果の直前に多い出来事: 記録の中で、結果（MCU のメモリの変化等）の直前にどの条件が来ているか -->
  <section class="causes" aria-label={$i18n("system.causes")}>
    <div class="sub-title" title={$i18n("system.causesTip")}>{$i18n("system.causes")}</div>
    {#if !recName}<Hint text={$i18n("system.causesHint")} />{/if}
    <div class="cause-form">
      <label>{$i18n("system.effect")}
        <input class="mono" placeholder="errors changed" bind:value={effectExpr} /></label>
      <label>{$i18n("system.extraCauses")}
        <input class="mono" placeholder="in0 rose, addr=RESULT and write rose" bind:value={extraCauses} /></label>
      <label class="narrow">{$i18n("system.window")}
        <input type="number" min="0.01" step="any" placeholder="ms" bind:value={windowMs} /></label>
      <button disabled={!linkOpen || correlating || !effectText || !causeList.length || !windowOk} onclick={() => investigate()}>
        {correlating ? $i18n("system.investigating") : $i18n("system.investigate")}</button>
      <label class="narrow" title={$i18n("system.autoLimitTip")}>{$i18n("system.autoLimit")}<span class="req"
        aria-hidden="true">*</span>
        <input class="mono" inputmode="numeric" bind:value={autoLimit} aria-required="true" /></label>
      <button title={$i18n("system.autoInvestigateTip")} disabled={!linkOpen || correlating || !effectText || !windowOk || autoLimitN === null}
              onclick={() => investigate(true)}>{$i18n("system.autoInvestigate")}</button>
    </div>
    {#if correlateError}<div class="note bad">{correlateError}</div>{/if}
    {#if correlation}
      <div class="muted small">{$i18n("system.causeNoteSampling", { n: correlation.effect.count, sampling: samplingText,
                                                                   hidden: correlation.fpga_hidden })}</div>
      {#each correlation.warnings ?? [] as w (w)}<div class="note">{w}</div>{/each}
      {#if correlation.gaps?.length}
        {@const hz = correlation.timebase_hz}
        <ul class="gap-list small">
          {#each correlation.gaps.slice(0, GAPS_SHOWN) as g, i (i)}
            <li><span>{$i18n(`system.gap.${g.reason}` as Key)}{g.variable ? ` (${g.variable})` : ""}{g.count ? ` ×${g.count}` : ""}</span>
              <span class="mono muted">{((g.t0 - correlation.range[0]) / hz).toFixed(3)} s{g.t1 > g.t0 ? ` +${((g.t1 - g.t0) / hz * 1000).toFixed(1)} ms` : ""}</span>
              <button onclick={() => showExample({ cause_t: g.t0, effect_t: g.t1 })}>{$i18n("system.showExample")}</button></li>
          {/each}
          {#if correlation.gaps.length > GAPS_SHOWN}<li class="muted">{$i18n("system.gapsMore", { n: correlation.gaps.length - GAPS_SHOWN })}</li>{/if}
        </ul>
      {/if}
      {#if correlation.no_values?.length}
        <div class="note" title={$i18n("system.noValuesTip")}>{$i18n("system.noValues2", { names: correlation.no_values.join(", ") })}</div>
      {/if}
      {#if correlation.truncated}
        <div class="note">{$i18n("common.truncated", { shown: correlation.causes.length, total: correlation.causes_total ?? "?", limit: correlation.limit ?? "?" })}</div>
      {/if}
      <!-- 候補ごとに 2 行: 名前・式・回数・見る / 数（狭い右の列でも式が折り返さない） -->
      <ol class="cause-list">
        {#each correlation.causes as c (c.condition)}
          {@const named = conditions.find((x) => x.expr === c.condition && x.name !== x.expr)}
          <li>
            <div class="cause-head">
              <span class="cause-name" title={[c.condition, ...(c.same ?? [])].join(" / ")}>{#if named}<strong>{named.name}</strong> <span class="mono muted">{c.condition}</span>{:else}<span class="mono">{c.condition}</span>{/if}
                <span class="muted">({c.count})</span>
                {#if c.same?.length}<span class="muted small">{$i18n("system.sameAs", { list: c.same.join(" / ") })}</span>{/if}</span>
              {#if c.fpga_only && !conditions.some((x) => x.expr === c.condition)}
                <button title={$i18n("system.addAsConditionTip")} onclick={() => addAsCondition(c.condition)}>{$i18n("system.addAsCondition")}</button>
              {/if}
              {#if c.examples.length}<button onclick={() => showExample(c.examples[0])}>{$i18n("system.showExample")}</button>{/if}
            </div>
            <div class="cause-stats mono">
              <span title={$i18n("system.precededTip")}>{$i18n("system.preceded")} <b>{pct(c.preceded)}</b>{#if c.judged !== undefined && c.judged < correlation.effect.count}<span class="muted" title={$i18n("system.judgedTip")}> {$i18n("system.judged", { n: c.judged, of: correlation.effect.count })}</span>{/if}</span>
              <span title={$i18n("system.coverageTip")}>{$i18n("system.coverage")} <b>{pct(c.coverage)}</b></span>
              <span title={$i18n("system.followedTip")}>{$i18n("system.followed")} <b>{pct(c.followed)}</b></span>
              <span>{$i18n("system.delay")} <b>{c.delay_s ? `${(c.delay_s.median * 1000).toFixed(2)} ms` : "—"}</b></span>
            </div>
          </li>
        {/each}
      </ol>
    {/if}

    <div class="sub-title" title={$i18n("system.durationsTip")}>{$i18n("system.durations")}</div>
    <div class="cause-form">
      <label>{$i18n("system.variable")}
        <input class="mono" placeholder="state" bind:value={durationVar}
               onkeydown={(e) => e.key === "Enter" && durationVar.trim() && measureDurations()} /></label>
      <button disabled={!linkOpen || !durationVar.trim()} onclick={measureDurations}>{$i18n("system.measure")}</button>
    </div>
    {#if durationError}<div class="note bad">{durationError}</div>{/if}
    {#if durationResult}
      {#if durationResult.excluded}<div class="note" title={$i18n("system.excludedTip")}>{$i18n("system.durExcluded", { n: durationResult.excluded, gaps: durationResult.gaps?.length ?? 0 })}</div>{/if}
      {#if durationResult.truncated}
        <div class="note">{$i18n("common.truncated", { shown: Object.keys(durationResult.values).length, total: durationResult.values_total ?? "?", limit: durationResult.limit ?? "?" })}</div>
      {/if}
      <table class="grid">
        <thead><tr><th>{$i18n("system.value")}</th><th class="nowrap">{$i18n("system.count")}</th>
          <th class="nowrap">{$i18n("system.share")}</th><th class="nowrap">{$i18n("system.durationMs")}</th></tr></thead>
        <tbody>
          {#each Object.entries(durationResult.values) as [value, d] (value)}
            <tr>
              <td class="mono">{value}</td><td class="mono">{d.count}</td><td class="mono">{pct(d.share)}</td>
              <td class="mono">{d.duration_s ? `${ms(d.duration_s.min)} / ${ms(d.duration_s.median)} / ${ms(d.duration_s.p90)} / ${ms(d.duration_s.max)}` : "—"}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}

    <div class="sub-title" title={$i18n("system.transactionsTip")}>{$i18n("system.transactions")}</div>
    <div class="cause-form">
      <label>{$i18n("system.txStart")}<input class="mono" placeholder="state changed and state=BUSY" bind:value={txStart} /></label>
      <label>{$i18n("system.txBy")}<input class="mono" placeholder="left_hi" bind:value={txBy} /></label>
      <label class="check nowrap" title={$i18n("system.txAtEndTip")}><input type="checkbox" bind:checked={txAtEnd} /> {$i18n("system.txAtEnd")}</label>
      <label>{$i18n("system.txCounts")}<input class="mono" placeholder="restart changed and state=BUSY" bind:value={txCounts} /></label>
      <label>{$i18n("system.effect")}<input class="mono" placeholder="errors changed" bind:value={txEffect}
               onkeydown={(e) => e.key === "Enter" && txStart.trim() && runTransactions()} /></label>
      <label class="narrow" title={$i18n("system.txLagTip")}>{$i18n("system.txLag")}<input type="number" min="0" step="0.01" class="mono" bind:value={txLagMs} /></label>
      <button disabled={!linkOpen || !txStart.trim()} onclick={runTransactions}>{$i18n("system.txRun")}</button>
    </div>
    {#if txError}<div class="note bad">{txError}</div>{/if}
    {#if txResult}
      <div class="muted small">{$i18n("system.txTotal", { n: txResult.transactions.toLocaleString() })}</div>
      {#if txResult.excluded}<div class="note" title={$i18n("system.excludedTip")}>{$i18n("system.txExcluded", { n: txResult.excluded, gaps: txResult.gaps?.length ?? 0 })}
        <span class="mono">{Object.entries(txResult.excluded_by ?? {}).map(([k, v]) => `${k} ${v}`).join(" / ")}</span></div>{/if}
      {#if txResult.effect_offset_s && txResult.length_s}
        <div class="muted small" title={$i18n("system.txOffsetTip")}>{$i18n("system.txOffset", { at: (txResult.effect_offset_s.median * 1000).toFixed(3), len: (txResult.length_s.median * 1000).toFixed(3) })}</div>
      {/if}
      <table class="grid">
        <thead><tr><th>{txBy || "—"}</th><th class="nowrap">{$i18n("system.txCount")}</th>
          {#if txResult.effect}<th class="nowrap">{$i18n("system.txEffectRate")}</th>{/if}
          {#each Object.keys(txResult.groups[0]?.counts ?? {}) as c (c)}<th class="mono">{c}</th>{/each}</tr></thead>
        <tbody>
          {#each txResult.groups as g (g.by)}
            <tr>
              <td class="mono">{g.by ?? "?"}</td><td class="mono">{g.count}</td>
              {#if txResult.effect}<td class="mono">{pct(g.effect_rate)}</td>{/if}
              {#each Object.values(g.counts) as v, k (k)}<td class="mono">{v.toFixed(2)}</td>{/each}
            </tr>
          {/each}
        </tbody>
      </table>
      <!-- 結果が起きた区間と起きなかった区間を並べる（違いを事実として見せる。判断はしない） -->
      {#if txResult.by_effect && Object.keys(txResult.by_effect.with.counts).length}
        {@const be = txResult.by_effect}
        <table class="grid" title={$i18n("system.txByEffectTip")}>
          <thead><tr><th></th><th class="nowrap">{$i18n("system.txCount")}</th>
            {#each Object.keys(be.with.counts) as c (c)}<th class="mono">{c}</th>{/each}</tr></thead>
          <tbody>
            {#each [["with", be.with], ["without", be.without]] as const as [k, row] (k)}
              <tr><td class="nowrap">{$i18n(k === "with" ? "system.txWithEffect" : "system.txWithoutEffect")}</td>
                <td class="mono">{row.count}</td>
                {#each Object.values(row.counts) as v, j (j)}<td class="mono">{v === null ? "—" : v.toFixed(2)}</td>{/each}</tr>
            {/each}
          </tbody>
        </table>
      {/if}
    {/if}
  </section>

<style>
  .sub-title { font-weight: 600; font-size: 12px; margin: 6px 0 2px; }
  .note { padding: 3px 8px; font-size: 12px; }
  .bad { color: var(--bad); }
  .small { font-size: 12px; padding: 2px 0; }
  .check { width: auto; min-width: 0; margin-right: 4px; }
  input { width: 100%; min-width: 5em; box-sizing: border-box; }
  .grid { width: 100%; table-layout: auto; }
  .grid th { white-space: nowrap; text-align: left; }
  .nowrap { white-space: nowrap; }
  .causes { margin: 0 8px 8px; border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; }
  .cause-form { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: end; font-size: 12px; }
  .cause-form label { display: flex; flex-direction: column; flex: 1 1 14em; }
  .cause-form label.narrow { flex: 0 0 6em; }
  .cause-form label.check { flex: 0 0 auto; flex-direction: row; align-items: center; gap: 4px; padding-bottom: 3px; }
  .cause-form label.check input { width: auto; flex: 0 0 auto; margin: 0; }
  .cause-list { list-style: none; margin: 4px 0 0; padding: 0; }
  .gap-list { list-style: none; margin: 2px 0; padding: 0; }
  .gap-list li { display: flex; align-items: center; gap: 8px; }
  .gap-list li > span:first-child { flex: 1; }
  .cause-list li { border-top: 1px solid var(--line); padding: 4px 0; }
  .cause-head { display: flex; align-items: center; gap: 8px; }
  .cause-name { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .cause-stats { display: flex; flex-wrap: wrap; gap: 2px 14px; font-size: 12px; color: var(--muted); }
  .cause-stats b { color: var(--text); font-weight: 600; }
</style>
