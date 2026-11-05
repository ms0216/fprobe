<script lang="ts">
  // システム解析の FPGA 側: 観測の型・状態と各組の最新の値（信号の名前で）、条件の回数と、条件でのハードのクロストリガ
  // （止める・前後の波形を捕捉する）。条件の回数は、使う人が入れた範囲（最後の N 件）でデーモンが数えた事実
  // （fpga_obs.count）をそのまま描く。画面では数えない・毎秒を計算しない・推さない。
  import Hint from "../Hint.svelte";
  import { type Result } from "../../../lib/api";
  import { shared } from "../../../lib/context";
  import { i18n } from "../../../lib/i18n";
  import { conditionError, fieldsText, type Condition, type ConditionCount, type HaltState } from "../../../lib/system";

  type Status = Result<"system.status">;
  type FpgaOpen = Extract<Status["fpga"], { available: true }>;

  let { status, fpga, rec, obsMatch, conditions = $bindable(), condCounts, condLastText = $bindable(), haltOn = $bindable(), onReopen }: {
    status: Status | null; fpga: FpgaOpen | null; rec: Result<"recording.status"> | null; obsMatch: boolean;
    conditions: Condition[]; condCounts: Record<string, ConditionCount>; condLastText: string; haltOn: HaltState | null;
    onReopen: () => void;
  } = $props();

  const { app, conn, dock, tl } = shared();

  let newCondName = $state("");
  let newCondExpr = $state("");
  let condError = $state<string | null>(null);

  // FPGA の条件でハードのクロストリガ（fpga_obs.trigger_set）: 成立した瞬間に MCU を止める（SWD 4 MHz で約 64 µs）
  async function armHalt(expr: string) {
    haltOn = (await app.act("fpga_obs.trigger_set", { condition: expr, halt: true })) ?? haltOn;
  }
  async function clearHalt() {
    await app.act("fpga_obs.trigger_clear", {});
    haltOn = null;
  }
  // 条件が成立した瞬間の前後のピンの波形を捕捉する: 同じ一致の回路をロジアナのトリガへ（停止の代わり。回路は 1 つ）、
  // ロジアナはクロストリガだけで成立させる。内蔵の捕捉が無い構成（PYNQ の深いバッファ版）は深いバッファで
  let capturingOn = $state<string | null>(null);
  async function captureOn(expr: string) {
    const caps = ($app.probe?.capabilities ?? {}) as Record<string, any>;
    capturingOn = expr;
    let armed = false;
    try {
      armed = Boolean(await app.act("fpga_obs.trigger_set", { condition: expr, halt: false, la: true }));
      if (!armed) return;                            // 仕掛けられなかった: 先に仕掛けてあった停止の条件は外さない
      haltOn = null;
      // 記録中は捕捉が記録にも入る: 時間軸は記録のまま（定期の読みを待たずに今の状態を読む。開いた直後に押しても）
      const recording = Boolean((await conn.call("recording.status", {}, { quiet: true }).catch(() => null))?.recording);
      tl.setSource(recording ? "recording" : "capture");
      const depth = Number(caps.la_depth ?? 0);
      // 速さ・プリトリガ・深いバッファの拍と形式（RLE / RAW）は Timeline で入れて覚えた設定（画面で決めない）。内蔵の捕捉が
      // 無い構成は深いバッファ
      const done = await tl.captureSaved({ deep: !depth, crossOnly: true });
      // 記録中なら捕捉は記録にも入る: 記録の時間軸でトリガの前後 1 ms を見せる（ピンと FPGA の信号・MCU が同じ時間軸に並ぶ）
      // 視点は共有の状態（ui.state）で動かす（結果の直前に多い出来事の「表示」と同じ。手元だけ動かすと共有の視点で戻される）
      if (recording && done?.trigger_time !== undefined && Number(caps.timebase_hz) > 0) {   // タイムベースは申告の値だけ
        const ms = Number(caps.timebase_hz) / 1000, t = done.trigger_time;
        await conn.call("ui.state.set", { view: { source: "recording", t0: Math.round(t - ms), t1: Math.round(t + ms) },
                                          cursors: { selection: t } }, { quiet: true });
      }
    } finally {
      if (armed) await app.act("fpga_obs.trigger_clear", {});
      capturingOn = null;
    }
  }

  async function addCondition() {
    const reason = await conditionError(conn, newCondExpr);          // 書き方はデーモンが確かめる
    if (reason !== null) {
      condError = `${$i18n("system.badCondition")} ${reason}`;
      return;
    }
    conditions = [...conditions.filter((c) => c.expr !== newCondExpr.trim()),
                  { name: newCondName.trim() || newCondExpr.trim(), expr: newCondExpr.trim() }];
    newCondName = newCondExpr = "";
    condError = null;
  }
  const removeCondition = (expr: string) => (conditions = conditions.filter((c) => c.expr !== expr));
  // 条件の回数: デーモンが数えた回数と、数えた範囲（値の数・最初と最後の値の間の秒）をそのまま
  const countText = (c: ConditionCount | undefined) => !c ? "—" : "error" in c ? "!" : String(c.count);
  const countTip = (c: ConditionCount | undefined) => !c ? $i18n("system.condLastTip") : "error" in c ? c.error
    : $i18n("system.condRange", { values: c.values, s: c.span_s === null ? "—" : c.span_s.toFixed(3), gaps: c.gaps, lost: c.lost });

  const fpgaOk = $derived(Boolean(fpga?.running && !fpga.stuck));
  const modeKey = (m?: string) => (m === "snapshot" ? "system.mode.snapshot" : "system.mode.stream");
