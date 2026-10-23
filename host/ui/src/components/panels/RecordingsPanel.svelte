<script lang="ts">
  // Recordings（SystemView・Tracealyzer の記録の開始・停止・保存した記録。G07）: 記録の状態（recording.status）と
  // 開始・停止、保存した記録の一覧（recording.list）と開く（recording.open → 時間軸を記録に切り替えて全体を表示）、
  // 書き出し（recording.export: VCD・CSV・JSON・Perfetto。パスはデーモンの PC）、長期の記録の分割（recording.segments）
  import { errorText } from "../../lib/api";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { onRecording } from "../../lib/notify";
  import { load, save } from "../../lib/prefs";
  import { defaultExportPath, exportParams, EXPORT_FORMATS, longTermParams, type ExportFormat } from "../../lib/recordings";
  import { formatDuration } from "../../lib/time";
  import { confirmer } from "../../lib/confirm";
  import { readSnapshots, type OpenSnapshot, type SnapshotRow } from "../../lib/snapshots";
  import TimeLink from "./TimeLink.svelte";

  const POLL_MS = 1000;                                   // 記録中の件数・範囲の読み直し

  const { app, conn, tl, dock } = shared();
  let status = $state<any>(null);
  let list = $state<{ name: string; title: string | null; board: string | null }[]>([]);
  let segments = $state<any>(null);
  let name = $state(load("recordings.name", ""));
  let long = $state({ segmentS: load("recordings.segmentS", ""), maxMb: load("recordings.maxMb", ""),
                      decimateAfterS: load("recordings.decimateAfterS", "") });
  let format = $state<ExportFormat>(load("recordings.format", "perfetto") as ExportFormat);
  let scope = $state<"all" | "view">("all");
  let path = $state("");
  let edited = $state(false);                             // 書き出しのパスを手で変えた
  let exported = $state<string | null>(null);
  let error = $state<string | null>(null);
  let chosen = $state<string | null>(null);
  // 保存した故障の状態（snapshot.*。Fault の「状態を保存」で作る）
  let snapshots = $state<SnapshotRow[]>([]);
  let openSnapshot = $state<OpenSnapshot | null>(null);
  let snapshotError = $state<string | null>(null);

  const linked = $derived($app.link === "open");          // つながってから読む（つなぎ直したら読み直す）
  const recording = $derived(!!status?.recording);
  const hz = $derived<number>(status?.timebase_hz ?? 0);
  const span = $derived<[number, number] | null>(status?.span ?? null);
  const viewing = $derived($tl.source === "recording" && $tl.recording === status?.name && !!$tl.extent);
  const viewRange = $derived<[number, number] | null>(viewing ? [$tl.view.start, $tl.view.end] : null);

  $effect(() => {
    if (!linked) return;
    void refresh();
    // ほかの利用者（AI・別の窓も）が記録を開いた・始めた・止めたら読み直す（時間軸は TimelineState が読み直す）
    return onRecording(conn, () => void refresh());
  });
  $effect(() => {
    if (!recording || !linked) return;
    const timer = setInterval(() => void readStatus(), POLL_MS);
    return () => clearInterval(timer);
  });
  // 書き出しのパス: 手で変えるまでは既定（記録の名前と形式の拡張子。デーモンの PC の作業フォルダ）
  const target = $derived(edited ? path : defaultExportPath(status?.name, format));

  async function readStatus() {
    try {
      status = await conn.call("recording.status", {}, { quiet: true });
    } catch {
      status = null;
    }
  }

  async function refresh() {
    await readStatus();
    try {
      list = (await conn.call("recording.list", {}, { quiet: true })).recordings ?? [];
    } catch {
      list = [];
    }
    segments = null;
    if (status?.segmented) {
      try {
        segments = await conn.call("recording.segments", {}, { quiet: true });
      } catch {
        segments = null;
      }
    }
  }

  async function start() {
    save("recordings.name", name);
    save("recordings.segmentS", long.segmentS);
    save("recordings.maxMb", long.maxMb);
    save("recordings.decimateAfterS", long.decimateAfterS);
    let extra;
    try {
      extra = longTermParams(long);
      error = null;
    } catch (e) {
      error = errorText(e);
      return;
    }
    if (!(await allowed(app))) return;
    if (await app.act("recording.start", { name, ...extra }) !== undefined) await refresh();
  }

  async function stop() {
    if (!(await allowed(app))) return;
    if (await app.act("recording.stop") !== undefined) await refresh();
  }

  /** 開いて時間軸を記録に（全体を表示。記録を見ていれば、TimelineState が通知 recording で読み直す） */
  async function open(n: string) {
    const out = await app.act("recording.open", { name: n });
    if (out === undefined) return;
    await refresh();
    show(false);
  }

  /** 時間軸を記録に切り替えて前に出す（reload = 記録を見ていても読み直す: 記録中に増えた分） */
  function show(reload = true) {
    if (tl.snapshot.source !== "recording") tl.setSource("recording");
    else if (reload) tl.reload();
    dock.show("timeline");
  }

  async function readSnaps() {
    const r = await readSnapshots(conn);
    snapshots = r.rows;
    openSnapshot = r.open;
    snapshotError = r.error;
  }
  $effect(() => {
    if (!linked) return;
    void readSnaps();
    return conn.on("activity", (e: { method: string }) => {
      if (e.method.startsWith("snapshot.")) void readSnaps();     // だれが（AI も）保存・開く・閉じても
    });
  });

  /** 保存した状態を「止まったターゲット」として開く（デーモンの ELF とレジスタ・メモリの表示が切り替わる）ので確かめる */
  async function openSnap(s: SnapshotRow) {
    if (!(await allowed(app))) return;
    if (!(await confirmer.ask($i18n("snapshots.confirmOpen", { name: s.name }), { ok: $i18n("snapshots.open") }))) return;
    await app.act("snapshot.open", { path: s.path });
    await readSnaps();
  }

  async function closeSnap() {
    if (!(await allowed(app))) return;
    await app.act("snapshot.close", {});
    await readSnaps();
  }

  /** ターゲットの RAM・レジスタを保存した状態に書き戻す（ターゲットの今の状態を上書きする） */
  async function restoreSnap(s: SnapshotRow) {
    if (!(await allowed(app))) return;
    if (!(await confirmer.ask($i18n("snapshots.confirmRestore", { name: s.name }), { ok: $i18n("snapshots.restore") }))) return;
    await app.act("snapshot.restore", { path: s.path });
    await readSnaps();
  }

  async function exportNow() {
    save("recordings.format", format);
    const out = await app.act("recording.export", exportParams(format, target, scope === "view" ? viewRange : null));
    exported = out === undefined ? null : out.path ?? target;
  }

  const duration = $derived(span && hz ? formatDuration((span[1] - span[0]) / hz) : "");
  const bytes = (n: number) => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.ceil(n / 1024)} KB`);
</script>

<div class="pane recordings">
  <div class="state">
    {#if recording}
      <span class="dot rec" aria-hidden="true"></span>
      <strong>{$i18n("recordings.recording")}</strong>
    {:else if status?.name}
      <span class="dot" aria-hidden="true"></span>
      <strong>{$i18n("recordings.opened")}</strong>
    {:else}
      <strong>{$i18n("recordings.idle")}</strong>
    {/if}
    {#if status?.name}
      <span class="mono">{status.name}</span>
      <span class="muted">{$i18n("recordings.events", { n: (status.events ?? 0).toLocaleString() })}</span>
      {#if duration}<span class="muted">{duration}</span>{/if}
      {#if status.segmented}<span class="muted">{$i18n("recordings.segmented")}</span>{/if}
      <button class="small" onclick={() => show()}>{$i18n("recordings.showTimeline")}</button>
      <button class="small" onclick={() => dock.show("eventLog")}>{$i18n("recordings.showEvents")}</button>
    {/if}
    <span class="spacer"></span>
    {#if recording}
      <button class="primary" onclick={stop}>{$i18n("recordings.stop")}</button>
    {/if}
  </div>

  {#if !recording}
    <form class="start" onsubmit={(e) => { e.preventDefault(); void start(); }}>
      <label>{$i18n("common.name")} <input bind:value={name} placeholder={$i18n("recordings.namePlaceholder")} /></label>
      <button type="submit" class="primary">{$i18n("recordings.start")}</button>
      <details>
        <summary>{$i18n("recordings.longTerm")}</summary>
        <div class="long">
          <label title={$i18n("recordings.segmentTip")}>{$i18n("recordings.segmentS")}
            <input class="num" bind:value={long.segmentS} placeholder="3600" inputmode="decimal" /></label>
          <label title={$i18n("recordings.maxTip")}>{$i18n("recordings.maxMb")}
            <input class="num" bind:value={long.maxMb} placeholder="—" inputmode="numeric" /></label>
          <label title={$i18n("recordings.decimateTip")}>{$i18n("recordings.decimateAfterS")}
            <input class="num" bind:value={long.decimateAfterS} placeholder="—" inputmode="decimal" /></label>
        </div>
      </details>
    </form>
  {/if}
  {#if error}<div class="error">{error}</div>{/if}

  <div class="cols">
    <section>
      <h3>{$i18n("recordings.saved")}</h3>
      <div class="rows">
        <table class="grid">
          <thead><tr><th>{$i18n("common.name")}</th><th>{$i18n("recordings.title")}</th><th>{$i18n("recordings.board")}</th><th></th></tr></thead>
          <tbody>
            {#each list as r (r.name)}
              <tr class:current={r.name === status?.name} class:selected={r.name === chosen}
                  onclick={() => (chosen = r.name)} ondblclick={() => void open(r.name)}>
                <td class="mono">{r.name}</td>
                <td>{r.title ?? ""}</td>
                <td class="muted">{r.board ?? ""}</td>
                <td><button class="small" disabled={recording} onclick={() => void open(r.name)}>{$i18n("recordings.open")}</button></td>
              </tr>
            {:else}
              <tr><td colspan="4" class="muted">{$i18n("recordings.noneSaved")}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section>
      <h3>{$i18n("snapshots.title")}</h3>
      {#if openSnapshot}
        <div class="row-open">{$i18n("snapshots.viewing", { path: openSnapshot.path })}
          <button class="small" onclick={() => void closeSnap()}>{$i18n("snapshots.close")}</button></div>
      {/if}
      {#if snapshotError}<div class="error">{snapshotError}</div>{/if}
      <div class="rows">
        <table class="grid">
          <thead><tr><th>{$i18n("common.name")}</th><th>{$i18n("snapshots.created")}</th><th>{$i18n("snapshots.where")}</th>
            <th>{$i18n("snapshots.reason")}</th><th></th></tr></thead>
          <tbody>
            {#each snapshots as s (s.path)}
              <tr class:current={s.path === openSnapshot?.path} title={s.note || s.path}>
                <td class="mono">{s.name}</td>
                {#if s.error}<td colspan="3" class="error">{s.error}</td>
                {:else}<td class="muted">{s.created ?? ""}</td><td class="mono">{s.where ?? ""}</td><td>{s.reason ?? ""}</td>{/if}
                <td class="actions">
                  <button class="small" disabled={!!s.error} onclick={() => void openSnap(s)}>{$i18n("snapshots.open")}</button>
                  <button class="small" disabled={!!s.error} title={$i18n("snapshots.restoreTip")}
                          onclick={() => void restoreSnap(s)}>{$i18n("snapshots.restore")}</button>
                </td>
              </tr>
            {:else}
              <tr><td colspan="5" class="muted">{$i18n("snapshots.none")}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section>
      <h3>{$i18n("recordings.export")}</h3>
      {#if status?.name}
        <form class="export" onsubmit={(e) => { e.preventDefault(); void exportNow(); }}>
          <label>{$i18n("recordings.format")}
            <select bind:value={format}>
              {#each EXPORT_FORMATS as f (f.id)}<option value={f.id}>{f.label}</option>{/each}
            </select>
          </label>
          <label>{$i18n("recordings.range")}
            <select bind:value={scope} disabled={format === "vcd"}>
              <option value="all">{$i18n("eventLog.scopeAll")}</option>
              <option value="view" disabled={!viewRange}>{$i18n("eventLog.scopeView")}</option>
            </select>
          </label>
          <label class="path" title={$i18n("recordings.pathTip")}>{$i18n("recordings.path")}
            <input class="mono" value={target} oninput={(e) => { path = e.currentTarget.value; edited = true; }} /></label>
          <button type="submit">{$i18n("recordings.exportButton")}</button>
          {#if exported}<span class="muted">{$i18n("recordings.exported", { path: exported })}</span>{/if}
        </form>
      {:else}
        <p class="muted">{$i18n("recordings.exportNone")}</p>
      {/if}

      {#if segments?.segmented}
        <h3>{$i18n("recordings.segments")}</h3>
        <table class="grid">
          <thead><tr><th>#</th><th>{$i18n("recordings.segState")}</th><th>{$i18n("recordings.segFrom")}</th>
            <th>{$i18n("recordings.segEvents")}</th><th>{$i18n("recordings.segBytes")}</th><th>{$i18n("recordings.segDropped")}</th></tr></thead>
          <tbody>
            {#each segments.segments ?? [] as s (s.no)}
              <tr class:muted={s.state === "deleted"}>
                <td class="mono">{s.no}</td>
                <td>{s.state}</td>
                <td>{#if s.t0 !== null && s.t0 !== undefined && span && hz}
                  <TimeLink to={s.state === "deleted" ? null : { source: "recording", t: s.t0, tEnd: s.t1 ?? undefined }}
                            at={{ source: "recording", t: s.t0 }} fallback={{ t0: span[0], tickS: 1 / hz }} />{/if}</td>
                <td class="mono">{(s.events ?? 0).toLocaleString()}</td>
                <td class="mono">{bytes(s.bytes ?? 0)}</td>
                <td class="muted">{(s.dropped ?? []).join(", ")}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </section>
  </div>
</div>

<style>
  .recordings { display: flex; flex-direction: column; }
  .state, .start { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 6px 8px;
                   border-bottom: 1px solid var(--line); }
  .state .spacer { flex: 1; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); display: inline-block; }
  .dot.rec { background: var(--bad); animation: blink 1.2s infinite; }
  @keyframes blink { 50% { opacity: 0.3; } }
  details summary { cursor: pointer; color: var(--muted); }
  .long { display: flex; gap: 8px; flex-wrap: wrap; padding-top: 4px; }
  .num { width: 6em; }
  .error { color: var(--bad); padding: 4px 8px; }
  .cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; padding: 6px 8px; flex: 1;
          min-height: 0; }
  @media (max-width: 720px) { .cols { grid-template-columns: 1fr; } }
  section { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
  h3 { font-size: 12px; color: var(--muted); font-weight: 600; margin: 4px 0; }
  .rows { overflow: auto; min-height: 60px; }
  tr.current td { font-weight: 600; }
  tr.selected td { background: var(--select); }
  .export { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .row-open { display: flex; gap: 8px; align-items: center; font-size: 12px; }
  td.actions { white-space: nowrap; }
  .path input { width: 22em; max-width: 100%; }
</style>
