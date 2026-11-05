<script lang="ts">
  import { untrack } from "svelte";
  import Req from "./Req.svelte";
  import { interval as everyMs } from "../common/interval";
  // Linux（R26）: SSH でつないだ Linux のターゲットの能力の事実・エージェント・プローブ・時刻合わせ・出来事、
  // 道 B（物理メモリのリングを JTAG で読む）・止めずに読む口（memap）・gdb・ftrace・journal。
  // 設定はすべて欄に出し、値は API の事実のまま並べる（隠さない。仕様 §2）
  import { errorText } from "../../lib/api";
  import { suggestedOf } from "../../lib/apispec";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { agentStartParams, connectParams, LinuxEvents, probeParams, syncParams, traceStartParams, type ConnectForm,
    type CoreForm, type GdbForm, type LinuxEvent, type LogForm, type MemapForm, type PerfForm, type ProbeForm,
    type RingForm } from "../../lib/linux";
  import { LINUX_KEEP, reconcileTarget } from "../../lib/linux";
  import type { MemapJtagForm } from "../../lib/linuxhalt";
  import { load, save } from "../../lib/prefs";
  import LinuxCoreDump from "./linux/LinuxCoreDump.svelte";
  import LinuxGdb from "./linux/LinuxGdb.svelte";
  import LinuxLog from "./linux/LinuxLog.svelte";
  import LinuxPerf from "./linux/LinuxPerf.svelte";
  import LinuxRing from "./linux/LinuxRing.svelte";

  const POLL_MS = 500;
  const { app, conn } = shared();

  // 接続・プローブの欄も空から（名前・ポート・方式・レジスタを画面で決めない。前に書いた値だけを戻す）
  let form = $state<ConnectForm>(restore("linux.connect", { target: "", host: "", user: "", port: String(suggestedOf("linux.connect", "port") ?? ""), key: "", sudo: false, sudoPasswordEnv: "" }));
  let probe = $state<ProbeForm>(restore("linux.probe", { name: "", binary: "", method: "", symbol: "", address: "", isa: "",
    fetchargs: "", regs: "", ret: false, pid: "" }));
  // 道 B などの欄は空で始める（既定の値を置かない。前に書いた値だけを戻す）
  let watchdog = $state(load("linux.watchdog", ""));
  let ring = $state<RingForm>(restore("linux.ring", { phys: "", size: "", cached: false, linger: false, address: "", access: "",
    maxRecords: "", interval: "" }));
  let memap = $state<MemapForm>(restore("linux.memap", { xsdb: "", cableSerial: "", chainIdcode: "", timeout: "" }));
  let memapJtag = $state<MemapJtagForm>(restore("linux.memapJtag", { chainIdcode: "", ap: "", frequencyHz: "" }));
  let gdb = $state<GdbForm>(restore("linux.gdb", { commands: "", timeout: "", pid: "", core: "", binary: "", port: "" }));
  let ftrace = $state(load("linux.ftrace", ""));
  let log = $state<LogForm>(restore("linux.log", { lines: "", unit: "", cursor: "" }));
  let ringResult = $state<Record<string, any> | null>(null);
  let memapInfo = $state<Record<string, any> | null>(null);
  let gdbResult = $state<Record<string, any> | null>(null);
  let gdbStatus = $state<Record<string, any> | null>(null);
  let ftraceResult = $state<Record<string, any> | null>(null);
  let logResult = $state<Record<string, any> | null>(null);
  let serveInfo = $state<Record<string, any> | null>(null);
  let core = $state<CoreForm>(restore("linux.core", { pid: "", pattern: "", soft: "", hard: "", path: "", localPath: "" }));
  let perf = $state<PerfForm>(restore("linux.perf", { events: "", duration: "", frequency: "", maxSamples: "", pid: "" }));
  let coreResult = $state<Record<string, any> | null>(null);
  let perfResult = $state<Record<string, any> | null>(null);

  // 前のターゲット・前の接続の結果を残さない（デーモンは切断で memap・gdb・ftrace を閉じる）
  function clearResults() {
    ringResult = memapInfo = gdbResult = gdbStatus = ftraceResult = logResult = serveInfo = coreResult = perfResult = null;
    log.cursor = "";                                       // journal の位置はそのターゲット・その接続のもの
  }
  let target = $state<string | null>(null);              // つないでいるターゲットの名前
  let caps = $state<Record<string, any> | null>(null);
  let agent = $state<Record<string, any> | null>(null);
  let probes = $state<Record<string, any>[]>([]);
  let lastProbe = $state<Record<string, any> | null>(null);
  let syncCount = $state("");                    // 時刻合わせの往復の回数・間隔は使う人が入れる（画面の既定を持たない）
  let interval = $state("");
  let tracing = $state(false);
  let records = $state<LinuxEvent[]>([]);
  let dropped = $state(0);
  let discarded = $state(0);                       // 画面に持つ上限を超えて捨てた記録
  let error = $state<string | null>(null);
  const events = new LinuxEvents(conn);

  function restore<T>(key: string, fallback: T): T {
    try {
      const v = JSON.parse(load(key, ""));
      if (v && typeof v === "object") return { ...fallback, ...v };
    } catch { /* 既定 */ }
    return fallback;
  }

  async function call(method: string, params: Record<string, unknown>): Promise<any> {
    if (!(await allowed(app))) return undefined;
    error = null;
    return app.act(method as never, params as never);
  }

  /** デーモンが持つ「いまつながっているターゲット」に、映すターゲットを合わせる（AI がつないだものも、切ったものも映す） */
  async function syncTargets() {
    try {
      const r = (await conn.call("linux.targets", {}, { quiet: true })) as { targets: { target: string; capabilities: Record<string, any> }[] };
      const next = reconcileTarget(target, r.targets);
      if (next.changed) {
        target = next.target;
        caps = r.targets.find((t) => t.target === target)?.capabilities ?? null;
        agent = null;
        probes = [];
        clearResults();
        events.reset();
        records = [];
      } else if (target) {
        caps = r.targets.find((t) => t.target === target)?.capabilities ?? caps;
      }
    } catch (e) {
      error = errorText(e);
    }
  }

  async function refresh() {
    await syncTargets();
    if (!target) return;
    try {
      agent = (await conn.call("linux.agent.status" as never, { target } as never, { quiet: true })) as any;
      probes = ((await conn.call("linux.probe.list" as never, { target } as never, { quiet: true })) as any).probes;
      gdbStatus = (await conn.call("linux.gdb.status" as never, { target } as never, { quiet: true })) as any;
    } catch (e) {
      error = errorText(e);
    }
  }

  async function connect() {
    const built = connectParams(form);
    if ("error" in built) { error = $i18n("linux.badField", { field: built.error }); return; }
    save("linux.connect", JSON.stringify(form));
    const r = await call("linux.connect", built.params);
    if (r === undefined) return;
    caps = r;
    target = form.target.trim();
    clearResults();
    events.reset();
    records = [];
    await refresh();
  }

  async function addProbe() {
    if (!target) return;
    const built = probeParams(target, probe);
    if ("error" in built) { error = $i18n("linux.badField", { field: built.error }); return; }
    save("linux.probe", JSON.stringify(probe));
    const r = await call("linux.probe.add", built.params);
    if (r !== undefined) lastProbe = r;
    await refresh();
  }

  // 欄から組んだ引数で呼ぶ（欄の誤りはその欄の名前で知らせる）。remember があれば欄の値を覚える
  async function run(method: string, built: { params: Record<string, unknown> } | { error: string },
                     remember?: [string, unknown]): Promise<any> {
    if ("error" in built) { error = $i18n("linux.badField", { field: built.error }); return undefined; }
    if (remember) save(remember[0], typeof remember[1] === "string" ? remember[1] : JSON.stringify(remember[1]));
    const r = await call(method, built.params);
    await refresh();
    return r;
  }

  async function pull() {
    if (!target) return;
    try {
      await events.more(target);
      records = events.records;
      dropped = events.dropped;
      discarded = events.discarded;
    } catch (e) {
      error = errorText(e);
    }
  }

  $effect(() => {
    if (!target) return;
    return everyMs(() => pull(), POLL_MS);
  });
  // 開いたとき・つながったとき（ページの再読み込みの後を含む）に、デーモンの接続の状態を読む
  // $app 全体ではなく「開いているか」だけを見る（$app は読み直しの記録のたびに更新され、読み直し → 記録 → $app の更新 →
  // 読み直し… の循環になった。2026-11-05 実機: attach したままページを読み直すと、毎秒数十回の linux.halt.debug.status）
  const linkOpen = $derived($app.link === "open");
  $effect(() => { if (linkOpen) untrack(() => void refresh()); });
  // だれが（AI も）操作しても読み直す
  $effect(() => conn.on("activity", (e: { method: string }) => {
    if (e.method.startsWith("linux.") && !e.method.startsWith("linux.halt.")) void refresh();     // linux.halt.* は SSH の状態ではない（停止デバッグの読みの記録で SSH の読み直しを呼ばない）
  }));

  const fmt = (v: unknown) => (v === null || v === undefined ? $i18n("linux.unknown") : typeof v === "object" ? JSON.stringify(v) : String(v));
