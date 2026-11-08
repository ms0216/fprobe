<script lang="ts">
  import { tick, untrack } from "svelte";
  import Req from "./Req.svelte";
  // Linux（JTAG で全コアを止める。API の linux.halt.*。CLI python -m fprobe.linux_halt と一対一）:
  // 開く設定・全コアの停止と再開・プロセスとスレッド・BKPT の回復・後始末・gdb のサーバ。
  // 考えない層: 値はすべて API の応答のまま描く（停止の上限の良し悪し・並べ替えの基準を持たない。比較は API の skew_verdict）
  import { errorText, type Methods, type Params, type Result } from "../../lib/api";
  import type { HaltGdbStatus, HaltStopReport } from "../../../../api/generated/fprobe_api";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { EMPTY_GDB_FORM, attachFormOf, shouldRefreshOn, exampleAttachForm, exampleHaltForm, haltDebugAttachParams, haltFormOf, haltGdbServeParams, haltOpenParams,
    haltThreadsParams, type DebugAttachForm, type GdbServeForm, type HaltForm } from "../../lib/linuxhalt";
  import { load, save } from "../../lib/prefs";

  const POLL_MS = 500;                                   // gdb のサーバが動いている間、その事実を読み直す間隔（描き直し）
  const { app, conn } = shared();

  // 欄の最初の値は、前に書いた値、無ければ API の仕様の「例・推奨の値」（examples。見えて・書き換えられ・送るときは欄の値を明示して送る）
  let form = $state<HaltForm>(restore("linuxHalt.open", exampleHaltForm()));
  let gdbForm = $state<GdbServeForm>(restore("linuxHalt.gdb", EMPTY_GDB_FORM));
  // 全コア停止で止めて調べる（linux.halt.debug.attach。止めた後は今あるデバッガの画面がそのプロセスを描く）
  let attachForm = $state<DebugAttachForm>(restore("linuxHalt.attach", exampleAttachForm()));
  let attached = $state<Result<"linux.halt.debug.attach"> | null>(null);
  // 空・誤りの欄の印（赤い枠と aria-invalid）。Open / Attach を押して断られたあとだけ出し、欄を直すと消える（欄の値から引き直す）
  let openChecked = $state(false);
  let attachChecked = $state(false);
  let advOpen = $state(false);                            // attach の Advanced（必須の欄が空のまま閉じていたら開く）
  let openForm: HTMLFormElement | undefined = $state();
  let attachFormEl: HTMLFormElement | undefined = $state();
  const ADV_FIELDS = ["skew_limit_ns", "thread_id", "kernel_isa", "max_step_retries", "bp_unmapped", "max_halt_s",
    "fetch_from_target", "breakpoint_kind", "step_max_instructions", "step_timeout_s"];
  let detached = $state<Result<"linux.halt.debug.close"> | null>(null);
  let threadsPid = $state(load("linuxHalt.threadsPid", ""));
  // 設定ファイル（API の linux.halt.profile.save / load。欄に入れるだけ。開く・attach は人が押す）。パスは前に書いた値（見せ方の便利さ）
  let profilePath = $state(load("linuxHalt.profilePath", ""));
  let profileOffsets = $state<boolean | null | undefined>(undefined);   // 読み込んだ offsets の SHA-256 の一致（API の offsets_matches）
  $effect(() => { save("linuxHalt.profilePath", profilePath); });
  type Status = Result<"linux.halt.status">;
  type OpenStatus = Extract<Status, { open: true }>;
  let status = $state<Status | null>(null);
  // Settings の枠: Open の前は開き、Open したら畳む（切り替わりのときだけ）。そのあとは人が開閉する。
  // `open={!status?.open}` と書くと、状態の更新や欄の入力のたびに再適用され、人が開いた枠が閉じて入力が失われた（GUI の評価 B1）
  let settingsOpen = $state(true);
  let wasOpen: boolean | undefined;
  $effect(() => {
    const isOpen = !!status?.open;
    if (wasOpen !== isOpen) { settingsOpen = !isOpen; wasOpen = isOpen; }
  });
  // 最後に書いた値を残す（次に開いたとき・別の窓でも同じ欄の値から始まる）
  $effect(() => { save("linuxHalt.open", JSON.stringify(form)); });
  $effect(() => { save("linuxHalt.attach", JSON.stringify(attachForm)); });
  let attachOptions = $state<Result<"linux.halt.debug.status">["options"] | null>(null);   // 今の attach で使っている設定（API の事実）
  // 開いている間・attach している間は、欄に「使っている値」を映す（AI が open / attach したときも人の画面に同じ値が見える）。欄は変えられない
  const openLocked = $derived(!!status?.open);
  const openBad = $derived.by(() => { if (!openChecked) return []; const b = haltOpenParams(form); return "errors" in b ? b.errors : []; });
  const attachBad = $derived.by(() => { if (!attachChecked) return []; const b = haltDebugAttachParams(attachForm); return "errors" in b ? b.errors : []; });
  const inv = (bad: string[], k: string) => (bad.includes(k) ? "true" : undefined);
  const attachLocked = $derived(!!attachOptions);
  let opened = $state<Result<"linux.halt.open"> | null>(null);
  let stop = $state<HaltStopReport | null>(null);            // 最後に操作が返した停止の報告（API の stop）
  let stopFrom = $state("");                                  // その操作の名前
  let processes = $state<Result<"linux.halt.processes"> | null>(null);
  let threads = $state<Result<"linux.halt.threads"> | null>(null);
  // プロセスの一覧の見せ方の絞り込み（人が選ぶ。comm の部分一致・has_mm=false を隠す。API の値で隠すだけで、判断ではない）
  let procFilter = $state("");
  let hideNoMm = $state(false);
  const shownProcesses = $derived((processes?.processes ?? []).filter((p) =>
    p.comm.toLowerCase().includes(procFilter.trim().toLowerCase()) && !(hideNoMm && p.has_mm === false)));
  // 一覧の行を押すと、その pid を attach と threads の欄に入れる（attach 中は attach の欄は変えられない）
  function pickPid(tgid: number) {
    threadsPid = String(tgid);
    if (!attachLocked) attachForm.pid = String(tgid);
  }
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
    if (!(await allowed(app, undefined, String(method)))) return undefined;
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
      error = null;                                   // 読めたら、前の読めなかった文は消す（つながっているのに「Not connected」が残っていた）
      gdb = status.open ? status.gdb : null;
      if (status.open) form = haltFormOf(form, status.options as Record<string, unknown>);
      if (status.open && status.debug && !status.debug.closed) {
        const d = await conn.call("linux.halt.debug.status", {}, { quiet: true });
        attachOptions = d.options;
        if (d.options) attachForm = attachFormOf(attachForm, d.options as Record<string, unknown>);
      } else {
        attachOptions = null;
      }
    } catch (e) {
      error = errorText(e);
    }
  }

  async function open() {
    const built = haltOpenParams(form);
    if ("errors" in built) {
      openChecked = true; settingsOpen = true;
      error = $i18n("linuxHalt.badFields", { fields: built.errors.join(", ") });
      await tick();
      openForm?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
      return;
    }
    openChecked = false;
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
    if ("errors" in built) {
      attachChecked = true;
      if (built.errors.some((k) => ADV_FIELDS.includes(k))) advOpen = true;
      error = $i18n("linuxHalt.badFields", { fields: built.errors.join(", ") });
      await tick();
      attachFormEl?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
      return;
    }
    attachChecked = false;
    save("linuxHalt.attach", JSON.stringify(attachForm));
    const r = await call("linux.halt.debug.attach", built.params);
    if (r !== undefined) { attached = r; detached = null; }
    await refresh();
  }

  async function profileSave() {
    await call("linux.halt.profile.save", { path: profilePath.trim() });
  }

  async function profileLoad() {
    const r = await call("linux.halt.profile.load", { path: profilePath.trim() });
    if (r === undefined) return;
    form = haltFormOf(form, r.open as Record<string, unknown>);
    if (r.attach) attachForm = attachFormOf(attachForm, r.attach as Record<string, unknown>);
    profileOffsets = r.offsets_matches;
  }

  async function threadsOf() {
    const built = haltThreadsParams(threadsPid);
    if ("error" in built) { error = $i18n("linux.badField", { field: built.error }); return; }
    save("linuxHalt.threadsPid", threadsPid);
    threads = (await withStop("linux.halt.threads", built.params)) ?? null;
  }

  // つながってから読む（つながる前の読みの失敗の文が、つながった後も残っていた）。つながったとき・つなぎ直したときに読み直す
  // $app 全体ではなく「開いているか」だけを見る（$app は読み直しの記録のたびに更新され、読み直し → 記録 → $app の更新 →
  // 読み直し… の循環になった。2026-11-05 実機: attach したままページを読み直すと、毎秒数十回の linux.halt.debug.status）
  const linkOpen = $derived($app.link === "open");
  $effect(() => { if (linkOpen) untrack(() => void refresh()); });
  // だれが（AI も）操作しても読み直す
  // 読み直す自分の読み（status・debug.status）の記録では読み直さない（debug.status は読みの種別で、いまは記録されない。2026-11-05 までは
  // 操作の種別で、静かな読みでも記録され、通知
  // activity → 読み直し → 記録 → 通知… の循環になる（2026-11-04 実機: attach 中に毎秒 500 回の問い合わせ）。状態を変える操作だけ。
  $effect(() => conn.on("activity", (e: { method: string }) => {
    if (shouldRefreshOn(e.method)) void refresh();
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
  <div class="head pad">
    <span class="chip" class:on={status?.open}>● {status?.open ? `${status.backend}${"part" in status ? " · " + status.part : ""} · ${$i18n("linuxHalt.nCores", { n: status.n_cores })}` : $i18n("linuxHalt.none")}</span>
    <button class="primary small" form="halt-open" disabled={openLocked} title={$i18n("linuxHalt.openHint")}>{$i18n("linuxHalt.open")}</button>
    {#if status?.open}<button type="button" class="small" onclick={async () => { const r = await call("linux.halt.close"); if (r !== undefined) { result = r; opened = null; } await refresh(); }}>{$i18n("linuxHalt.close")}</button>{/if}
  </div>
  <details class="pad" bind:open={settingsOpen}>
    <summary>{$i18n("linuxHalt.settings")}</summary>
  <form id="halt-open" class="pane-bar wrapbar" bind:this={openForm} onsubmit={(e) => { e.preventDefault(); void open(); }}>
    <label class="fld"><span class="nm" title="backend">{$i18n("linuxHalt.f.backend")}<Req /></span><select disabled={openLocked} bind:value={form.backend} aria-label="backend" aria-required="true" aria-invalid={inv(openBad, "backend")}>
      <option value="" disabled>{$i18n("linuxHalt.choose")}</option><option value="fprobe">fprobe</option><option value="openocd">openocd</option>
    </select></label>
    <label class="fld"><span class="nm" title="jtag_khz">{$i18n("linuxHalt.f.jtag_khz")}<Req /></span><input class="mono num" bind:value={form.jtagKhz} placeholder="jtag_khz" aria-label="jtag_khz" aria-required="true" aria-invalid={inv(openBad, "jtag_khz")} disabled={openLocked} /></label>
    <label class="fld wide"><span class="nm" title="offsets">{$i18n("linuxHalt.f.offsets")}<Req /></span><input class="mono" bind:value={form.offsets} placeholder="offsets" aria-label="offsets" title={form.offsets ? `${form.offsets}
${$i18n("linuxHalt.offsetsHint")}` : $i18n("linuxHalt.offsetsHint")} aria-required="true" aria-invalid={inv(openBad, "offsets")} disabled={openLocked} /></label>
    <label class="fld"><span class="nm" title="page_offset">{$i18n("linuxHalt.f.page_offset")}<Req /></span><input class="mono short" bind:value={form.pageOffset} placeholder="page_offset" aria-label="page_offset" aria-required="true" aria-invalid={inv(openBad, "page_offset")} disabled={openLocked} /></label>
    <label class="fld"><span class="nm" title="phys_offset">{$i18n("linuxHalt.f.phys_offset")}<Req /></span><input class="mono short" bind:value={form.physOffset} placeholder="phys_offset" aria-label="phys_offset" aria-required="true" aria-invalid={inv(openBad, "phys_offset")} disabled={openLocked} /></label>
    <label class="fld"><span class="nm" title="linear_map_end">{$i18n("linuxHalt.f.linear_map_end")}<Req /></span><input class="mono short" bind:value={form.linearMapEnd} placeholder="linear_map_end" aria-label="linear_map_end" aria-required="true" aria-invalid={inv(openBad, "linear_map_end")} disabled={openLocked} /></label>
    <label class="fld"><span class="nm" title="max_tasks">{$i18n("linuxHalt.f.max_tasks")}<Req /></span><input class="mono num" bind:value={form.maxTasks} placeholder="max_tasks" aria-label="max_tasks" aria-required="true" aria-invalid={inv(openBad, "max_tasks")} disabled={openLocked} /></label>
    <label class="fld"><span class="nm" title="max_threads">{$i18n("linuxHalt.f.max_threads")}<Req /></span><input class="mono num" bind:value={form.maxThreads} placeholder="max_threads" aria-label="max_threads" aria-required="true" aria-invalid={inv(openBad, "max_threads")} disabled={openLocked} /></label>
    <label class="fld"><span class="nm" title="mmu_format">{$i18n("linuxHalt.f.mmu_format")}<Req /></span><select disabled={openLocked} bind:value={form.mmuFormat} aria-label="mmu_format" aria-required="true" aria-invalid={inv(openBad, "mmu_format")}>
      <option value="" disabled>{$i18n("linuxHalt.choose")}</option><option value="short">short</option><option value="lpae">lpae</option>
    </select></label>
    <label class="fld wide"><span class="nm" title="journal">{$i18n("linuxHalt.f.journal")}<Req /></span><input class="mono" bind:value={form.journal} placeholder="journal" aria-label="journal" title={form.journal ? `${form.journal}
${$i18n("linuxHalt.journalHint")}` : $i18n("linuxHalt.journalHint")} aria-required="true" aria-invalid={inv(openBad, "journal")} disabled={openLocked} /></label>
    <label class="fld"><span class="nm" title="fpga_request_times">{$i18n("linuxHalt.f.fpga_request_times")}<Req /></span><select disabled={openLocked} bind:value={form.fpgaRequestTimes} aria-label="fpga_request_times" title={$i18n("linuxHalt.requestTimesHint")} aria-required="true" aria-invalid={inv(openBad, "fpga_request_times")}>
      <option value="" disabled>{$i18n("linuxHalt.choose")}</option><option value="true">true</option><option value="false">false</option>
    </select></label>
  </form>
    <div class="bar">
      <span class="nm" title="linux.halt.profile.*">{$i18n("linuxHalt.profile")}</span>
      <input class="mono grow" bind:value={profilePath} placeholder="path" aria-label={$i18n("linuxHalt.profilePath")} title={profilePath} />
      <button type="button" class="small" disabled={!status?.open || !profilePath.trim()} title={$i18n("linuxHalt.profileSaveHint")} onclick={() => void profileSave()}>{$i18n("linuxHalt.profileSave")}</button>
      <button type="button" class="small" disabled={!profilePath.trim()} title={$i18n("linuxHalt.profileLoadHint")} onclick={() => void profileLoad()}>{$i18n("linuxHalt.profileLoad")}</button>
      {#if profileOffsets !== undefined}<span class="mono small">{$i18n("linuxHalt.profileOffsets", { state: profileOffsets === null ? $i18n("linuxHalt.profileUnread") : profileOffsets ? $i18n("linuxHalt.profileSame") : $i18n("linuxHalt.profileDiffers") })}</span>{/if}
    </div>
  </details>
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
      </div>
      <details class="more">
        <summary>{$i18n("linuxHalt.more")}</summary>
        <div class="bar">
        <input class="mono num" bind:value={threadsPid} placeholder="pid" aria-label="pid" />
        <button class="small" onclick={() => void threadsOf()}>{$i18n("linuxHalt.threads")}</button>
        <button class="small" title={$i18n("linuxHalt.recoverHint")} onclick={async () => { result = (await withStop("linux.halt.recover")) ?? null; }}>{$i18n("linuxHalt.recover")}</button>
        <button class="small" title={$i18n("linuxHalt.rescueHint")} onclick={async () => { result = (await call("linux.halt.rescue")) ?? null; await refresh(); }}>{$i18n("linuxHalt.rescue")}</button>
        </div>
      </details>
      <details>
        <summary>{$i18n("linuxHalt.facts")}</summary>
        <table class="mono small"><tbody>
          {#each FACTS.filter((k) => k in status!) as k (k)}<tr><td>{k}</td><td>{fmt(fact(status, k))}</td></tr>{/each}
        </tbody></table>
        {#if status.occupies}
          <div class="mono small">probe_dap={fmt(status.occupies.probe_dap)} {$i18n(`linuxHalt.lock.${status.occupies.device_lock}`)}</div>
          {#each status.occupies.refused_while_open as what (what)}<div class="mono small muted" title={what}>✕ {$i18n(`linuxHalt.refused.${what}`)}</div>{/each}
        {/if}
      </details>
      <section class="cores">
        <strong>{$i18n("linuxHalt.cores")}</strong>
        {#if status.cores}
          {#each status.cores as c (c.core)}
            <span class="chip mono small" class:on={!c.running} title={`running=${fmt(c.running)} breakpoints=${fmt(c.breakpoints)} watchpoints=${fmt(c.watchpoints)}`}>{c.core}: {c.running ? $i18n("linuxHalt.running") : $i18n("linuxHalt.halted")} · bp {c.breakpoints.length} · wp {c.watchpoints.length}</span>
          {/each}
        {:else}<span class="muted small">{status.cores_unread}</span>{/if}
      </section>
      <details>
        <summary>{$i18n("linuxHalt.journalTitle")}</summary>
        <strong>{$i18n("linuxHalt.journalTitle")}</strong> <span class="mono small" title={status.journal.path}>{status.journal.path} exists={fmt(status.journal.exists)}</span>
        {#if status.journal.error}<div class="bad small">{status.journal.error}</div>{/if}
        {#each status.journal.entries ?? [] as e, i (i)}<div class="mono small">pa={hex(e.pa)} width={e.width} owners={fmt(e.owners)}</div>{/each}
      </details>
      <details open>
        <summary>{$i18n("linuxHalt.debug")}</summary>
        <form class="bar" bind:this={attachFormEl} onsubmit={(e) => { e.preventDefault(); void attach(); }}>
          <label class="fld"><span class="nm" title="pid">{$i18n("linuxHalt.f.pid")}<Req /></span><input class="mono num" bind:value={attachForm.pid} placeholder="pid" aria-label="pid" aria-required="true" aria-invalid={inv(attachBad, "pid")} disabled={attachLocked} /></label>
          <label class="fld wide"><span class="nm" title="elf">{$i18n("linuxHalt.f.elf")}<Req /></span><input class="mono" bind:value={attachForm.elf} placeholder="elf" aria-label="elf" title={attachForm.elf ? `${attachForm.elf}
${$i18n("linuxHalt.elfHint")}` : $i18n("linuxHalt.elfHint")} aria-required="true" aria-invalid={inv(attachBad, "elf")} disabled={attachLocked} /></label>
          <details class="adv" bind:open={advOpen}>
            <summary>{$i18n("linuxHalt.advanced")} <span class="mono muted small">{attachForm.skewLimitNs} ns · {attachForm.maxHaltS} s · {attachForm.breakpointKind || "-"} bp · {attachForm.stepMaxInstructions} / {attachForm.stepTimeoutS} s</span>{#if !attachForm.breakpointKind} <span class="bad small">⚠ {$i18n("linuxHalt.bpKindWarn")}</span>{/if}</summary>
          <label class="fld"><span class="nm" title="skew_limit_ns">{$i18n("linuxHalt.f.skew_limit_ns")}<Req /></span><input class="mono num" bind:value={attachForm.skewLimitNs} placeholder="skew_limit_ns" aria-label="skew_limit_ns" title={$i18n("linuxHalt.skewLimitHint")} aria-required="true" aria-invalid={inv(attachBad, "skew_limit_ns")} disabled={attachLocked} /></label>
          <label class="fld"><span class="nm" title="thread_id">{$i18n("linuxHalt.f.thread_id")}<Req /></span><select disabled={attachLocked} bind:value={attachForm.threadId} aria-label="thread_id" aria-required="true" aria-invalid={inv(attachBad, "thread_id")}>
            <option value="" disabled>{$i18n("linuxHalt.choose")}</option><option value="stack">stack</option><option value="contextidr">contextidr</option>
          </select></label>
          <label class="fld"><span class="nm" title="kernel_isa">{$i18n("linuxHalt.f.kernel_isa")}<Req /></span><select disabled={attachLocked} bind:value={attachForm.kernelIsa} aria-label="kernel_isa" aria-required="true" aria-invalid={inv(attachBad, "kernel_isa")}>
            <option value="" disabled>{$i18n("linuxHalt.choose")}</option><option value="arm">arm</option><option value="thumb">thumb</option>
          </select></label>
          <label class="fld"><span class="nm" title="max_step_retries">{$i18n("linuxHalt.f.max_step_retries")}<Req /></span><input class="mono num" bind:value={attachForm.maxStepRetries} placeholder="max_step_retries" aria-label="max_step_retries" aria-required="true" aria-invalid={inv(attachBad, "max_step_retries")} disabled={attachLocked} /></label>
          <label class="fld"><span class="nm" title="bp_unmapped">{$i18n("linuxHalt.f.bp_unmapped")}<Req /></span><select disabled={attachLocked} bind:value={attachForm.bpUnmapped} aria-label="bp_unmapped" aria-required="true" aria-invalid={inv(attachBad, "bp_unmapped")}>
            <option value="" disabled>{$i18n("linuxHalt.choose")}</option><option value="error">error</option><option value="hw">hw</option>
          </select></label>
          <label class="fld"><span class="nm" title="max_halt_s">{$i18n("linuxHalt.f.max_halt_s")}<Req /></span><input class="mono num" bind:value={attachForm.maxHaltS} placeholder="max_halt_s" aria-label="max_halt_s" title={$i18n("linuxHalt.maxHaltHint")} aria-required="true" aria-invalid={inv(attachBad, "max_halt_s")} disabled={attachLocked} /></label>
          <label class="fld"><span class="nm" title="fetch_from_target">{$i18n("linuxHalt.f.fetch_from_target")}</span><input class="mono" bind:value={attachForm.fetchFromTarget} placeholder="fetch_from_target" aria-label="fetch_from_target" title={$i18n("linuxHalt.fetchHint")} disabled={attachLocked} /></label>
          <label class="fld"><span class="nm" title="breakpoint_kind">{$i18n("linuxHalt.f.breakpoint_kind")}</span><select disabled={attachLocked} bind:value={attachForm.breakpointKind} aria-label="breakpoint_kind" title={$i18n("linuxHalt.bpKindHint")}>
            <option value="">breakpoint_kind</option><option value="sw">sw</option><option value="hw">hw</option>
          </select></label>
          <label class="fld"><span class="nm" title="step_max_instructions">{$i18n("linuxHalt.f.step_max_instructions")}</span><input class="mono num" bind:value={attachForm.stepMaxInstructions} placeholder="step_max_instructions" aria-label="step_max_instructions" title={$i18n("linuxHalt.stepLimitHint")} disabled={attachLocked} /></label>
          <label class="fld"><span class="nm" title="step_timeout_s">{$i18n("linuxHalt.f.step_timeout_s")}</span><input class="mono num" bind:value={attachForm.stepTimeoutS} placeholder="step_timeout_s" aria-label="step_timeout_s" title={$i18n("linuxHalt.stepLimitHint")} disabled={attachLocked} /></label>
          </details>
          <button class="primary small" disabled={attachLocked}>{$i18n("linuxHalt.attach")}</button>
          <button type="button" class="small" onclick={async () => { const r = await call("linux.halt.debug.close"); if (r !== undefined) { detached = r; attached = null; } await refresh(); }}>{$i18n("linuxHalt.detach")}</button>
        </form>
        {#if attached}
          <div class="mono small attsum">{$i18n("linuxHalt.attachSummary")}: pid {fmt(attached.pid)} {fmt(attached.comm)} · {attached.running ? $i18n("linuxHalt.running") : $i18n("linuxHalt.halted")}</div>
          <table class="mono small"><tbody>
            {#each ["pid", "comm", "running", "breakpoint_kind", "step_max_instructions", "step_timeout_s", "sources_fetched", "fetched"] as k (k)}
              <tr><td>{k}</td><td>{fmt((attached as Record<string, unknown>)[k])}</td></tr>
            {/each}
          </tbody></table>
          {#if attached.attach_stop}<div class="small">attach_stop</div>{@render report(attached.attach_stop)}{/if}
        {/if}
        {#if detached}<pre class="mono small">{JSON.stringify(detached, null, 1)}</pre>{/if}
      </details>
      {#if stop}
        <section><strong>stop</strong> <span class="muted small">{stopFrom}</span>{@render report(stop)}</section>
      {/if}
      {#if processes}
        <section>
          <strong>{$i18n("linuxHalt.processes")}</strong>
          <span class="bar procbar">
            <input class="mono" bind:value={procFilter} placeholder="comm" aria-label={$i18n("linuxHalt.procFilter")} title={$i18n("linuxHalt.procFilter")} />
            <label class="small"><input type="checkbox" bind:checked={hideNoMm} /> {$i18n("linuxHalt.hideNoMm")}</label>
            <span class="muted small">{$i18n("linuxHalt.procShown", { n: shownProcesses.length, total: processes.processes.length })}</span>
          </span>
          <div class="muted small">{$i18n("linuxHalt.procRowHint")}</div>
          <div class="proclist">
          <table class="mono small">
            <thead><tr><th>tgid</th><th>comm</th><th>has_mm</th></tr></thead>
            <tbody>{#each shownProcesses as p, i (i)}<tr class="pick" role="button" tabindex="0" onclick={() => pickPid(p.tgid)} onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickPid(p.tgid); } }}><td>{p.tgid}</td><td>{p.comm}</td><td>{fmt(p.has_mm)}</td></tr>{/each}</tbody>
          </table>
          </div>
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
      <details>
        <summary>gdb</summary>
        <form class="bar" onsubmit={(e) => { e.preventDefault(); void serve(); }}>
          <input class="mono num" bind:value={gdbForm.pid} placeholder="pid" aria-label="pid" aria-required="true" /><Req />
          <input class="mono num" bind:value={gdbForm.port} placeholder="port" aria-label="port" aria-required="true" /><Req />
          <input class="mono num" bind:value={gdbForm.skewLimitNs} placeholder="skew_limit_ns" aria-label="skew_limit_ns" title={$i18n("linuxHalt.skewLimitHint")} aria-required="true" /><Req />
          <select bind:value={gdbForm.threadId} aria-label="thread_id" aria-required="true">
            <option value="" disabled>thread_id</option><option value="stack">stack</option><option value="contextidr">contextidr</option>
          </select><Req />
          <select bind:value={gdbForm.kernelIsa} aria-label="kernel_isa" aria-required="true">
            <option value="" disabled>kernel_isa</option><option value="arm">arm</option><option value="thumb">thumb</option>
          </select><Req />
          <input class="mono num" bind:value={gdbForm.maxStepRetries} placeholder="max_step_retries" aria-label="max_step_retries" aria-required="true" /><Req />
          <select bind:value={gdbForm.bpUnmapped} aria-label="bp_unmapped" aria-required="true">
            <option value="" disabled>bp_unmapped</option><option value="error">error</option><option value="hw">hw</option>
          </select><Req />
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
  .head { display: flex; gap: 6px; align-items: center; }
  .chip { font-size: 12px; padding: 1px 8px; border-radius: 10px; background: var(--select); }
  .chip.on { background: color-mix(in srgb, var(--warn) 25%, transparent); }
  .cores { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .more > summary, .adv > summary { cursor: pointer; font-size: 12px; opacity: 0.85; }
  .adv { margin-top: 4px; }
  /* 欄の名前を常に見せる（値が入ると placeholder は消える。何の値かが分からなくなる） */
  .fld { display: inline-flex; flex-direction: column; gap: 1px; }
  .fld .nm { font-size: 10px; opacity: 0.7; }
  /* 欄の名前（placeholder）が切れて読めなかった: 中身（空なら名前）の幅に合わせる */
  .short { width: auto; field-sizing: content; min-width: 7em; }
  .num { width: auto; field-sizing: content; min-width: 6em; }
  .small { font-size: 12px; }
  /* 長い値（パス）の欄: 余白を使って広げる。全文は title に */
  .fld.wide { flex: 1 1 22em; min-width: 12em; }
  .fld.wide input { width: 100%; box-sizing: border-box; }
  .bar input.grow { flex: 1 1 24em; min-width: 12em; }
  input[aria-invalid="true"], select[aria-invalid="true"] { border: 1px solid var(--bad); outline: 1px solid var(--bad); }
  .attsum { margin-top: 2px; }
  .proclist { max-height: 14em; overflow: auto; }
  tr.pick { cursor: pointer; }
  tr.pick:hover, tr.pick:focus { background: var(--select); }
  table td, table th { padding: 0 8px 0 0; vertical-align: top; text-align: left; }
  .wrap { white-space: pre-wrap; word-break: break-word; }
  pre { margin: 2px 0; white-space: pre-wrap; }
  .bad { color: var(--bad); }
  .pad { padding: 4px 8px; }
</style>
