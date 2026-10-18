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
  import Empty from "./Empty.svelte";
  import { hex32 } from "./format";

  const { app, conn, tl, reveal, showAddress } = shared();
  let info = $state<any>(null);
  let failed = $state(false);
  let open = $state<Set<number>>(new Set());
  let frames = $state<Record<number, { frames?: any[]; error?: string }>>({});
  let atSelection = $state<number | null>(null);         // 時間軸で選んだ時刻に動いていたタスク（TCB）
  let note = $state("");

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
</style>
