<script lang="ts">
  // 試す › 故障注入（inject.run）: 台本を欄で組む・JSON の文・デーモンの PC のファイル（JSON / YAML）で渡して走らせ、
  // 試行ごとの判定（API の verdict・checks のまま）・注入の時刻（時間軸へ飛ぶ）を表に描く。timeout_s・observe_s は必須で
  // 既定を画面に持たない。送る引数は「引数」で見える（AI が送るものと同じ）
  import { untrack } from "svelte";
  import { shared } from "../../lib/context";
  import { onCalls } from "../../lib/notify";
  import { lastRunText, readLastRun, type LastRun } from "../../lib/verify";
  import { allowed } from "../../lib/guard";
  import { i18n, type Key } from "../../lib/i18n";
  import {
    COMPARE, EXPECT_KINDS, WHEN_KINDS, checkText, emptyExpect, emptyScript, emptyTrial, injectCall, injectRows, wordText,
    type Errors, type ScriptForm, type ScriptSource,
  } from "../../lib/verify";
  import TimeLink from "./TimeLink.svelte";

  const { app, conn } = shared();
  // 最後の inject.run（だれが走らせても。rules.last）を開いたとき・だれかが走らせたときに読み直す
  let lastRun = $state<LastRun | null>(null);
  async function readLast() {
    try {
      lastRun = await readLastRun((m, p, o) => conn.call(m as never, p as never, o), "inject");
      if (lastRun) result = lastRun.result;
    } catch {
      /* つながっていない: つながってからの通知で読む */
    }
  }
  $effect(() => {
    if ($app.link !== "open") return;
    untrack(() => void readLast());
    return onCalls(conn, ["inject.run"], () => void readLast());
  });

  let source = $state<ScriptSource>("form");
  let form = $state<ScriptForm>({ ...emptyScript(), trials: [emptyTrial()] });
  let text = $state("");
  let path = $state("");
  let errors = $state<Errors>({});
  let showArgs = $state(false);
  let running = $state(false);
  let result = $state<any>(null);

  const built = $derived(injectCall(source, form, text, path));
  const rows = $derived(injectRows(result));

  async function run() {
    const call = injectCall(source, form, text, path);
    if ("errors" in call) {
      errors = call.errors;
      return;
    }
    errors = {};
    if (!(await allowed(app))) return;
    running = true;
    try {
      const r = await app.act("inject.run", call.params as any);
      if (r !== undefined) result = r;
    } finally {
      running = false;
    }
  }

  /** 欄で組んだ台本を JSON の文へ（そのまま直して走らせる・保存する） */
  function toJson() {
    const call = injectCall("form", form, "", "");
    if ("errors" in call) {
      errors = call.errors;
      return;
    }
    text = JSON.stringify(call.params.script, null, 2);
    source = "json";
  }

  const err = (p: string) => errors[p];
  const VERDICT_CLASS: Record<string, string> = { pass: "ok", fail: "bad" };
  const vclass = (v: string) => VERDICT_CLASS[v] ?? "other";
</script>

