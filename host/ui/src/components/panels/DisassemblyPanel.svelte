<script lang="ts">
  // Disassembly: 止まった PC（呼び出し履歴で呼び出し元を選んでいればその戻り先。時間軸・ビューから番地を頼まれたら
  // その番地）の前後の命令（ソースの行の見出しを挟む）。いまの命令を強調。
  // 左の余白で命令の番地にブレークポイント（止めると壊れうるターゲットは確かめる）。番地の欄（番地・関数名・C の式。
  // Memory と同じ解き方）でその場所の命令を見る（走っていても ELF から）。命令ステップ（1 命令）
  import { tick } from "svelte";
  import { errorText } from "../../lib/api";
  import { command, type CommandState } from "../../lib/commands";
  import { shared } from "../../lib/context";
  import { markedAddresses } from "../../lib/debugger";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { resolveAddress } from "../../lib/memory";
  import Empty from "./Empty.svelte";

  const ctx = shared();
  const { app, conn, dbg, ui, safety, showAddress } = ctx;
  let box = $state<HTMLElement>();
  let input = $state("");
  let error = $state<string | null>(null);

  const online = $derived($app.link === "open");
  const marked = $derived(markedAddresses($dbg));
  const framePc = $derived($dbg.frame > 0 ? $dbg.backtrace[$dbg.frame]?.pc : undefined);
  const cs = $derived<CommandState>({ link: $app.link, target: $app.target?.state, elf: $dbg.elf, selection: $ui.selection });
  const step = command("stepInstruction");
  // いまの命令（頼まれた番地、選んだフレームの命令）を見える位置へ
  $effect(() => {
    const pc = $dbg.focus ?? framePc ?? $dbg.stop?.pc;
    if (pc === undefined || !box || !$dbg.disasm.length) return;
    const el = box;
    const which = $dbg.focus !== null ? ".ins.focus" : framePc !== undefined ? ".ins.frame" : ".ins.pc";
    tick().then(() => el.querySelector(which)?.scrollIntoView({ block: "center" }));
  });
  const hex = (n: number) => n.toString(16).toUpperCase().padStart(8, "0");

  async function go(e: Event) {
    e.preventDefault();
    if (!input.trim()) return;
    try {
      showAddress(await resolveAddress(conn.call.bind(conn), input, $dbg.frame));
      error = null;
    } catch (err) {
      error = errorText(err);
    }
  }

  async function toggle(address: number) {
    if (!(await allowed(app))) return;
    if (!marked.has(address) && !(await safety.confirmHalt("breakpoint", $i18n))) return;
    await dbg.toggleAddress(address);
  }
</script>

<div class="pane disasm" class:stale={$dbg.stale}>
  {#if $dbg.elf || $dbg.disasm.length}
    <form class="pane-bar" onsubmit={go}>
      <input class="mono where" bind:value={input} placeholder={$i18n("disasm.address")} aria-label={$i18n("disasm.address")}
             title={$i18n("disasm.addressTip")} spellcheck="false" disabled={!online} />
      <button class="small" disabled={!online || !input.trim()}>{$i18n("disasm.go")}</button>
      {#if error}<span class="bad small-text" title={error}>{error}</span>{/if}
      <span class="spacer"></span>
      <button type="button" class="small" disabled={step.blocked(cs) !== null} title={$i18n("tip.stepInstruction")}
              onclick={() => void step.run(ctx, cs)}>{$i18n("cmd.stepInstruction")}</button>
    </form>
  {/if}
  <div class="body" bind:this={box}>
    {#if $dbg.disasm.length}
      <div class="listing mono">
        {#each $dbg.disasm as ins (ins.address)}
          {#if ins.new_line && ins.file}
            <div class="src muted">{ins.file}:{ins.line} — {ins.function ?? ""}</div>
          {/if}
          <div class="ins" class:pc={ins.address === $dbg.stop?.pc} class:frame={ins.address === framePc}
               class:focus={ins.address === $dbg.focus}>
            <button class="gutter" disabled={!online} onclick={() => toggle(ins.address)}
                    title={$i18n("disasm.breakpointTip", { address: hex(ins.address) })}
                    aria-label={$i18n("disasm.breakpointTip", { address: hex(ins.address) })}>
              {#if marked.has(ins.address)}<span class="dot"></span>{:else}<span class="can"></span>{/if}
            </button>
            <span class="arrow">{ins.address === $dbg.stop?.pc ? "▶" : ins.address === framePc ? "▷" : ""}</span>
            <span class="muted">{hex(ins.address)}</span>
            <span class="muted bytes">{ins.bytes ?? ""}</span>
            <span><b>{ins.mnemonic}</b> {ins.operands}</span>
          </div>
        {/each}
      </div>
    {:else if $dbg.stop}
      <p class="empty">{$i18n("disasm.empty")}</p>
    {:else}
      <Empty need="halted" />
    {/if}
  </div>
</div>

<style>
  .disasm { display: flex; flex-direction: column; }
  .body { flex: 1; min-height: 0; overflow: auto; }
  .where { width: 18ch; }
  .small-text { font-size: 12px; max-width: 30ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bad { color: var(--bad); }
  .listing { padding: 4px 0; font-size: 12.5px; }
  .src { padding: 4px 10px 0 28px; font-size: 11.5px; }
  .ins { display: grid; grid-template-columns: 18px 16px 76px 64px 1fr; gap: 6px; padding: 0 10px 0 4px; align-items: center; }
  .ins.pc { background: color-mix(in srgb, var(--warn) 20%, transparent); }
  .ins.frame { background: color-mix(in srgb, var(--ok) 16%, transparent); }
  .ins.frame .arrow { color: var(--ok); }
  .ins.focus { outline: 1px solid var(--accent); background: var(--select); }
  .arrow { color: var(--warn); }
  .gutter { border: none; background: none; padding: 0; height: 17px; display: grid; place-items: center; cursor: pointer; }
  .gutter:disabled { cursor: default; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--bad); }
  .can { width: 8px; height: 8px; border-radius: 50%; }
  .gutter:not(:disabled):hover .can { background: color-mix(in srgb, var(--bad) 45%, transparent); }
  b { font-weight: 600; }
</style>
