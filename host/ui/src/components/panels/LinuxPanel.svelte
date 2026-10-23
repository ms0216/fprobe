<script lang="ts">
  // Linux（R26）: SSH でつないだ Linux のターゲットの能力の事実・エージェント・プローブ・時刻合わせ・出来事、
  // 道 B（物理メモリのリングを JTAG で読む）・止めずに読む口（memap）・gdb・ftrace・journal。
  // 設定はすべて欄に出し、値は API の事実のまま並べる（隠さない。仕様 §2）
  import { errorText } from "../../lib/api";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { agentStartParams, connectParams, coreFetchParams, coreLimitParams, corePatternParams, coreStatusParams,
    perfRecordParams, perfStatParams, type CoreForm, type PerfForm, ftraceParams, gdbBatchParams, gdbServeParams, LinuxEvents, logParams, memapParams,
    probeParams, ringOpenParams, ringReadParams, ringWatchParams, type ConnectForm, type GdbForm, type LinuxEvent, type LogForm,
    type MemapForm, type ProbeForm, type RingForm } from "../../lib/linux";
  import { load, save } from "../../lib/prefs";

  const POLL_MS = 500;
  const { app, conn } = shared();

  let form = $state<ConnectForm>(restore("linux.connect", { target: "linux", host: "", user: "", port: "22", key: "", sudo: false, sudoPasswordEnv: "" }));
  let probe = $state<ProbeForm>(restore("linux.probe", { name: "", binary: "", method: "ptrace", symbol: "", address: "", isa: "",
    fetchargs: "", regs: "r0", ret: false, pid: "" }));
  // 道 B などの欄は空で始める（既定の値を置かない。前に書いた値だけを戻す）
  let watchdog = $state(load("linux.watchdog", ""));
  let ring = $state<RingForm>(restore("linux.ring", { phys: "", size: "", cached: false, linger: false, address: "", access: "",
    maxRecords: "", interval: "" }));
  let memap = $state<MemapForm>(restore("linux.memap", { xsdb: "", cableSerial: "", chainIdcode: "", timeout: "" }));
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
  let syncCount = $state("8");
  let interval = $state("1.0");
  let tracing = $state(false);
  let records = $state<LinuxEvent[]>([]);
  let dropped = $state(0);
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

  async function refresh() {
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
    } catch (e) {
      error = errorText(e);
    }
  }

  $effect(() => {
    if (!target) return;
    const timer = setInterval(() => void pull(), POLL_MS);
    return () => clearInterval(timer);
  });
  // だれが（AI も）操作しても読み直す
  $effect(() => conn.on("activity", (e: { method: string }) => {
    if (e.method.startsWith("linux.")) void refresh();
  }));

  const fmt = (v: unknown) => (v === null || v === undefined ? $i18n("linux.unknown") : typeof v === "object" ? JSON.stringify(v) : String(v));
</script>