</script>

<section class="peer" aria-label="FPGA">
  <header>
    <span class="dot" class:ok={fpgaOk} class:bad={fpga?.stuck} class:warn={fpga?.overflow_seen}></span>
    <strong>FPGA</strong>
    {#if fpga}
      {#if fpga.driving}
        <span>{$i18n(modeKey(fpga.mode))}</span>
        <span class="mono muted" title={$i18n("system.scansTip")}>{$i18n("fpgaObs.scans")} {fpga.scans ?? "—"}</span>
        {#if fpga.reopened}<span class="muted" title={$i18n("fpgaObs.autoReopenTip")}>{$i18n("fpgaObs.reopened", { n: fpga.reopened })}</span>{/if}
      {:else}
        <span class="muted">{$i18n("system.fpgaIdle")}</span>
        <button onclick={() => dock.show("fpgaObs")}>{$i18n("panel.fpgaObs")}</button>
      {/if}
    {:else if status}
      <span class="muted">{$i18n("system.noFpga")}</span>
    {/if}
  </header>
  {#if fpga?.stuck}
    <div class="note bad">{$i18n("fpgaObs.state.stuck")}
      {#if fpga.config}<button title={$i18n("system.reopenTip")} onclick={onReopen}>{$i18n("system.reopen")}</button>{/if}
    </div>
  {/if}
  <div class="sub-title">{fpga?.driving ? $i18n("system.latest") : $i18n("system.lastSeen")}</div>
  {#if fpga && fpga.latest?.length}
    <table class="grid">
      <thead><tr><th>{$i18n("fpgaObs.group")}</th><th>{$i18n("system.value")}</th><th></th></tr></thead>
      <tbody>
        {#each fpga.latest as l (l.group)}
          <tr class:stale={!fpga.driving}>
            <td class="mono">{l.group}</td><td class="mono">{fieldsText(l.fields, l.value)}</td>
            <td class="muted nowrap">{l.age_us !== null ? $i18n("system.ago", { s: (l.age_us / 1e6).toFixed(1) }) : ""}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    <div class="muted small">{$i18n("system.gaps")} {fpga.gaps ?? 0}</div>
  {:else if fpga}
    <div class="muted small">{fpga.latest_error ? $i18n("system.noValues") : $i18n("system.noChange")}</div>
  {/if}
  <div class="sub-title" title={$i18n("system.conditionsTip")}>{$i18n("system.conditions")}</div>
  {#if obsMatch && haltOn?.orphan}
    <div class="note">{$i18n(haltOn.halt ? "system.haltOrphan" : "system.laOrphan", { n: haltOn.count ?? 0 })}
      <button class="armed" onclick={clearHalt}>{$i18n("system.haltDisarm")}</button></div>
  {/if}
  {#if !conditions.length}<Hint text={$i18n("system.conditionsHint")} />{/if}
  {#if conditions.length}
    <label class="small" title={$i18n("system.condLastTip")}>{$i18n("system.condLast")}
      <input class="rate mono" inputmode="numeric" placeholder={$i18n("system.condLastPlaceholder")} bind:value={condLastText} />
      {$i18n("system.condLastUnit")}</label>
  {/if}
  <table class="grid">
    <thead><tr><th>{$i18n("system.name")}</th><th>{$i18n("system.condition")}</th><th class="nowrap">{$i18n("system.condCount")}</th><th></th></tr></thead>
    <tbody>
      {#each conditions as c (c.expr)}
        <tr>
          <td>{c.name}</td><td class="mono">{c.expr}</td>
          <td class="mono" title={countTip(condCounts[c.expr])}>{countText(condCounts[c.expr])}</td>
          <td class="nowrap">
            {#if obsMatch}
              <button title={$i18n(rec?.la_stream ? "system.captureOnStreamingTip" : "system.captureOnTip")}
                      disabled={capturingOn !== null || Boolean(rec?.la_stream)}
                      onclick={() => captureOn(c.expr)}>{capturingOn === c.expr ? $i18n("system.capturingOn") : $i18n("system.captureOn")}</button>
            {/if}
            {#if obsMatch && haltOn?.condition === c.expr && haltOn.halt !== false}
              <button class="armed" title={$i18n("system.haltArmedTip")} onclick={clearHalt}>{haltOn.count ? $i18n("system.haltFired", { n: haltOn.count }) : $i18n("system.haltArmed")}</button>
            {:else if obsMatch}
              <button title={$i18n("system.haltOnTip")} disabled={capturingOn !== null}
                      onclick={() => armHalt(c.expr)}>{$i18n("system.haltOn")}</button>
            {/if}
            <button class="x" title={$i18n("fpgaObs.remove")} aria-label={$i18n("fpgaObs.remove")} onclick={() => removeCondition(c.expr)}>✕</button></td>
        </tr>
      {/each}
      <tr>
        <td><input placeholder={$i18n("system.name")} bind:value={newCondName} /></td>
        <td><input class="mono" placeholder="restart changed and state=BUSY" bind:value={newCondExpr}
                   onkeydown={(e) => e.key === "Enter" && addCondition()} /></td>
        <td colspan="2"><button onclick={addCondition}>{$i18n("system.addWatch")}</button></td>
      </tr>
    </tbody>
  </table>
  {#if condError}<div class="note bad">{condError}</div>{/if}
</section>

<style>
  .peer { border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; min-width: 0; }
  .peer header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--line); }
  .dot.ok { background: var(--ok); }
  .dot.warn { background: var(--warn); }
  .dot.bad { background: var(--bad); }
  .sub-title { font-weight: 600; font-size: 12px; margin: 6px 0 2px; }
  .note { padding: 3px 8px; font-size: 12px; }
  .bad { color: var(--bad); }
  .small { font-size: 12px; padding: 2px 0; }
  input { width: 100%; min-width: 5em; box-sizing: border-box; }
  .grid { width: 100%; table-layout: auto; }
  .grid th { white-space: nowrap; text-align: left; }
  .nowrap, .nowrap button { white-space: nowrap; }
  .stale td { opacity: 0.6; }
  input.rate { width: 5em; min-width: 0; }
  .armed { border-color: var(--bad); color: var(--bad); }
  button.x { padding: 0 6px; }
</style>
