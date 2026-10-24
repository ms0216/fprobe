<script lang="ts">
  // Memory: 16 進表示（番地は式。単位 8/16/32、前回の読みから変わった値を赤く）。止まるたび・誰か（AI も）が
  // 止まっている間に書いたときに読み直し、Auto Refresh で周期的にも読む（走行中も AHB-AP で読める）。
  // 値のダブルクリックで書く（Enter で書き、Tab で書いて次へ、Esc でやめる）。右の文字の列もダブルクリックで文字で書ける
  // （変えた文字のバイトだけ）。時間軸・ほかのビューから番地を頼まれたらそこを開く
  import { errorText } from "../../lib/api";
  import { tick, untrack } from "svelte";
  import { shared } from "../../lib/context";
  import { onCalls } from "../../lib/notify";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { parseCell, resolveAddress, rowBytes, textEdit, unitHex } from "../../lib/memory";
  import { load, save } from "../../lib/prefs";
  import Empty from "./Empty.svelte";

  const LENGTH = 256;
  // 止まっている間にメモリを変え得る操作（ほかの利用者のものも）
  const WRITES = ["target.memory.write", "debug.write_expression", "debug.variable.write", "target.peripheral.write",
                  "target.registers.write", "debug.call", "target.program_ram"];
  const PERIODS = [500, 1000, 2000, 5000];

  const { app, conn, dbg, ui } = shared();
  let input = $state(load("memory.address", ""));   // 番地は使う人が入れる（特定の品種の SRAM を入れない）
  let unit = $state(Number(load("memory.unit", "8")));
  let auto = $state(load("memory.auto", "0") === "1");
  let period = $state(Number(load("memory.period", "1000")));
  let base = $state<number | null>(null);          // いま見ている番地（式を解いたもの）
  let view = $state<any>(null);
  let error = $state<string | null>(null);
  let failed = $state(false);                       // 最後の読みが失敗した（表は前の中身。古いと示す）
  let editing = $state<{ row: number; col: number; text: string } | null>(null);
  let editingText = $state<{ row: number; text: string } | null>(null);     // 文字の列の編集
  let reading = false;

  const target = $derived($app.target);
  const connected = $derived(!!target?.state && target.state !== "DISCONNECTED");
  const halted = $derived(target?.state === "HALTED");
  const key = $derived(halted ? `${target?.pc}/${target?.run}` : "");
  const digits = $derived(unit / 4);

  // 止まるたびに読み直す（読みの中で使う欄・フレームの変化では読まない）
  $effect(() => {
    if (key) untrack(() => void go(true));
  });
  // 時間軸のダブルクリック・ほかのビューから頼まれた番地を開く
  let shownSeq = 0;
  $effect(() => {
    const want = $ui.memory;
    if (!want || want.seq === shownSeq) return;
    shownSeq = want.seq;
    untrack(() => {
      input = `0x${want.address.toString(16).toUpperCase().padStart(8, "0")}`;
      void go();
    });
  });
  // ほかの操作（AI の書き込みも）で止まっている間にメモリが変わり得たら読み直す
  $effect(() => onCalls(conn, WRITES, () => {
    if (halted && base !== null) void read(true);
  }));
  // 周期的な読み直し
  $effect(() => {
    save("memory.auto", auto ? "1" : "0");
    save("memory.period", String(period));
    if (!auto || !connected) return;
    const timer = setInterval(() => base !== null && !editing && !editingText && void read(true), period);
    return () => clearInterval(timer);
  });

  /** 番地の式を解いて読む */
  async function go(compare = false) {
    save("memory.address", input);
    save("memory.unit", String(unit));
    if (!input.trim()) {                            // 入れていなければ読まない
      base = view = null;
      error = null;
      return;
    }
    try {
      base = await resolveAddress(conn.call.bind(conn), input, $dbg.frame);
      error = null;
    } catch (e) {
      error = errorText(e);
      base = view = null;                           // 前の番地の中身を、新しい番地のものと見誤らせない
      return;
    }
    await read(compare);
  }

  async function read(compare = false) {
    if (base === null || reading) return;
    reading = true;
    try {
      view = await conn.call("target.memory.view", { address: base, length: LENGTH, unit: unit as 8 | 16 | 32, width: 16, compare },
                             { quiet: true });
      error = null;
      failed = false;
    } catch (e) {
      error = errorText(e);
      failed = true;
    } finally {
      reading = false;
    }
  }

  function page(dir: -1 | 1) {
    if (base === null) return;
    base = Math.max(0, base + dir * LENGTH) >>> 0;
    input = `0x${base.toString(16).toUpperCase().padStart(8, "0")}`;
    save("memory.address", input);
    void read();
  }

  function startEdit(row: number, col: number) {
    if (!connected) return;
    editing = { row, col, text: hex(view.rows[row].values[col], digits) };
  }

  /** 書く。next なら次の単位の欄へ */
  async function commit(next: boolean) {
    if (!editing || !view) return;
    const { row, col, text } = editing;
    const value = parseCell(text, unit);
    const r = view.rows[row];
    if (value === null) {
      error = $i18n("memory.badValue", { value: text, bits: unit });
      return;
    }
    const cols = r.values.length;
    editing = next ? (col + 1 < cols ? { row, col: col + 1, text: "" } : row + 1 < view.rows.length ? { row: row + 1, col: 0, text: "" } : null) : null;
    if (value !== r.values[col] && (await allowed(app))) {
      const address = r.address + (col * unit) / 8;
      // 読み直しは書き込みの記録（activity）で。変わった値に印が付く
      await app.act("target.memory.write", { address, data_hex: unitHex(value, unit) });
    }
    if (editing) editing = { ...editing, text: hex(view.rows[editing.row].values[editing.col], digits) };
  }

  /** 文字の列を書く（変えた文字のバイトだけ。"." のまま残した所は元のバイト） */
  async function commitText() {
    if (!editingText || !view) return;
    const r = view.rows[editingText.row];
    let change: { offset: number; data_hex: string } | null;
    try {
      change = textEdit(rowBytes(r.values, unit), r.text, editingText.text);
    } catch {
      error = $i18n("memory.badText");
      return;
    }
    editingText = null;
    if (change && (await allowed(app))) await app.act("target.memory.write", { address: r.address + change.offset, data_hex: change.data_hex });
  }

  function onTextKey(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      void commitText();
    } else if (e.key === "Escape") {
      e.preventDefault();
      editingText = null;
    }
  }

  /** 欄から離れたらやめる（Tab で次の欄へ移ったときの、前の欄の blur ではやめない） */
  function leave(row: number, col: number) {
    if (editing?.row === row && editing.col === col) editing = null;
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      void commit(e.key === "Tab");
    } else if (e.key === "Escape") {
      e.preventDefault();
      editing = null;
    }
  }

  const focus = (el: HTMLInputElement) => {
    void tick().then(() => {
      el.focus();
      el.select();
    });
  };
  const hex = (n: number, width: number) => n.toString(16).toUpperCase().padStart(width, "0");