<div class="pane linux">
  <form class="pane-bar" onsubmit={(e) => { e.preventDefault(); void connect(); }}>
    <input class="mono short" bind:value={form.target} placeholder={$i18n("linux.target")} aria-label={$i18n("linux.target")} />
    <input class="mono" bind:value={form.host} placeholder={$i18n("linux.host")} aria-label={$i18n("linux.host")} />
    <input class="mono short" bind:value={form.user} placeholder={$i18n("linux.user")} aria-label={$i18n("linux.user")} />
    <input class="mono num" bind:value={form.port} placeholder="22" aria-label={$i18n("linux.port")} />
    <input class="mono" bind:value={form.key} placeholder={$i18n("linux.key")} aria-label={$i18n("linux.key")} />
    <label><input type="checkbox" bind:checked={form.sudo} /> sudo</label>
    <input class="mono short" bind:value={form.sudoPasswordEnv} placeholder={$i18n("linux.sudoEnv")} aria-label={$i18n("linux.sudoEnv")}
      title={$i18n("linux.sudoEnvHint")} />
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
          <select bind:value={probe.method} aria-label={$i18n("linux.method")}>
            <option value="ptrace">ptrace</option><option value="uprobe">uprobe</option>
          </select>
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
        <input class="mono num" bind:value={syncCount} aria-label={$i18n("linux.syncCount")} title={$i18n("linux.syncCount")} />
        <button class="small" onclick={() => void call("linux.sync", { target, count: Number(syncCount) })}>{$i18n("linux.syncNow")}</button>
        <input class="mono num" bind:value={interval} aria-label={$i18n("linux.interval")} title={$i18n("linux.interval")} />
        <button class="primary small" disabled={tracing} onclick={async () => {
          if ((await call("linux.trace.start", { target, sync_interval_s: Number(interval) })) !== undefined) tracing = true; }}>{$i18n("linux.traceStart")}</button>
        <button class="small" disabled={!tracing} onclick={async () => { await call("linux.trace.stop", { target }); tracing = false; }}>{$i18n("linux.traceStop")}</button>
      </section>
      <details>
        <summary>{$i18n("linux.ring")}</summary>
        <div class="bar">
          <input class="mono short" bind:value={ring.phys} placeholder="phys" aria-label="phys" title={$i18n("linux.ringPhys")} />
          <input class="mono short" bind:value={ring.size} placeholder="size" aria-label="size" />
          <label><input type="checkbox" bind:checked={ring.cached} /> cached</label>
          <label title={$i18n("linux.lingerHint")}><input type="checkbox" bind:checked={ring.linger} /> linger</label>
          <button class="small" onclick={() => void run("linux.ring.open", ringOpenParams(target!, ring), ["linux.ring", ring])}>{$i18n("linux.ringOpen")}</button>
        </div>
        <div class="bar">
          <select bind:value={ring.access} aria-label="access"><option value="">access</option><option value="halt">halt</option><option value="memap">memap</option></select>
          <input class="mono short" bind:value={ring.address} placeholder="address" aria-label="address" title={$i18n("linux.ringAddress")} />
          <input class="mono num" bind:value={ring.maxRecords} placeholder="max" aria-label="max_records" title="max_records" />
          <button class="small" onclick={async () => { ringResult = null; ringResult = (await run("linux.ring.read", ringReadParams(target!, ring), ["linux.ring", ring])) ?? null; }}>{$i18n("linux.ringRead")}</button>
          <input class="mono num" bind:value={ring.interval} placeholder="s" aria-label="interval_s" title="interval_s" />
          <button class="small" onclick={() => void run("linux.ring.watch", ringWatchParams(target!, ring), ["linux.ring", ring])}>{$i18n("linux.ringWatch")}</button>
          <button class="small" onclick={async () => { ringResult = null; ringResult = (await run("linux.ring.unwatch", { params: { target: target! } })) ?? null; }}>{$i18n("linux.ringUnwatch")}</button>
        </div>
        {#if ringResult}<pre class="mono small">{JSON.stringify({ ...ringResult, records: ringResult.records?.length }, null, 1)}</pre>{/if}
        <div class="bar">
          <strong>memap</strong>
          <input class="mono" bind:value={memap.xsdb} placeholder="xsdb" aria-label="xsdb" title={$i18n("linux.xsdb")} />
          <input class="mono short" bind:value={memap.cableSerial} placeholder="cable_serial" aria-label="cable_serial" />
          <input class="mono short" bind:value={memap.chainIdcode} placeholder="chain_idcode" aria-label="chain_idcode" />
          <input class="mono num" bind:value={memap.timeout} placeholder="s" aria-label="timeout_s" title="timeout_s" />
          <button class="small" onclick={async () => { memapInfo = null; memapInfo = (await run("linux.memap.connect", memapParams(target!, memap), ["linux.memap", memap])) ?? null; }}>{$i18n("linux.connect")}</button>
          <button class="small" onclick={async () => { if ((await run("linux.memap.disconnect", { params: { target: target! } })) !== undefined) memapInfo = null; }}>{$i18n("linux.disconnect")}</button>
        </div>
        {#if memapInfo}<div class="mono small">{JSON.stringify(memapInfo)}</div>{/if}
      </details>
      <details>
        <summary>gdb</summary>
        <textarea class="mono small" rows="3" bind:value={gdb.commands} placeholder={$i18n("linux.gdbCommands")} aria-label={$i18n("linux.gdbCommands")}></textarea>
        <div class="bar">
          <input class="mono short" bind:value={gdb.pid} placeholder="pid" aria-label="pid" />
          <input class="mono short" bind:value={gdb.core} placeholder="core" aria-label="core" />
          <input class="mono short" bind:value={gdb.binary} placeholder="binary" aria-label="binary" />
          <input class="mono num" bind:value={gdb.timeout} placeholder="s" aria-label="timeout_s" title="timeout_s" />
          <button class="small" onclick={async () => { gdbResult = null; gdbResult = (await run("linux.gdb.batch", gdbBatchParams(target!, gdb), ["linux.gdb", gdb])) ?? null; }}>{$i18n("linux.gdbRun")}</button>
          <input class="mono num" bind:value={gdb.port} placeholder="port" aria-label="port" />
          <button class="small" onclick={async () => { serveInfo = null; serveInfo = (await run("linux.gdb.serve", gdbServeParams(target!, gdb), ["linux.gdb", gdb])) ?? null; }}>{$i18n("linux.gdbServe")}</button>
          <button class="small" onclick={async () => { if ((await run("linux.gdb.stop", { params: { target: target! } })) !== undefined) serveInfo = null; }}>{$i18n("linux.gdbStop")}</button>
        </div>
        {#if serveInfo}<div class="mono small">{serveInfo.connect}</div>{/if}
        {#if gdbStatus}<div class="mono small">{JSON.stringify(gdbStatus)}</div>{/if}
        {#if gdbResult}
          <div class="mono small">exit_code={fmt(gdbResult.exit_code)} timed_out={fmt(gdbResult.timed_out)}</div>
          <pre class="mono small">{gdbResult.stdout}</pre>
          {#if gdbResult.stderr}<pre class="mono small bad">{gdbResult.stderr}</pre>{/if}
        {/if}
      </details>
      <details>
        <summary>ftrace / journal</summary>
        <div class="bar">
          <input class="mono" bind:value={ftrace} placeholder="sched/sched_switch" aria-label="events" />
          <button class="small" onclick={async () => { ftraceResult = null; ftraceResult = (await run("linux.ftrace.enable", ftraceParams(target!, ftrace), ["linux.ftrace", ftrace])) ?? null; }}>{$i18n("linux.ftraceOn")}</button>
          <button class="small" onclick={async () => { ftraceResult = null; ftraceResult = (await run("linux.ftrace.disable", ftraceParams(target!, ftrace))) ?? null; }}>{$i18n("linux.ftraceOff")}</button>
        </div>
        {#if ftraceResult}<div class="mono small">{JSON.stringify(ftraceResult)}</div>{/if}
        <div class="bar">
          <input class="mono num" bind:value={log.lines} placeholder="lines" aria-label="lines" />
          <input class="mono short" bind:value={log.unit} placeholder="unit" aria-label="unit" oninput={() => { log.cursor = ""; }} />
          <input class="mono short" bind:value={log.cursor} placeholder="since_cursor" aria-label="since_cursor" />
          <button class="small" onclick={async () => { logResult = null; logResult = (await run("linux.log.read", logParams(target!, log), ["linux.log", { ...log, cursor: "" }])) ?? null; }}>{$i18n("linux.logRead")}</button>
          {#if logResult?.next_cursor}<button class="small" onclick={() => { log.cursor = logResult!.next_cursor; }}>{$i18n("linux.logNext")}</button>{/if}
        </div>
        {#if logResult}
          <div class="mono small">{logResult.clock} exit_code={fmt(logResult.exit_code)} bad_lines={fmt(logResult.bad_lines)}</div>
          <div class="rows mono small">
            {#each logResult.entries as e, i (i)}<div>{e.__MONOTONIC_TIMESTAMP} {e._SYSTEMD_UNIT ?? e.SYSLOG_IDENTIFIER ?? ""} {e.MESSAGE}</div>{/each}
          </div>
          {#if logResult.stderr}<pre class="mono small bad">{logResult.stderr}</pre>{/if}
        {/if}
      </details>
      <details>
        <summary>core dump</summary>
        <div class="bar">
          <input class="mono short" bind:value={core.pid} placeholder="pid" aria-label="pid" />
          <button class="small" onclick={async () => { coreResult = null; coreResult = (await run("linux.core.status", coreStatusParams(target!, core), ["linux.core", core])) ?? null; }}>{$i18n("linux.coreStatus")}</button>
          <input class="mono short" bind:value={core.soft} placeholder="soft" aria-label="soft" title={$i18n("linux.coreLimitHint")} />
          <input class="mono short" bind:value={core.hard} placeholder="hard" aria-label="hard" title={$i18n("linux.coreLimitHint")} />
          <button class="small" onclick={async () => { coreResult = null; coreResult = (await run("linux.core.limit_set", coreLimitParams(target!, core), ["linux.core", core])) ?? null; }}>{$i18n("linux.coreLimit")}</button>
        </div>
        <div class="bar">
          <input class="mono" bind:value={core.pattern} placeholder="core_pattern" aria-label="core_pattern" title={$i18n("linux.corePatternHint")} />
          <button class="small" onclick={async () => { coreResult = null; coreResult = (await run("linux.core.pattern_set", corePatternParams(target!, core), ["linux.core", core])) ?? null; }}>{$i18n("linux.corePattern")}</button>
        </div>
        <div class="bar">
          <input class="mono" bind:value={core.path} placeholder="path" aria-label="path" />
          <input class="mono" bind:value={core.localPath} placeholder="local_path" aria-label="local_path" title={$i18n("linux.coreLocalHint")} />
          <button class="small" onclick={async () => { coreResult = null; coreResult = (await run("linux.core.fetch", coreFetchParams(target!, core), ["linux.core", core])) ?? null; }}>{$i18n("linux.coreFetch")}</button>
        </div>
        {#if coreResult}<pre class="mono small">{JSON.stringify(coreResult, null, 1)}</pre>{/if}
      </details>
      <details>
        <summary>perf</summary>
        <div class="bar">
          <input class="mono short" bind:value={perf.pid} placeholder="pid" aria-label="pid" title={$i18n("linux.perfPidHint")} />
          <input class="mono num" bind:value={perf.duration} placeholder="s" aria-label="duration_s" title="duration_s" />
          <input class="mono" bind:value={perf.events} placeholder="task-clock cycles" aria-label="events" />
          <button class="small" onclick={async () => { perfResult = null; perfResult = (await run("linux.perf.stat", perfStatParams(target!, perf), ["linux.perf", perf])) ?? null; }}>{$i18n("linux.perfStat")}</button>
        </div>
        <div class="bar">
          <input class="mono num" bind:value={perf.frequency} placeholder="Hz" aria-label="frequency_hz" title="frequency_hz" />
          <input class="mono num" bind:value={perf.maxSamples} placeholder="max" aria-label="max_samples" title={$i18n("linux.perfMaxHint")} />
          <button class="small" onclick={async () => { perfResult = null; perfResult = (await run("linux.perf.record", perfRecordParams(target!, perf), ["linux.perf", perf])) ?? null; }}>{$i18n("linux.perfRecord")}</button>
        </div>
        {#if perfResult?.rows}
          <table class="mono small"><tbody>
            {#each perfResult.rows as row, i (i)}<tr><td>{row.event}</td><td>{fmt(row.value ?? row.counted)}</td><td>{row.unit}</td><td>{fmt(row.run_pct)} %</td></tr>{/each}
          </tbody></table>
        {:else if perfResult}
          <pre class="mono small">{JSON.stringify({ ...perfResult, stderr: undefined }, null, 1)}</pre>
        {/if}
        {#if perfResult?.stderr}<pre class="mono small muted">{perfResult.stderr}</pre>{/if}
      </details>
      {#if dropped}<div class="muted pad">{$i18n("linux.dropped", { n: dropped })}</div>{/if}
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
  .short { width: 7em; }
  .num { width: 4em; }
  .small { font-size: 12px; }
  table td { padding: 0 8px 0 0; vertical-align: top; }
  pre { margin: 2px 0; white-space: pre-wrap; }
  textarea { width: 100%; box-sizing: border-box; }
  .rows { overflow: auto; }
  .row { display: grid; grid-template-columns: 130px 60px 100px 60px 1fr auto; gap: 8px; }
  .bad { color: var(--bad); }
  .pad { padding: 4px 8px; }
</style>
