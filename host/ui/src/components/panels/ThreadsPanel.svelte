<script lang="ts">
  // Threads: RTOS のタスク（状態・優先度・待っているもの・スタックの使用量とあふれ）。止まるたびに読み、走っている間は
  // 1 秒ごとに走らせたまま読む（rtos.tasks の live。止めると BLE 等の接続が切れるターゲットでも見られる）。
  // 行を開くとそのタスクの呼び出し履歴（rtos.backtrace。フレームはソース・逆アセンブルへのリンク）。
  // 行のダブルクリックで時間軸のそのタスクの次の実行へ（記録があるとき）。時間軸で選んだ時刻に動いていたタスクを強調
  import { errorText } from "../../lib/api";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";
  import { isRunning } from "../../lib/commands";
  import { nextRun, runningAt, taskStateText, waitText } from "../../lib/rtos";
  import { onTimeline } from "../../lib/timeline/events";
  import { confirmer } from "../../lib/confirm";
  import { allowed } from "../../lib/guard";
  import { boundsParams, readUsage, usagePercent, type StackUsage } from "../../lib/stack";
  import { load, save } from "../../lib/prefs";
  import Empty from "./Empty.svelte";
  import { hex32 } from "./format";

  const { app, conn, tl, reveal, showAddress } = shared();
  let info = $state<any>(null);
  let failed = $state(false);
  let open = $state<Set<number>>(new Set());
  let frames = $state<Record<number, { frames?: any[]; error?: string }>>({});
  let atSelection = $state<number | null>(null);         // 時間軸で選んだ時刻に動いていたタスク（TCB）
  let note = $state("");
  // 主スタック（MSP）: 塗りの残りから使用量（ベアメタルでも。RTOS のスレッドの表とは別）
  let msp = $state<StackUsage | null>(null);
  let mspError = $state<string | null>(null);
  // 範囲（ELF に _estack 等が無いときは人が明示する。その人のブラウザに覚える）
  let mspBottom = $state(load("stack.bottom", ""));
  let mspTop = $state(load("stack.top", ""));

  const halted = $derived($app.target?.state === "HALTED");
  const running = $derived(isRunning($app.target?.state));
  const key = $derived(halted ? `${$app.target?.pc}/${$app.target?.run}` : "");

  $effect(() => {
    if (key) void read(false);
  });

  // 走っている間は 1 秒ごとに走らせたまま読む
  $effect(() => {
    if (!running) return;
    void read(true);
    const timer = setInterval(() => void read(true), 1000);
    return () => clearInterval(timer);
  });

  // 時間軸の 1 回クリック（記録）: その時刻に動いていたタスクの行を強調して見える位置へ
  $effect(() => onTimeline({
    select: (d) => {
      if (d.source !== "recording") return;
      void runningAt(conn, d.t).then((r) => {
        atSelection = r?.handle ?? null;
        if (r) document.getElementById(`task-${r.handle}`)?.scrollIntoView({ block: "nearest" });
      }).catch(() => (atSelection = null));
    },
  }));

  async function read(live: boolean) {
    try {
      info = await conn.call("rtos.tasks", live ? { live: true } : {}, { quiet: true });
      failed = false;
      frames = {};
      for (const h of open) void loadFrames(h);
    } catch {
      info = null;
      failed = true;
    }
  }

  async function loadFrames(handle: number) {
    try {
      const r = await conn.call("rtos.backtrace", { task: handle }, { quiet: true });
      frames = { ...frames, [handle]: { frames: r.frames } };
    } catch (e) {
      frames = { ...frames, [handle]: { error: errorText(e) } };
    }
  }

  function toggle(handle: number) {
    const next = new Set(open);
    if (next.has(handle)) next.delete(handle);
    else {
      next.add(handle);
      if (!frames[handle]) void loadFrames(handle);
    }
    open = next;
  }

  /** 時間軸のこのタスクの、選択カーソルの後の最初の実行へ */
  async function revealNext(task: any) {
    const s = tl.snapshot;
    const after = s.source === "recording" && s.selection !== null ? s.selection : Number.MIN_SAFE_INTEGER;
    note = "";
    try {
      const run = await nextRun(conn, task.handle, after);
      if (run) reveal({ source: "recording", t: run.t, tEnd: run.tEnd });
      else note = $i18n("threads.noLaterRun", { task: task.name });
    } catch (e) {
      note = errorText(e);
    }
  }

  const pct = (s: any) => (s?.size ? Math.round((100 * (s.peak_used ?? s.used ?? 0)) / s.size) : 0);

  function mspBounds(): Record<string, number> | null {
    const b = boundsParams(mspBottom, mspTop);
    if ("error" in b) {
      mspError = $i18n("stack.badBound", { field: b.error });
      return null;
    }
    save("stack.bottom", mspBottom);
    save("stack.top", mspTop);
    return b.params;
  }

  async function readMsp() {
    const bounds = mspBounds();
    if (!bounds) return;
    const r = await readUsage(conn, bounds);
    msp = r.usage;
    mspError = r.error;
  }

  /** 使っていない領域（SP より下）をパターンで埋める。RAM を書くので確かめる */
  async function paintMsp() {
    if (!(await allowed(app))) return;
    const bounds = mspBounds();
    if (!bounds) return;
    if (!(await confirmer.ask($i18n("stack.confirmPaint"), { ok: $i18n("stack.paint") }))) return;
    if ((await app.act("target.stack.paint", bounds)) !== undefined) await readMsp();
  }