</script>

<div class="pane">
  <form class="pane-bar" onsubmit={(e) => { e.preventDefault(); void go(); }}>
    <label>{$i18n("memory.address")} <input class="mono addr" bind:value={input} title={$i18n("memory.addressHint")}
      aria-required="true" /><span class="req" aria-hidden="true">*</span></label>
    <button disabled={!connected}>{$i18n("common.go")}</button>
    <button type="button" class="small" disabled={base === null || !connected} title={$i18n("memory.previous")}
            aria-label={$i18n("memory.previous")} onclick={() => page(-1)}>‹</button>
    <button type="button" class="small" disabled={base === null || !connected} title={$i18n("memory.next")}
            aria-label={$i18n("memory.next")} onclick={() => page(1)}>›</button>
    <label>{$i18n("memory.unit")}
      <select bind:value={unit} onchange={() => read()}>
        {#each [8, 16, 32] as u (u)}<option value={u}>{u}</option>{/each}
      </select>
    </label>
    <label title={$i18n("memory.autoTip")}><input type="checkbox" bind:checked={auto} /> {$i18n("memory.auto")}</label>
    {#if auto}
      <select bind:value={period} aria-label={$i18n("memory.period")}>
        {#each PERIODS as p (p)}<option value={p}>{p < 1000 ? `${p} ms` : `${p / 1000} s`}</option>{/each}
      </select>
    {/if}
    <span class="spacer"></span>
    {#if error}<span class="bad" title={error}>{error}</span>{/if}
  </form>
  {#if view && connected}
    <table class="dump mono" class:stale={(!halted && !auto) || failed} title={failed ? $i18n("memory.staleTip") : undefined}>
      <tbody>
        {#each view.rows as row, ri (row.address)}
          <tr>
            <td class="muted">{hex(row.address, 8)}</td>
            <td>
              {#each row.values as v, i (i)}
                {#if editing && editing.row === ri && editing.col === i}
                  <input class="cell" style:width="{digits + 1.5}ch" maxlength={digits + 2} aria-label={$i18n("memory.edit")}
                         bind:value={editing.text} use:focus onkeydown={onKey} onblur={() => leave(ri, i)} />
                {:else}
                  <!-- svelte-ignore a11y_no_static_element_interactions -->
                  <span class="cellv" class:changed={row.changed?.[i] === true} title={$i18n("memory.editTip")}
                        ondblclick={() => startEdit(ri, i)}>{hex(v, digits)}</span>
                {/if}
              {/each}
            </td>
            {#if editingText && editingText.row === ri}
              <td><input class="cell text" style:width="{row.text.length + 1.5}ch" maxlength={row.text.length}
                         aria-label={$i18n("memory.editText")} bind:value={editingText.text} use:focus
                         onkeydown={onTextKey} onblur={() => (editingText = null)} /></td>
            {:else}
              <!-- svelte-ignore a11y_no_static_element_interactions -->
              <td class="muted text" title={$i18n("memory.editTextTip")}
                  ondblclick={() => connected && (editingText = { row: ri, text: row.text })}>{row.text}</td>
            {/if}
          </tr>
        {/each}
      </tbody>
    </table>
    {#if view.names?.length}
      <div class="names muted mono">
        {#each view.names as n (n.address + n.name)}<span>{hex(n.address, 8)} {n.name}</span>{/each}
      </div>
    {/if}
  {:else}
    <Empty need="connected" />
    {#if connected && !view}<p class="empty">{$i18n("memory.hint")}</p>{/if}
  {/if}
</div>

<style>
  .addr { width: 180px; }
  .bad { color: var(--bad); font-size: 12px; max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dump { border-collapse: collapse; margin: 4px 0; font-size: 12.5px; }
  .dump td { padding: 1px 10px; white-space: nowrap; }
  .cellv { margin-right: 6px; cursor: text; }
  .cell { margin-right: 6px; padding: 0 2px; font: inherit; border-radius: 3px; }
  .changed { color: var(--bad); font-weight: 600; }
  .text { white-space: pre; }
  .names { display: flex; flex-wrap: wrap; gap: 4px 16px; padding: 4px 10px; font-size: 12px; }
</style>
