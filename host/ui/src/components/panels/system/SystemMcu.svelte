<script lang="ts">
  // システム解析の MCU 側: コアの状態と、止めずに読む番地（ファームウェアのメールボックス・カウンタ等。読んだ値と読んだ時刻）。
  // 番地の語は値のダブルクリックで書ける（target.memory.write。止めずに書ける）
  import { tick } from "svelte";
  import Hint from "../Hint.svelte";
  import { errorText, type Result } from "../../../lib/api";
  import { shared } from "../../../lib/context";
  import { i18n } from "../../../lib/i18n";
  import { haltReasonKey } from "../../../lib/target";
  import { addressKey, isSymbolName, parseAddress, symbolWatch, writeWord, type Watch } from "../../../lib/system";

  type Status = Result<"system.status">;
  type McuRead = Extract<Status["mcu"], { state: unknown }>;

  let { status, mcu, mcuError, error, linkOpen, watches = $bindable(), poll, onReconnect }: {
    status: Status | null; mcu: McuRead | null; mcuError: string | null; error: string | null; linkOpen: boolean;
    watches: Watch[]; poll: () => Promise<void>; onReconnect: () => void;
  } = $props();

  const { app, conn, dock } = shared();

  let newName = $state("");
  let newAddress = $state("");
  let addError = $state<string | null>(null);

  async function addWatch() {
    let a = parseAddress(newAddress);
    if (a === null && isSymbolName(newAddress)) {   // 変数名: 読み込んだ ELF で番地に（名前が空なら変数名）
      const info = await conn.call("debug.symbol.info", { location: newAddress.trim() }, { quiet: true }).catch((e) => {
        addError = errorText(e);
        return null;
      });
      if (!info) return;
      const w = symbolWatch(newAddress, newName, info);
      if (typeof w === "string") {
        addError = $i18n(w);
        return;
      }
      a = w.address;
      newName = w.name;
    }
    if (a === null || !newName.trim()) {
      addError = $i18n("system.badAddress");
      return;
    }
    if (watches.some((w) => w.name === newName.trim() && w.address !== a)) {   // 名前は記録の段・条件で使う
      addError = $i18n("system.duplicateName");
      return;
    }
    watches = [...watches.filter((w) => w.address !== a), { name: newName.trim(), address: a }];
    newName = newAddress = "";
    addError = null;
  }
  const removeWatch = (a: number) => (watches = watches.filter((w) => w.address !== a));

  // MCU の番地の語を書く: 値の欄をダブルクリック → Enter で書く・Esc でやめる。
  // AI の操作と同じ API。AI がつながっていて一時停止していなければ書く前に確かめる（writeWord の中の allowed）
  const canWrite = $derived(linkOpen && mcu !== null);
  let writing = $state<{ address: number; text: string } | null>(null);
  let writeError = $state<string | null>(null);
  function startWrite(address: number, v: number | undefined) {
    if (!canWrite) return;
    writing = { address, text: v === undefined ? "" : `0x${v.toString(16).toUpperCase()}` };
    writeError = null;
  }
  async function commitWrite() {
    if (!writing) return;
    const { address, text } = writing;
    const bad = await writeWord(app, address, text);
    if (bad) {
      writeError = $i18n(bad);
      return;
    }
    writing = null;
    writeError = null;
    await poll();                                    // 書いた値を読み直して見せる
  }
  function onWriteKey(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      void commitWrite();
    } else if (e.key === "Escape") {
      e.preventDefault();
      writing = null;
      writeError = null;
    }
  }
  const focusSelect = (el: HTMLInputElement) => {
    void tick().then(() => {
      el.focus();
      el.select();
    });
  };
</script>