</script>

<div class="pane">
  {#if info && (halted || running)}
    <div class="pane-bar">
      <span class="mono">{info.rtos}</span>{#if info.tick !== undefined && info.tick !== null}<span class="muted">{$i18n("threads.tick", { n: info.tick })}</span>{/if}
      {#if running}<span class="muted">{$i18n("threads.live")}</span>{/if}
      {#if note}<span class="muted note">{note}</span>{/if}
    </div>
    <table class="grid">
      <thead><tr><th>{$i18n("common.name")}</th><th>{$i18n("threads.state")}</th><th>{$i18n("threads.priority")}</th>
        <th>{$i18n("threads.waiting")}</th><th>{$i18n("threads.stack")}</th></tr></thead>
      <tbody>
        {#each info.tasks as task (task.handle)}
          <tr id="task-{task.handle}" class="task" class:current={task.name === info.current}
              class:at={task.handle === atSelection} title={$i18n("threads.rowTip")}
              onclick={() => toggle(task.handle)} ondblclick={() => revealNext(task)}>
            <td class="mono name">
              <span class="caret">{open.has(task.handle) ? "▾" : "▸"}</span>{task.name === info.current ? "▶ " : ""}{task.name}
              {#if task.handle === atSelection}<span class="badge" title={$i18n("threads.runningAt")}>●</span>{/if}
            </td>
            <td>{taskStateText(task.state, $i18n)}</td>
            <td class="mono">{task.priority}</td>
            <td class="muted">{waitText(task.wait, $i18n)}</td>
            <td class="mono stack" title="{task.stack.peak_used ?? "?"} / {task.stack.size ?? "?"}">
              <span class="bar"><span style:width="{pct(task.stack)}%" class:over={task.stack.overflow}></span></span>
              {task.stack.peak_used ?? task.stack.used ?? "?"} / {task.stack.size ?? "?"}
              {#if task.stack.overflow}<span class="bad" title={$i18n("threads.overflow")}>⚠</span>{/if}
            </td>
          </tr>
          {#if open.has(task.handle)}
            <tr class="frames"><td colspan="5">
              {#if frames[task.handle]?.error}
                <span class="bad">{$i18n("threads.backtraceFailed", { error: frames[task.handle].error ?? "" })}</span>
              {:else if frames[task.handle]?.frames}
                <ol class="mono">
                  {#each frames[task.handle].frames ?? [] as f (f.index)}
                    <li>
                      <button class="link" title={$i18n("threads.frameTip")} onclick={() => showAddress(f.pc)}>
                        {f.where ?? f.function ?? hex32(f.pc)}</button>
                      {#if f.note === "exception"}<span class="muted">{$i18n("callStack.exception")}</span>{/if}
                      {#if f.note === "lr_without_cfi"}<span class="muted">{$i18n("callStack.lrWithoutCfi")}</span>{/if}
                      <span class="muted">{hex32(f.pc)}</span>
                    </li>
                  {/each}
                </ol>
              {:else}
                <span class="muted">…</span>
              {/if}
            </td></tr>
          {/if}
        {/each}
      </tbody>
    </table>
  {:else if failed && (halted || running)}
    <div class="empty"><strong>{$i18n("threads.none")}</strong><span>{$i18n("threads.noneHint")}</span></div>
  {:else}
    <Empty need="halted" />
  {/if}
  <details class="msp">
    <summary>{$i18n("stack.msp")}</summary>
    <div class="msp-bar">
      <button class="small" disabled={!halted} title={$i18n("stack.paintTip")} onclick={() => void paintMsp()}>{$i18n("stack.paint")}</button>
      <button class="small" onclick={() => void readMsp()}>{$i18n("stack.read")}</button>
      <input class="mono bound" bind:value={mspBottom} placeholder={$i18n("stack.bottom")} aria-label={$i18n("stack.bottom")}
             title={$i18n("stack.boundTip")} />
      <input class="mono bound" bind:value={mspTop} placeholder={$i18n("stack.top")} aria-label={$i18n("stack.top")}
             title={$i18n("stack.boundTip")} />
    </div>
    {#if mspError}<div class="bad note">{mspError}</div>{/if}
    {#if msp}
      {@const p = usagePercent(msp)}
      <div class="mono stack">
        <span class="bar wide"><span style:width="{p ?? 0}%" class:over={msp.overflow}></span></span>
        {msp.used ?? "?"} / {msp.size ?? "?"}{#if p !== null}{" "}({p}%){/if}
        {#if msp.overflow}<span class="bad">{$i18n("threads.overflow")}</span>{/if}
        {#if msp.reached_bottom}<span class="bad">{$i18n("stack.reachedBottom")}</span>{/if}
      </div>
      <div class="muted mono note">{hex32(msp.bottom ?? 0)}–{hex32(msp.top ?? 0)}{#if msp.sp != null}{" · SP "}{hex32(msp.sp)}{/if}{#if msp.current != null}{" · "}{$i18n("stack.current", { n: String(msp.current) })}{/if}</div>
      {#if msp.warning}<div class="bad note">{msp.warning}</div>{/if}
      {#each msp.notes ?? [] as n, k (k)}<div class="muted note">{n}</div>{/each}
    {/if}
  </details>
</div>

<style>
  .task { cursor: pointer; }
  .task:hover td { background: var(--select); }
  .current td { font-weight: 600; }
  .at td { background: color-mix(in srgb, var(--accent) 14%, transparent); }
  .name { white-space: nowrap; }
  .caret { display: inline-block; width: 12px; color: var(--muted); }
  .badge { color: var(--accent); margin-left: 4px; font-size: 10px; }
  .stack { white-space: nowrap; }
  .bar { display: inline-block; width: 60px; height: 6px; background: var(--line); border-radius: 3px; margin-right: 6px;
         vertical-align: middle; overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--accent); }
  .bar span.over { background: var(--bad); }
  .bad { color: var(--bad); }
  .note { font-size: 12px; }
  .frames ol { margin: 0; padding: 2px 0 4px 28px; }
  .frames li { display: flex; gap: 8px; }
  .msp { padding: 6px 8px; border-top: 1px solid var(--line); }
  .msp summary { font-size: 12px; font-weight: 600; color: var(--muted); cursor: pointer; }
  .msp-bar { display: flex; gap: 6px; margin: 6px 0; }
  .bar.wide { width: 160px; }
  .bound { width: 9em; }
</style>
