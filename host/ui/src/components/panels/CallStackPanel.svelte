<script lang="ts">
  // Call Stack: 呼び出し履歴（いまのフレームが上）。フレームを選ぶと Locals・Watch・Disassembly がそのフレームに
  // なり、Source はその行を開いて選ぶ（VS Code・Ozone と同じ）。↑/↓ で選び直す。
  // 上に時間軸で選んだ記録の時刻の事実（CodeAtNote。呼び出し履歴は記録に無い）
  import { shared } from "../../lib/context";
  import { focusFrame } from "../../lib/debugger";
  import { i18n } from "../../lib/i18n";
  import CodeAtNote from "./CodeAtNote.svelte";
  import Empty from "./Empty.svelte";
  import { hex32 } from "./format";

  const { dbg, ui } = shared();

  const go = (index: number) => focusFrame(dbg, ui, index);

  function onKey(e: KeyboardEvent) {
    const next = e.key === "ArrowDown" ? $dbg.frame + 1 : e.key === "ArrowUp" ? $dbg.frame - 1 : -1;
    if (next < 0 || next >= $dbg.backtrace.length) return;
    e.preventDefault();
    void go(next);
    (e.currentTarget as HTMLElement).querySelectorAll<HTMLElement>(".frame")[next]?.focus();
  }
</script>

<div class="pane" class:stale={$dbg.stale}>
  <CodeAtNote panel="callStack" />
  {#if $dbg.backtrace.length}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <ol class="frames mono" aria-label={$i18n("panel.callStack")} onkeydown={onKey}>
      {#each $dbg.backtrace as f, i (i)}
        <li class:top={i === 0} class:selected={i === $dbg.frame}>
          <button class="frame" title={f.note || f.where} aria-current={i === $dbg.frame} onclick={() => go(i)}>
            <span class="mark">{i === $dbg.frame ? "▶" : ""}</span>
            <span class="where">{f.where}</span>
            {#if f.note === "exception"}<span class="note">{$i18n("callStack.exception")}</span>{/if}
            {#if f.note === "lr_without_cfi"}<span class="note">{$i18n("callStack.lrWithoutCfi")}</span>{/if}
            <span class="muted">{hex32(f.pc)}</span>
          </button>
        </li>
      {/each}
    </ol>
  {:else}
    <Empty need="halted" />
  {/if}
</div>

<style>
  .frames { list-style: none; margin: 0; padding: 4px 0; }
  .frame { display: flex; align-items: center; gap: 8px; width: 100%; border: none; border-radius: 0;
           background: none; padding: 2px 10px 2px 4px; text-align: left; }
  .frame:hover { background: var(--select); }
  .selected .frame { background: var(--select); }
  .mark { width: 12px; font-size: 10px; color: var(--warn); flex: none; }
  .top .where { font-weight: 600; }
  .where { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .note { font-size: 11px; color: var(--warn); border: 1px solid currentColor; border-radius: 4px; padding: 0 4px; }
</style>