<section class="peer" aria-label="MCU">
  <header>
    <span class="dot" class:ok={mcu !== null} class:bad={mcuError !== null}></span>
    <strong>MCU</strong>
    {#if mcu}
      <span class="mono">{mcu.state}</span>
      {#if haltReasonKey(mcu)}<span class="muted">{$i18n(haltReasonKey(mcu)!)}</span>{/if}
      <!-- DFSR の立っていたビット全部（halt_reason は 1 語。複数立っていた事実をそのまま） -->
      {#if mcu.state === "HALTED" && mcu.halt_bits?.length}<span class="muted mono">DFSR {mcu.halt_bits.join(" ")}</span>{/if}
      {#if mcu.state === "HALTED"}
        <!-- 止まった MCU の中（ELF が無くても使えるもの）: 呼び出し履歴・逆アセンブル・レジスタ -->
        <button title={$i18n("system.inspectTip")}
                onclick={() => ["registers", "disassembly", "callStack"].forEach((id) => dock.show(id))}>{$i18n("system.inspect")}</button>
      {/if}
      {#if mcu.target}<span class="muted">{mcu.target}{mcu.core !== undefined ? ` core ${mcu.core}` : ""}</span>{/if}
    {:else if status === null && linkOpen && error === null}
      <span class="muted" aria-busy="true">{$i18n("load.loading")}</span>
    {:else}
      <span class="muted">{$i18n("system.notConnected")}</span>
      {#if status && "linux_attaching" in status.mcu && status.mcu.linux_attaching}
        <button onclick={() => dock.show("linuxHalt")}>{$i18n("status.target.linuxAttaching", { pid: status.mcu.linux_attaching.pid })}</button>
      {:else if status && "linux_halt_open" in status.mcu && status.mcu.linux_halt_open}
        <button title={$i18n("status.target.linuxHaltOpenTip", { backend: status.mcu.linux_halt_open.backend })}
                onclick={() => dock.show("linuxHalt")}>{$i18n("status.target.linuxHaltOpen")}</button>
      {/if}
    {/if}
  </header>
  {#if mcuError !== null}
    <div class="note bad">{mcuError}
      {#if status?.mcu.connect}<button title={$i18n("system.reconnectTip")} onclick={onReconnect}>{$i18n("system.reconnect")}</button>{/if}
    </div>
  {/if}
  <div class="sub-title" title={$i18n("system.memoryTip")}>{$i18n("system.memory")}</div>
  {#if $app.target?.linux}<div class="note muted">{$i18n("system.memoryLinux")}</div>{/if}
  {#if !watches.length}<Hint text={$i18n("system.memoryHint")} />{/if}
  <table class="grid">
    <thead><tr><th>{$i18n("system.name")}</th><th>{$i18n("system.address")}</th><th>{$i18n("system.value")}</th>
      <th></th></tr></thead>
    <tbody>
      {#each watches as w (w.address)}
        {@const v = mcu?.memory[addressKey(w.address)]}
        <tr>
          <td>{w.name}</td>
          <td class="mono muted">{addressKey(w.address)}</td>
          <td class="mono">
            {#if writing?.address === w.address}
              <input class="mono write" aria-label={$i18n("system.writeValue")} bind:value={writing.text} use:focusSelect
                     onkeydown={onWriteKey} onblur={() => (writing = null)} />
            {:else}
              <!-- svelte-ignore a11y_no_static_element_interactions -->
              <span class:writable={canWrite} title={canWrite ? $i18n("system.writeTip") : undefined}
                    ondblclick={() => startWrite(w.address, v)}>{v === undefined ? "—" : `${v} (0x${v.toString(16).toUpperCase()})`}</span>
            {/if}
          </td>
          <td class="nowrap"><button class="x" title={$i18n("fpgaObs.remove")} aria-label={$i18n("fpgaObs.remove")} onclick={() => removeWatch(w.address)}>✕</button></td>
        </tr>
      {/each}
      <tr>
        <td><input placeholder={$i18n("system.name")} bind:value={newName} /></td>
        <td><input class="mono" placeholder="0x20040800 / g_errors" bind:value={newAddress}
                   onkeydown={(e) => e.key === "Enter" && addWatch()} /></td>
        <td colspan="2"><button onclick={addWatch}>{$i18n("system.addWatch")}</button></td>
      </tr>
    </tbody>
  </table>
  {#if status && mcu && watches.length}
    <div class="muted small">{$i18n("system.readAt", { t: (status.time_us / 1e6).toFixed(3) })}</div>
  {/if}
  {#if writeError}<div class="note bad">{writeError}</div>{/if}
  {#if addError}<div class="note bad">{addError}</div>{/if}
</section>

<style>
  .peer { border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; min-width: 0; }
  .peer header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--line); }
  .dot.ok { background: var(--ok); }
  .dot.bad { background: var(--bad); }
  .sub-title { font-weight: 600; font-size: 12px; margin: 6px 0 2px; }
  .note { padding: 3px 8px; font-size: 12px; }
  .bad { color: var(--bad); }
  .small { font-size: 12px; padding: 2px 0; }
  input { width: 100%; min-width: 5em; box-sizing: border-box; }
  .grid { width: 100%; table-layout: auto; }
  .grid th { white-space: nowrap; text-align: left; }
  .nowrap, .nowrap button { white-space: nowrap; }
  /* 書ける値（ダブルクリックで書く）: 点線の下線と文字のカーソル */
  .writable { cursor: text; border-bottom: 1px dashed var(--muted); }
  .writable:hover { border-bottom-color: var(--accent); }
  input.write { min-width: 8em; }
  button.x { padding: 0 6px; }
</style>