{#snippet e(p: string)}
  {#if err(p)}<span class="err" role="alert">{$i18n(`verify.err.${err(p)}` as Key)}</span>{/if}
{/snippet}

{#snippet req(label: string)}<span class="mono">{label}<span class="ast" aria-hidden="true">*</span></span>{/snippet}

<section class="vsec">
  <div class="bar">
    <label>{$i18n("verify.scriptFrom")}
      <select bind:value={source}>
        <option value="form">{$i18n("verify.fromForm")}</option>
        <option value="json">{$i18n("verify.fromJson")}</option>
        <option value="path">{$i18n("verify.fromPath")}</option>
      </select>
    </label>
    {#if source === "form"}<button onclick={toJson} title={$i18n("verify.toJsonTip")}>{$i18n("verify.toJson")}</button>{/if}
    <label class="small"><input type="checkbox" bind:checked={showArgs} /> {$i18n("verify.showArgs")}</label>
    <span class="spacer"></span>
    <button class="primary" disabled={running} onclick={() => void run()} title="inject.run">
      {running ? $i18n("verify.running") : $i18n("verify.run")}</button>
  </div>
  <div class="muted small">{$i18n("verify.injectSummary")}</div>

  {#if source === "json"}
    <textarea class="mono" rows="10" bind:value={text} placeholder={'{"name": "...", "trials": [...]}'}
              aria-invalid={!!err("text")}></textarea>
    {@render e("text")}
  {:else if source === "path"}
    <label>{@render req("path")} <input class="mono wide" bind:value={path} aria-required="true"
                                         title={$i18n("verify.pathTip")} /></label>
    {@render e("path")}
    <div class="muted small">{$i18n("verify.pathTip")}</div>
  {:else}
    <div class="row">
      <label><span class="mono">name</span> <input bind:value={form.name} size="18" /></label>
      <label class="grow"><span class="mono">requirements</span>
        <textarea rows="2" bind:value={form.requirements} placeholder={$i18n("verify.requirementsHint")}></textarea></label>
      {@render e("requirements")}
    </div>
    {@render e("trials")}
    {#each form.trials as t, n (n)}
      {@const at = `trials.${n}`}
      <div class="card">
        <div class="row">
          <strong>{$i18n("verify.trial", { n: n + 1 })}</strong>
          <label>{@render req("id")} <input bind:value={t.id} size="10" aria-required="true" aria-invalid={!!err(`${at}.id`)} /></label>
          {@render e(`${at}.id`)}
          <label><span class="mono">requirement</span> <input bind:value={t.requirement} size="10" placeholder="R1, R2" /></label>
          <label><span class="mono">description</span> <input bind:value={t.description} size="18" /></label>
          <label><span class="mono">repeat</span> <input class="mono" bind:value={t.repeat} size="5" /></label>
          {@render e(`${at}.repeat`)}
          <span class="spacer"></span>
          <button class="small" onclick={() => form.trials.splice(n, 1)}>{$i18n("common.remove")}</button>
        </div>
        <div class="row">
          <label>{@render req("when")}
            <select bind:value={t.when} aria-required="true" aria-invalid={!!err(`${at}.when`)}>
              <option value=""></option>
              {#each WHEN_KINDS as k (k)}<option value={k}>{k}</option>{/each}
            </select></label>
          {@render e(`${at}.when`)}
          {#if t.when === "host"}
            <label><span class="mono">delay_s</span> <input class="mono" bind:value={t.delay} size="6" /></label>
            {@render e(`${at}.when.host.delay_s`)}
          {:else if t.when === "pattern"}
            {#each ["mask", "value", "rise", "fall"] as const as k (k)}
              <label><span class="mono">{k}</span> <input class="mono" bind:value={t.pattern[k]} size="7" /></label>
              {@render e(`${at}.when.pattern.${k}`)}
            {/each}
          {:else if t.when === "dap_match"}
            {#each ["address", "address_mask", "data", "data_mask"] as const as k (k)}
              <label>{#if k === "address"}{@render req(k)}{:else}<span class="mono">{k}</span>{/if}
                <input class="mono" bind:value={t.dapMatch[k]} size="11" /></label>
              {@render e(`${at}.when.dap_match.${k}`)}
            {/each}
            {#each ["on_write", "on_read"] as const as k (k)}
              <label><span class="mono">{k}</span>
                <select bind:value={t.dapMatch[k]}><option value=""></option><option>true</option><option>false</option></select></label>
            {/each}
          {:else if t.when === "sequence"}
            <label class="grow">{@render req("sequence")}
              <input class="mono wide" bind:value={t.sequence} placeholder={'{"stages": [{"when": ["pattern_a"]}], "pattern_a": {...}}'} /></label>
            {@render e(`${at}.when.sequence`)}
          {/if}
        </div>
        <div class="row">
          <span class="muted small">write</span>
          <label>{@render req("address")} <input class="mono" bind:value={t.address} size="12" title={$i18n("verify.addressTip")}
                                                 aria-invalid={!!err(`${at}.write.address`)} /></label>
          {@render e(`${at}.write.address`)}
          <label>{@render req("value | flip")}
            <select bind:value={t.writeMode} aria-invalid={!!err(`${at}.write.mode`)}>
              <option value=""></option><option value="value">value</option><option value="flip">flip</option>
            </select></label>
          {@render e(`${at}.write.mode`)}
          <input class="mono" bind:value={t.word} size="11" aria-label="word" aria-required="true" aria-invalid={!!err(`${at}.write.word`)} />
          {@render e(`${at}.write.word`)}
          <label><span class="mono">ap</span> <input class="mono" bind:value={t.ap} size="4" /></label>
          {@render e(`${at}.write.ap`)}
        </div>
        <div class="row">
          <label title={$i18n("verify.timeoutTip")}>{@render req("timeout_s")}
            <input class="mono" bind:value={t.timeout} size="6" aria-required="true" aria-invalid={!!err(`${at}.timeout_s`)} /></label>
          {@render e(`${at}.timeout_s`)}
          <label title={$i18n("verify.observeTip")}>{@render req("observe_s")}
            <input class="mono" bind:value={t.observe} size="6" aria-required="true" aria-invalid={!!err(`${at}.observe_s`)} /></label>
          {@render e(`${at}.observe_s`)}
        </div>
        <div class="sub">
          <div class="row"><span class="muted small">expect</span>
            <button class="small" onclick={() => t.expect.push(emptyExpect())}>{$i18n("common.add")}</button></div>
          {#each t.expect as x, k (k)}
            {@const ex = `${at}.expect.${k}`}
            <div class="row">
              <select bind:value={x.kind} aria-label="expect" aria-invalid={!!err(`${ex}.kind`)}>
                <option value=""></option>{#each EXPECT_KINDS as kind (kind)}<option value={kind}>{kind}</option>{/each}
              </select>
              {@render e(`${ex}.kind`)}
              {#if x.kind === "state"}
                <select bind:value={x.state} aria-label="state"><option value=""></option><option>running</option><option>halted</option></select>
                {@render e(`${ex}.state`)}
              {:else if x.kind === "rules"}
                <input class="mono wide" bind:value={x.rules} placeholder={'[{"id": "...", "kind": "never", "event": {...}}]'} aria-label="rules" />
                {@render e(`${ex}.rules`)}
              {:else if x.kind === "expression" || x.kind === "memory"}
                {#if x.kind === "expression"}
                  <input class="mono" bind:value={x.expression} size="16" aria-label="expression" placeholder="expression" />
                  {@render e(`${ex}.expression`)}
                {:else}
                  <input class="mono" bind:value={x.address} size="12" aria-label="address" placeholder="address" />
                  {@render e(`${ex}.address`)}
                  <select bind:value={x.size} aria-label="size"><option value="">size</option><option>1</option><option>2</option><option>4</option></select>
                {/if}
                <select bind:value={x.op} aria-label="compare"><option value=""></option>{#each COMPARE as c (c)}<option value={c}>{c}</option>{/each}</select>
                {@render e(`${ex}.op`)}
                <input class="mono" bind:value={x.value} size="10" aria-label="value" />
                {@render e(`${ex}.value`)}
              {/if}
              <button class="small" onclick={() => t.expect.splice(k, 1)}>{$i18n("common.remove")}</button>
            </div>
          {/each}
        </div>
        <div class="row">
          <span class="muted small">restore</span>
          <label><span class="mono">write</span> <input class="mono" bind:value={t.restoreWrite} size="26"
                                                         placeholder={'[{"address": ..., "value": ...}]'} /></label>
          {@render e(`${at}.restore.write`)}
          <label><span class="mono">reset</span> <input class="mono" bind:value={t.restoreReset} size="8" /></label>
          <label><span class="mono">resume</span>
            <select bind:value={t.restoreResume}><option value=""></option><option>true</option><option>false</option></select></label>
        </div>
      </div>
    {/each}
    <div class="bar"><button onclick={() => form.trials.push(emptyTrial())}>{$i18n("verify.addTrial")}</button></div>
  {/if}

  {#if showArgs}
    <pre class="args mono">{"params" in built ? JSON.stringify(built.params, null, 2) : $i18n("verify.argsInvalid")}</pre>
  {/if}
</section>

{#if result}
  <section class="vsec">
    <h3>{$i18n("verify.result")}</h3>
    <div class="facts">
      {#if lastRun}<span class="small muted">{lastRunText($i18n, lastRun)}</span>{/if}
      <span class="mono">{result.name}</span>
      <span>verdict <b class="verdict {vclass(result.verdict)}">{result.verdict}</b></span>
      {#each Object.entries(result.counts ?? {}) as [k, v] (k)}<span class="mono small">{k}={v}</span>{/each}
      {#if result.sha256}<span class="mono small muted" title="sha256">{String(result.sha256).slice(0, 12)}…</span>{/if}
    </div>
    <div class="scroll">
      <table class="grid">
        <thead><tr><th>id</th><th>repeat</th><th>requirement</th><th>verdict</th><th>{$i18n("verify.injectedAt")}</th>
          <th>write</th><th>checks</th><th>{$i18n("verify.errorCol")}</th></tr></thead>
        <tbody>
          {#each rows as r (r.key)}
            <tr>
              <td class="mono nw">{r.id}</td>
              <td class="mono">{r.repeat}</td>
              <td class="mono small">{r.requirement.join(", ")}</td>
              <td><b class="verdict {vclass(r.verdict)}">{r.verdict}</b></td>
              <td>{#if r.to}<TimeLink to={r.to} />{:else}<span class="muted">—</span>{/if}</td>
              <td class="mono small nw">{#if r.address !== undefined}{wordText(r.address)} ← {wordText(r.value)}{#if r.previous !== undefined}
                <div class="muted">{$i18n("verify.previous", { v: wordText(r.previous) })}</div>{/if}{/if}</td>
              <td class="mono small">{#each r.checks as c, i (i)}<div class:bad={!c.ok}>{checkText(c)}</div>{/each}
                {#if r.note}<div class="muted">{r.note}</div>{/if}</td>
              <td class="small">{#if r.error}<div class="bad">{r.error}</div>{/if}{#if r.restoreError}<div class="bad">restore: {r.restoreError}</div>{/if}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </section>
{/if}
