<script lang="ts">
  // Linux（JTAG で全コアを止める。API の linux.halt.*。CLI python -m fprobe.linux_halt と一対一）:
  // 開く設定・全コアの停止と再開・プロセスとスレッド・BKPT の回復・後始末・gdb のサーバ。
  // 考えない層: 値はすべて API の応答のまま描く（停止の上限の良し悪し・並べ替えの基準を持たない。比較は API の skew_verdict）
  import { errorText, type Methods, type Params, type Result } from "../../lib/api";
  import type { HaltGdbStatus, HaltStopReport } from "../../../../api/generated/fprobe_api";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { EMPTY_ATTACH_FORM, EMPTY_GDB_FORM, EMPTY_HALT_FORM, haltDebugAttachParams, haltGdbServeParams, haltOpenParams,
    haltThreadsParams, type DebugAttachForm, type GdbServeForm, type HaltForm } from "../../lib/linuxhalt";
  import { load, save } from "../../lib/prefs";

  const POLL_MS = 500;                                   // gdb のサーバが動いている間、その事実を読み直す間隔（描き直し）
  const { app, conn } = shared();

  // 欄は空から（前に書いた値だけを戻す）
  let form = $state<HaltForm>(restore("linuxHalt.open", EMPTY_HALT_FORM));
  let gdbForm = $state<GdbServeForm>(restore("linuxHalt.gdb", EMPTY_GDB_FORM));
  // 全コア停止で止めて調べる（linux.halt.debug.attach。止めた後は今あるデバッガの画面がそのプロセスを描く）
  let attachForm = $state<DebugAttachForm>(restore("linuxHalt.attach", EMPTY_ATTACH_FORM));
  let attached = $state<Result<"linux.halt.debug.attach"> | null>(null);
  let detached = $state<Result<"linux.halt.debug.close"> | null>(null);
  let threadsPid = $state(load("linuxHalt.threadsPid", ""));
  type Status = Result<"linux.halt.status">;
  type OpenStatus = Extract<Status, { open: true }>;
  let status = $state<Status | null>(null);
  let opened = $state<Result<"linux.halt.open"> | null>(null);
  let stop = $state<HaltStopReport | null>(null);            // 最後に操作が返した停止の報告（API の stop）
  let stopFrom = $state("");                                  // その操作の名前
  let processes = $state<Result<"linux.halt.processes"> | null>(null);
  let threads = $state<Result<"linux.halt.threads"> | null>(null);
  // recover・rescue・close の応答（そのまま JSON で描く）
  let result = $state<Result<"linux.halt.recover" | "linux.halt.rescue" | "linux.halt.close"> | null>(null);
  let gdb = $state<HaltGdbStatus | null>(null);
  let error = $state<string | null>(null);

  function restore<T extends object>(key: string, fallback: T): T {
    try {
      const v = JSON.parse(load(key, ""));
      if (v && typeof v === "object") return { ...fallback, ...v };
    } catch { /* 空から */ }
    return { ...fallback };
  }

  async function call<M extends keyof Methods>(method: M, params?: Params<M>): Promise<Result<M> | undefined> {
    if (!(await allowed(app))) return undefined;
    error = null;
    try {
      return await app.act(method, params);
    } catch (e) {
      error = errorText(e);
      return undefined;
    }
  }

  async function refresh() {
    try {
      status = await conn.call("linux.halt.status", {}, { quiet: true });
      gdb = status.open ? status.gdb : null;
    } catch (e) {
      error = errorText(e);
    }
  }

  async function open() {
    const built = haltOpenParams(form);
    if ("error" in built) { error = $i18n("linux.badField", { field: built.error }); return; }
    save("linuxHalt.open", JSON.stringify(form));
    const r = await call("linux.halt.open", built.params);
    if (r === undefined) return;
    opened = r;
    stop = processes = threads = result = null;
    await refresh();
  }

  // 停止の報告を返す操作（halt_all・processes・threads・recover）: 返った stop をそのまま描く
  type StopMethod = "linux.halt.halt_all" | "linux.halt.resume_all" | "linux.halt.processes" | "linux.halt.threads"
    | "linux.halt.recover";
  async function withStop<M extends StopMethod>(method: M, params?: Params<M>): Promise<Result<M> | undefined> {
    const r = await call(method, params);
    const reply: Result<StopMethod> | undefined = r;
    if (reply !== undefined && "stop" in reply) { stop = reply.stop; stopFrom = method; }
    await refresh();
    return r;
  }

  async function serve() {
    const built = haltGdbServeParams(gdbForm);
    if ("error" in built) { error = $i18n("linux.badField", { field: built.error }); return; }
    save("linuxHalt.gdb", JSON.stringify(gdbForm));
    const r = await call("linux.halt.gdb.serve", built.params);
    if (r !== undefined) gdb = r;
    await refresh();
  }

  async function attach() {
    const built = haltDebugAttachParams(attachForm);
    if ("error" in built) { error = $i18n("linux.badField", { field: built.error }); return; }
    save("linuxHalt.attach", JSON.stringify(attachForm));
    const r = await call("linux.halt.debug.attach", built.params);
    if (r !== undefined) { attached = r; detached = null; }
    await refresh();
  }

  async function threadsOf() {
    const built = haltThreadsParams(threadsPid);
    if ("error" in built) { error = $i18n("linux.badField", { field: built.error }); return; }
    save("linuxHalt.threadsPid", threadsPid);
    threads = (await withStop("linux.halt.threads", built.params)) ?? null;
  }

  $effect(() => { void refresh(); });
  // だれが（AI も）操作しても読み直す
  $effect(() => conn.on("activity", (e: { method: string }) => {
    if (e.method.startsWith("linux.halt.")) void refresh();
  }));
  // gdb のサーバが動いている間は、その事実（接続・終わり方）を読み直す（gdb の操作は API の記録に出ないため）
  $effect(() => {
    if (gdb?.state !== "listening" && gdb?.state !== "serving") return;
    const timer = setInterval(async () => {
      try {
        gdb = (await conn.call("linux.halt.gdb.status", {}, { quiet: true })).gdb;
      } catch (e) {
        error = errorText(e);
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  });

  const fmt = (v: unknown) => (v === null || v === undefined ? $i18n("linux.unknown")
    : typeof v === "object" ? JSON.stringify(v) : String(v));
  const hex = (v: unknown) => (typeof v === "number" ? "0x" + v.toString(16).toUpperCase().padStart(8, "0") : fmt(v));
  const bound = (ns: number | null | undefined) => (ns === null || ns === undefined ? $i18n("linuxHalt.boundUnknown") : `${ns} ns`);
  // 開いている口の事実の欄（後ろの FACTS の名前。backend で欄が違うので、名前で引いてそのまま描く）
  const fact = (s: OpenStatus | HaltGdbStatus, k: string): unknown => (s as Record<string, unknown>)[k];
  const FACTS = ["backend", "part", "n_cores", "hw_slots", "watch_slots", "debug_ap", "discovery", "actual_jtag_khz", "cti",
    "fpga_halt_watch", "fpga_request_times", "halting_debug", "opened_by"];
</script>

{#snippet report(r: HaltStopReport & { skew_verdict?: string; skew_limit_ns?: number })}
  <table class="mono small"><tbody>
    <tr><td>method</td><td>{r.method}</td></tr>
    <tr><td>cores_halted</td><td>{fmt(r.cores_halted)}</td></tr>
    <tr><td>skew_upper_bound_ns</td><td>{bound(r.skew_upper_bound_ns)}</td></tr>
    <!-- 上限が頼った仮定と、仮定なしの上限（PC の時計）: API の値のまま描く（どちらを信じるかは使う側が決める） -->
    {#if "assumptions" in r}<tr><td>assumptions</td><td class="wrap">{fmt(r.assumptions)}</td></tr>{/if}
    {#if "bound_without_assumptions_ns" in r}<tr><td>bound_without_assumptions_ns</td><td>{bound(r.bound_without_assumptions_ns)}</td></tr>{/if}
    {#if "skew_verdict" in r}<tr><td>skew_verdict</td><td>{r.skew_verdict} (skew_limit_ns {r.skew_limit_ns})</td></tr>{/if}
    <tr><td>basis</td><td class="wrap">{r.basis}</td></tr>
  </tbody></table>
{/snippet}

<div class="pane linuxhalt">
  <form class="pane-bar wrapbar" onsubmit={(e) => { e.preventDefault(); void open(); }}>
    <select bind:value={form.backend} aria-label="backend" aria-required="true">
      <option value="" disabled>backend</option><option value="fprobe">fprobe</option><option value="openocd">openocd</option>
    </select><span class="req" aria-hidden="true">*</span>
    <input class="mono num" bind:value={form.jtagKhz} placeholder="jtag_khz" aria-label="jtag_khz" aria-required="true" /><span class="req" aria-hidden="true">*</span>
    <input class="mono" bind:value={form.offsets} placeholder="offsets" aria-label="offsets" title={$i18n("linuxHalt.offsetsHint")} aria-required="true" /><span class="req" aria-hidden="true">*</span>
    <input class="mono short" bind:value={form.pageOffset} placeholder="page_offset" aria-label="page_offset" aria-required="true" /><span class="req" aria-hidden="true">*</span>
    <input class="mono short" bind:value={form.physOffset} placeholder="phys_offset" aria-label="phys_offset" aria-required="true" /><span class="req" aria-hidden="true">*</span>
    <input class="mono short" bind:value={form.linearMapEnd} placeholder="linear_map_end" aria-label="linear_map_end" aria-required="true" /><span class="req" aria-hidden="true">*</span>
    <input class="mono num" bind:value={form.maxTasks} placeholder="max_tasks" aria-label="max_tasks" aria-required="true" /><span class="req" aria-hidden="true">*</span>
    <input class="mono num" bind:value={form.maxThreads} placeholder="max_threads" aria-label="max_threads" aria-required="true" /><span class="req" aria-hidden="true">*</span>
    <select bind:value={form.mmuFormat} aria-label="mmu_format" aria-required="true">
      <option value="" disabled>mmu_format</option><option value="short">short</option><option value="lpae">lpae</option>
    </select><span class="req" aria-hidden="true">*</span>
    <input class="mono" bind:value={form.journal} placeholder="journal" aria-label="journal" title={$i18n("linuxHalt.journalHint")} aria-required="true" /><span class="req" aria-hidden="true">*</span>
    <select bind:value={form.fpgaRequestTimes} aria-label="fpga_request_times" title={$i18n("linuxHalt.requestTimesHint")} aria-required="true">
      <option value="" disabled>fpga_request_times</option><option value="true">true</option><option value="false">false</option>
    </select><span class="req" aria-hidden="true">*</span>
    <button class="primary small">{$i18n("linuxHalt.open")}</button>
    {#if status?.open}<button type="button" class="small" onclick={async () => { const r = await call("linux.halt.close"); if (r !== undefined) { result = r; opened = null; } await refresh(); }}>{$i18n("linuxHalt.close")}</button>{/if}
  </form>
  <details class="pad">
    <summary>{$i18n("linuxHalt.backendFields")}</summary>
    <div class="bar">
      <strong>fprobe</strong>
      <input class="mono short" bind:value={form.part} placeholder="part" aria-label="part" />
      <input class="mono short" bind:value={form.ctiBoard} placeholder="cti_board" aria-label="cti_board" />
      <input class="mono num" bind:value={form.ctiRequestBoundNs} placeholder="cti_request_bound_ns" aria-label="cti_request_bound_ns" />
      <input class="mono num" bind:value={form.ctiEventBoundNs} placeholder="cti_event_bound_ns" aria-label="cti_event_bound_ns" title={$i18n("linuxHalt.unmeasuredHint")} />
      <input class="mono num" bind:value={form.ctiHwWaitS} placeholder="cti_hw_wait_s" aria-label="cti_hw_wait_s" />
      <input class="mono num" bind:value={form.ctiAckPolls} placeholder="cti_ack_polls" aria-label="cti_ack_polls" />
      <input class="mono num" bind:value={form.watchBoundNs} placeholder="fpga_halt_watch_bound_ns" aria-label="fpga_halt_watch_bound_ns" title={$i18n("linuxHalt.unmeasuredHint")} />
      <input class="mono num" bind:value={form.watchPollS} placeholder="fpga_halt_watch_poll_s" aria-label="fpga_halt_watch_poll_s" />
    </div>
    <div class="bar">
      <strong>openocd</strong>
      <input class="mono short" bind:value={form.ocdBoard} placeholder="ocd_board" aria-label="ocd_board" />
      <input class="mono num" bind:value={form.ocdPort} placeholder="ocd_port" aria-label="ocd_port" />
      <input class="mono num" bind:value={form.ocdTimeoutS} placeholder="ocd_timeout_s" aria-label="ocd_timeout_s" />
      <input class="mono num" bind:value={form.hwSlots} placeholder="hw_slots" aria-label="hw_slots" />
      <input class="mono num" bind:value={form.watchSlots} placeholder="watch_slots" aria-label="watch_slots" />
    </div>
  </details>
  {#if error}<div class="bad pad">{error}</div>{/if}
  {#if !status?.open}
    <div class="muted pad">{$i18n("linuxHalt.none")}</div>
    {#if result}<pre class="mono small pad">{JSON.stringify(result, null, 1)}</pre>{/if}
  {:else}
    <div class="body">
      <div class="bar">
        <button class="small" onclick={() => void refresh()}>{$i18n("linuxHalt.status")}</button>
        <button class="small" onclick={() => void withStop("linux.halt.halt_all")}>{$i18n("linuxHalt.haltAll")}</button>
        <button class="small" onclick={() => void withStop("linux.halt.resume_all")}>{$i18n("linuxHalt.resumeAll")}</button>
        <button class="small" onclick={async () => { processes = (await withStop("linux.halt.processes")) ?? null; }}>{$i18n("linuxHalt.processes")}</button>
        <input class="mono num" bind:value={threadsPid} placeholder="pid" aria-label="pid" />
        <button class="small" onclick={() => void threadsOf()}>{$i18n("linuxHalt.threads")}</button>
        <button class="small" title={$i18n("linuxHalt.recoverHint")} onclick={async () => { result = (await withStop("linux.halt.recover")) ?? null; }}>{$i18n("linuxHalt.recover")}</button>
        <button class="small" title={$i18n("linuxHalt.rescueHint")} onclick={async () => { result = (await call("linux.halt.rescue")) ?? null; await refresh(); }}>{$i18n("linuxHalt.rescue")}</button>
      </div>
      <details open>
        <summary>{$i18n("linuxHalt.facts")}</summary>
        <table class="mono small"><tbody>
          {#each FACTS.filter((k) => k in status!) as k (k)}<tr><td>{k}</td><td>{fmt(fact(status, k))}</td></tr>{/each}
          <tr><td>options</td><td class="wrap">{fmt(status.options)}</td></tr>
        </tbody></table>
        {#if status.occupies}
          <div class="mono small">probe_dap={fmt(status.occupies.probe_dap)} {status.occupies.device_lock}</div>
          {#each status.occupies.refused_while_open as what (what)}<div class="mono small muted">✕ {what}</div>{/each}
        {/if}
      </details>
      <section>
        <strong>{$i18n("linuxHalt.cores")}</strong>
        {#if status.cores}
          <table class="mono small">
            <thead><tr><th>core</th><th>running</th><th>breakpoints</th><th>watchpoints</th></tr></thead>
            <tbody>{#each status.cores as c (c.core)}<tr><td>{c.core}</td><td>{fmt(c.running)}</td><td>{fmt(c.breakpoints)}</td><td>{fmt(c.watchpoints)}</td></tr>{/each}</tbody>
          </table>
        {:else}<div class="muted small">{status.cores_unread}</div>{/if}
      </section>
      <section>
        <strong>journal</strong> <span class="mono small">{status.journal.path} exists={fmt(status.journal.exists)}</span>
        {#if status.journal.error}<div class="bad small">{status.journal.error}</div>{/if}
        {#each status.journal.entries ?? [] as e, i (i)}<div class="mono small">pa={hex(e.pa)} width={e.width} owners={fmt(e.owners)}</div>{/each}
      </section>
      {#if stop}
        <section><strong>stop</strong> <span class="muted small">{stopFrom}</span>{@render report(stop)}</section>
      {/if}
      {#if processes}
        <section>
          <strong>{$i18n("linuxHalt.processes")}</strong>
          <table class="mono small">
            <thead><tr><th>tgid</th><th>comm</th><th>has_mm</th></tr></thead>
            <tbody>{#each processes.processes as p, i (i)}<tr><td>{p.tgid}</td><td>{p.comm}</td><td>{fmt(p.has_mm)}</td></tr>{/each}</tbody>
          </table>
        </section>
      {/if}
      {#if threads}
        <section>
          <strong>{$i18n("linuxHalt.threads")}</strong> <span class="mono small">pid {threads.pid} {threads.comm} pgd={hex(threads.pgd)}</span>
          <table class="mono small">
            <thead><tr><th>tid</th><th>comm</th><th>task</th></tr></thead>
            <tbody>{#each threads.threads as t, i (i)}<tr><td>{t.tid}</td><td>{t.comm}</td><td>{hex(t.task)}</td></tr>{/each}</tbody>
          </table>
        </section>
      {/if}
      {#if result}<pre class="mono small">{JSON.stringify(result, null, 1)}</pre>{/if}
      <details open>
        <summary>{$i18n("linuxHalt.debug")}</summary>
        <form class="bar" onsubmit={(e) => { e.preventDefault(); void attach(); }}>
          <input class="mono num" bind:value={attachForm.pid} placeholder="pid" aria-label="pid" aria-required="true" /><span class="req" aria-hidden="true">*</span>
          <input class="mono" bind:value={attachForm.elf} placeholder="elf" aria-label="elf" title={$i18n("linuxHalt.elfHint")} aria-required="true" /><span class="req" aria-hidden="true">*</span>
          <input class="mono num" bind:value={attachForm.skewLimitNs} placeholder="skew_limit_ns" aria-label="skew_limit_ns" title={$i18n("linuxHalt.skewLimitHint")} aria-required="true" /><span class="req" aria-hidden="true">*</span>
          <select bind:value={attachForm.threadId} aria-label="thread_id" aria-required="true">
            <option value="" disabled>thread_id</option><option value="stack">stack</option><option value="contextidr">contextidr</option>
          </select><span class="req" aria-hidden="true">*</span>
          <select bind:value={attachForm.kernelIsa} aria-label="kernel_isa" aria-required="true">
            <option value="" disabled>kernel_isa</option><option value="arm">arm</option><option value="thumb">thumb</option>
          </select><span class="req" aria-hidden="true">*</span>
          <input class="mono num" bind:value={attachForm.maxStepRetries} placeholder="max_step_retries" aria-label="max_step_retries" aria-required="true" /><span class="req" aria-hidden="true">*</span>
          <select bind:value={attachForm.bpUnmapped} aria-label="bp_unmapped" aria-required="true">
            <option value="" disabled>bp_unmapped</option><option value="error">error</option><option value="hw">hw</option>
          </select><span class="req" aria-hidden="true">*</span>
          <input class="mono num" bind:value={attachForm.maxHaltS} placeholder="max_halt_s" aria-label="max_halt_s" title={$i18n("linuxHalt.maxHaltHint")} aria-required="true" /><span class="req" aria-hidden="true">*</span>
          <input class="mono" bind:value={attachForm.fetchFromTarget} placeholder="fetch_from_target" aria-label="fetch_from_target" title={$i18n("linuxHalt.fetchHint")} />
          <select bind:value={attachForm.breakpointKind} aria-label="breakpoint_kind" title={$i18n("linuxHalt.bpKindHint")}>
            <option value="">breakpoint_kind</option><option value="sw">sw</option><option value="hw">hw</option>
          </select>
          <input class="mono num" bind:value={attachForm.stepMaxInstructions} placeholder="step_max_instructions" aria-label="step_max_instructions" title={$i18n("linuxHalt.stepLimitHint")} />
          <input class="mono num" bind:value={attachForm.stepTimeoutS} placeholder="step_timeout_s" aria-label="step_timeout_s" title={$i18n("linuxHalt.stepLimitHint")} />
          <button class="primary small">{$i18n("linuxHalt.attach")}</button>
          <button type="button" class="small" onclick={async () => { const r = await call("linux.halt.debug.close"); if (r !== undefined) { detached = r; attached = null; } await refresh(); }}>{$i18n("linuxHalt.detach")}</button>
        </form>
        {#if attached}
          <table class="mono small"><tbody>
            {#each ["pid", "comm", "running", "breakpoint_kind", "step_max_instructions", "step_timeout_s", "sources_fetched", "fetched"] as k (k)}
              <tr><td>{k}</td><td>{fmt((attached as Record<string, unknown>)[k])}</td></tr>
            {/each}
          </tbody></table>
          {#if attached.attach_stop}<div class="small">attach_stop</div>{@render report(attached.attach_stop)}{/if}
        {/if}
        {#if detached}<pre class="mono small">{JSON.stringify(detached, null, 1)}</pre>{/if}
      </details>
      <details open>
        <summary>gdb</summary>
        <form class="bar" onsubmit={(e) => { e.preventDefault(); void serve(); }}>
          <input class="mono num" bind:value={gdbForm.pid} placeholder="pid" aria-label="pid" aria-required="true" /><span class="req" aria-hidden="true">*</span>
          <input class="mono num" bind:value={gdbForm.port} placeholder="port" aria-label="port" aria-required="true" /><span class="req" aria-hidden="true">*</span>
          <input class="mono num" bind:value={gdbForm.skewLimitNs} placeholder="skew_limit_ns" aria-label="skew_limit_ns" title={$i18n("linuxHalt.skewLimitHint")} aria-required="true" /><span class="req" aria-hidden="true">*</span>
          <select bind:value={gdbForm.threadId} aria-label="thread_id" aria-required="true">
            <option value="" disabled>thread_id</option><option value="stack">stack</option><option value="contextidr">contextidr</option>
          </select><span class="req" aria-hidden="true">*</span>
          <select bind:value={gdbForm.kernelIsa} aria-label="kernel_isa" aria-required="true">
            <option value="" disabled>kernel_isa</option><option value="arm">arm</option><option value="thumb">thumb</option>
          </select><span class="req" aria-hidden="true">*</span>
          <input class="mono num" bind:value={gdbForm.maxStepRetries} placeholder="max_step_retries" aria-label="max_step_retries" aria-required="true" /><span class="req" aria-hidden="true">*</span>
          <select bind:value={gdbForm.bpUnmapped} aria-label="bp_unmapped" aria-required="true">
            <option value="" disabled>bp_unmapped</option><option value="error">error</option><option value="hw">hw</option>
          </select><span class="req" aria-hidden="true">*</span>
          <input class="mono" bind:value={gdbForm.log} placeholder="log" aria-label="log" />
          <button class="primary small">{$i18n("linuxHalt.gdbServe")}</button>
          <button type="button" class="small" onclick={async () => { const r = await call("linux.halt.gdb.stop"); if (r !== undefined) gdb = r.gdb; await refresh(); }}>{$i18n("linuxHalt.gdbStop")}</button>
        </form>
        {#if gdb}
          <table class="mono small"><tbody>
            {#each ["state", "connect", "port", "pid", "comm", "pgd", "threads", "ended_by", "error", "recovered", "cleanup_errors", "cleanup_reconnected", "cleanup_errors_after_reconnect"].filter((k) => k in gdb!) as k (k)}
              <tr><td>{k}</td><td class:bad={k === "error" && fact(gdb!, k)}>{k === "pgd" ? hex(fact(gdb!, k)) : fmt(fact(gdb!, k))}</td></tr>
            {/each}
          </tbody></table>
          {#if gdb.stop_for_attach}<div class="small">stop_for_attach</div>{@render report(gdb.stop_for_attach)}{/if}
          {#if gdb.attach_stop}<div class="small">attach_stop</div>{@render report(gdb.attach_stop)}{/if}
          {#if gdb.last_stop}<div class="small">last_stop</div>{@render report(gdb.last_stop)}{/if}
          {#if gdb.stats}<pre class="mono small">{JSON.stringify(gdb.stats, null, 1)}</pre>{/if}
        {/if}
      </details>
      {#if opened?.design_switch}<div class="mono small muted">design_switch {fmt(opened.design_switch)}</div>{/if}
    </div>
  {/if}
</div>

<style>
  .linuxhalt { display: flex; flex-direction: column; }
  .body { flex: 1; overflow: auto; padding: 4px 8px; display: flex; flex-direction: column; gap: 6px; }
  .bar, .wrapbar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .short { width: 7em; }
  .num { width: 6em; }
  .small { font-size: 12px; }
  table td, table th { padding: 0 8px 0 0; vertical-align: top; text-align: left; }
  .wrap { white-space: pre-wrap; word-break: break-word; }
  pre { margin: 2px 0; white-space: pre-wrap; }
  .bad { color: var(--bad); }
  .pad { padding: 4px 8px; }
</style>