</script>

<div class="pane linux">
  <form class="pane-bar" onsubmit={(e) => { e.preventDefault(); void connect(); }}>
    <label class="fld"><span class="nm">{$i18n("linux.target")}</span><input class="mono short" bind:value={form.target} placeholder={$i18n("linux.target")} aria-label={$i18n("linux.target")}
           aria-required="true" /></label><Req />
    <label class="fld"><span class="nm">{$i18n("linux.host")}</span><input class="mono" bind:value={form.host} placeholder={$i18n("linux.host")} aria-label={$i18n("linux.host")}
           aria-required="true" /></label><Req />
    <label class="fld"><span class="nm">{$i18n("linux.user")}</span><input class="mono short" bind:value={form.user} placeholder={$i18n("linux.user")} aria-label={$i18n("linux.user")}
           aria-required="true" /></label><Req />
    <label class="fld"><span class="nm">{$i18n("linux.port")}</span><input class="mono num" bind:value={form.port} placeholder={$i18n("linux.port")} aria-label={$i18n("linux.port")}
           aria-required="true" /></label><Req />
    <label class="fld"><span class="nm">{$i18n("linux.key")}</span><input class="mono" bind:value={form.key} placeholder={$i18n("linux.key")} aria-label={$i18n("linux.key")} /></label>
    <label><input type="checkbox" bind:checked={form.sudo} /> sudo</label>
    <label class="fld"><span class="nm">{$i18n("linux.sudoEnv")}</span><input class="mono short" bind:value={form.sudoPasswordEnv} placeholder={$i18n("linux.sudoEnv")} aria-label={$i18n("linux.sudoEnv")}
      title={$i18n("linux.sudoEnvHint")} /></label>
    <button class="primary small">{$i18n("linux.connect")}</button>
    {#if target}<button type="button" class="small" onclick={async () => { if ((await call("linux.disconnect", { target })) !== undefined) { target = null; caps = null; clearResults(); } }}>
      {$i18n("linux.disconnect")}</button>{/if}
  </form>
  {#if error}<div class="bad pad">{error}</div>{/if}
  {#if !target}
    <div class="muted pad">{$i18n("linux.none")}</div>
  {:else}
    <div class="body">
      <details open>
        <summary>{$i18n("linux.caps")}</summary>
        <table class="mono small"><tbody>
          {#each Object.entries(caps ?? {}) as [k, v] (k)}
            {#if k === "config" || k === "tools"}
              {#each Object.entries(v ?? {}) as [k2, v2] (k2)}<tr><td>{k2}</td><td>{fmt(v2)}</td></tr>{/each}
            {:else}
              <tr><td>{k}</td><td>{fmt(v)}</td></tr>
            {/if}
          {/each}
        </tbody></table>
      </details>
      <section>
        <div class="bar">
          <strong>{$i18n("linux.agent")}</strong>
          <button class="small" onclick={async () => { await call("linux.agent.deploy", { target }); await refresh(); }}>{$i18n("linux.deploy")}</button>
          <input class="mono num" bind:value={watchdog} placeholder="watchdog_s" aria-label="watchdog_s" title={$i18n("linux.watchdogHint")} />
          <button class="small" onclick={() => void run("linux.agent.start", agentStartParams(target!, watchdog), ["linux.watchdog", watchdog])}>{$i18n("linux.start")}</button>
          <button class="small" onclick={async () => { await call("linux.agent.stop", { target }); await refresh(); }}>{$i18n("linux.stop")}</button>
        </div>
        {#if agent}
          <table class="mono small"><tbody>
            {#each Object.entries(agent).filter(([k]) => k !== "stderr_tail") as [k, v] (k)}
              <tr><td>{k}</td><td class:bad={k === "sync_error" && v}>{fmt(v)}</td></tr>
            {/each}
          </tbody></table>
          {#if agent.stderr_tail}<pre class="mono small">{agent.stderr_tail}</pre>{/if}
        {/if}
      </section>
      <section>
        <form class="bar" onsubmit={(e) => { e.preventDefault(); void addProbe(); }}>
          <strong>{$i18n("linux.probe")}</strong>
          <input class="mono short" bind:value={probe.name} placeholder={$i18n("linux.name")} aria-label={$i18n("linux.name")} />
          <input class="mono" bind:value={probe.binary} placeholder={$i18n("linux.binary")} aria-label={$i18n("linux.binary")} />
          <select bind:value={probe.method} aria-label={$i18n("linux.method")} aria-required="true">
            {#if !probe.method}<option value="" disabled></option>{/if}
            <option value="ptrace">ptrace</option><option value="uprobe">uprobe</option>
          </select><Req />
          <input class="mono short" bind:value={probe.symbol} placeholder={$i18n("linux.symbol")} aria-label={$i18n("linux.symbol")} />
          <input class="mono short" bind:value={probe.address} placeholder={$i18n("linux.address")} aria-label={$i18n("linux.address")} />
          <select bind:value={probe.isa} aria-label="isa"><option value="">isa</option><option value="arm">arm</option><option value="thumb">thumb</option></select>
          {#if probe.method === "ptrace"}
            <input class="mono short" bind:value={probe.pid} placeholder="pid" aria-label="pid" />
            <input class="mono" bind:value={probe.regs} placeholder={$i18n("linux.regs")} aria-label={$i18n("linux.regs")} />
          {:else}
            <input class="mono" bind:value={probe.fetchargs} placeholder={$i18n("linux.fetchargs")} aria-label={$i18n("linux.fetchargs")} />
            <label><input type="checkbox" bind:checked={probe.ret} /> {$i18n("linux.ret")}</label>
          {/if}
          <button class="small">{$i18n("linux.add")}</button>
        </form>
        {#if lastProbe}<pre class="mono small">{lastProbe.line ?? JSON.stringify(lastProbe.plan)}</pre>{/if}
        {#each probes as p (p.name)}
          <div class="mono small">{p.name} {p.method} {p.binary} 0x{p.site.offset.toString(16)}
            {p.line ?? `${p.plan?.insn} pid=${p.pid} threads=${p.threads}`}
            <button class="small" onclick={async () => { await call("linux.probe.remove", { target, name: p.name }); await refresh(); }}>{$i18n("linux.remove")}</button></div>
        {/each}
      </section>
      <section class="bar">
        <strong>{$i18n("linux.sync")}</strong>
        <label class="muted">{$i18n("linux.syncCount")}<Req />
          <input class="mono num" bind:value={syncCount} inputmode="numeric" aria-required="true" /></label>
        <button class="small" onclick={() => void run("linux.sync", syncParams(target!, syncCount))}>{$i18n("linux.syncNow")}</button>
        <label class="muted">{$i18n("linux.interval")}<Req />
          <input class="mono num" bind:value={interval} inputmode="decimal" aria-required="true" /></label>
        <button class="primary small" disabled={tracing} onclick={async () => {
          if ((await run("linux.trace.start", traceStartParams(target!, interval))) !== undefined) tracing = true; }}>{$i18n("linux.traceStart")}</button>
        <button class="small" disabled={!tracing} onclick={async () => { await call("linux.trace.stop", { target }); tracing = false; }}>{$i18n("linux.traceStop")}</button>
      </section>
      <LinuxRing target={target!} bind:ring bind:memap bind:memapJtag bind:ringResult bind:memapInfo {run} />
      <LinuxGdb target={target!} bind:gdb bind:gdbResult {gdbStatus} bind:serveInfo {run} {fmt} />
      <LinuxLog target={target!} bind:ftrace bind:log bind:ftraceResult bind:logResult {run} {fmt} />
      <LinuxCoreDump target={target!} bind:core bind:coreResult {run} />
      <LinuxPerf target={target!} bind:perf bind:perfResult {run} {fmt} />
      {#if dropped}<div class="muted pad">{$i18n("linux.dropped", { n: dropped })}</div>{/if}
      {#if discarded}<div class="muted pad">{$i18n("common.discarded", { n: discarded, limit: LINUX_KEEP })}</div>{/if}
      <div class="rows mono small">
        {#each records as r (r.seq)}
          <div class="row">
            <span>{r.target_ns ?? "-"}</span>
            <span>{r.method}</span>
            <span>{r.probe}</span>
            <span>{r.pid ?? r.tid ?? ""}</span>
            <span>{JSON.stringify(r.args ?? r.regs ?? {})}</span>
            <span class="muted">{r.sync ? `±${r.sync.half_rtt_ns} ns${r.sync.outside ? " outside" : ""}` : $i18n("linux.noSync")}</span>
          </div>
        {:else}
          <div class="muted pad">{$i18n("linux.noEvents")}</div>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .linux { display: flex; flex-direction: column; }
  .body { flex: 1; overflow: auto; padding: 4px 8px; display: flex; flex-direction: column; gap: 6px; }
  .bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  /* 欄の名前を常に見せる（値が入ると placeholder は消える） */
  .fld { display: inline-flex; flex-direction: column; gap: 1px; }
  .fld .nm { font-size: 10px; opacity: 0.7; }
  /* 欄の名前（placeholder）が切れて読めなかった（「Target nam」「sudo passw」）: 中身（空なら名前）の幅に合わせる */
  .short { width: auto; field-sizing: content; min-width: 7em; }
  .num { width: auto; field-sizing: content; min-width: 4em; }
  .small { font-size: 12px; }
  table td { padding: 0 8px 0 0; vertical-align: top; }
  pre { margin: 2px 0; white-space: pre-wrap; }
  .rows { overflow: auto; }
  .row { display: grid; grid-template-columns: 130px 60px 100px 60px 1fr auto; gap: 8px; }
  .bad { color: var(--bad); }
  .pad { padding: 4px 8px; }
</style>
