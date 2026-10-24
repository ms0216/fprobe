<script lang="ts">
  // 試す › フック: デーモンに登録したフックの一覧（hooks.list。AI と人が同じものを見る）・足す（hooks.add）・外す
  // （hooks.remove）・実行の記録（hooks.log。通知 hook で読み直す）・保存と読み込み（hooks.save・hooks.load）。
  // 引数は API の仕様の欄（ApiFields）、動作（actions）は行で組む
  import { errorText } from "../../lib/api";
  import { buildParams, type FieldError, type FieldValues } from "../../lib/apiform";
  import { shared } from "../../lib/context";
  import { methodFields } from "../../lib/crosstrigger";
  import { allowed } from "../../lib/guard";
  import { i18n, type Key } from "../../lib/i18n";
  import {
    ACTION_KINDS, actionText, emptyAction, fieldErrors, hookAddCall, hookFields, valueText, type ActionForm, type Errors,
  } from "../../lib/verify";
  import ApiFields from "./ApiFields.svelte";

  let { active }: { active: boolean } = $props();
  const POLL_MS = 2000;
  const { app, conn } = shared();
  const linked = $derived($app.link === "open");
  const blank = (fields: { name: string }[]): FieldValues => Object.fromEntries(fields.map((f) => [f.name, ""]));

  // --- 読む（見えている間 2 秒ごと・通知 hook のとき） ---
  let hooks = $state<any[] | null>(null);
  let log = $state<any[] | null>(null);
  let readError = $state<string | null>(null);
  const logFields = methodFields("hooks.log");
  let logValues = $state<FieldValues>(blank(logFields));
  let logErrors = $state<Record<string, FieldError>>({});

  async function read() {
    const built = buildParams(logFields, logValues);
    logErrors = "errors" in built ? built.errors : {};
    try {
      const [l, g] = await Promise.all([
        conn.call("hooks.list", {}, { quiet: true }),
        conn.call("hooks.log", ("params" in built ? built.params : {}) as any, { quiet: true }),
      ]);
      hooks = l?.hooks ?? [];
      log = g?.entries ?? [];
      readError = null;
    } catch (e) {
      readError = errorText(e);
    }
  }

  $effect(() => {
    if (!linked || !active) return;
    void read();
    const timer = setInterval(() => void read(), POLL_MS);
    const off = conn.on("hook", () => void read());
    return () => { clearInterval(timer); off(); };
  });

  // --- 足す ---
  const addFields = hookFields();
  let addValues = $state<FieldValues>(blank(addFields));
  let actions = $state<ActionForm[]>([emptyAction()]);
  let addErrors = $state<Errors>({});

  async function add() {
    const call = hookAddCall(addValues, actions);
    if ("errors" in call) {
      addErrors = call.errors;
      return;
    }
    addErrors = {};
    if (!(await allowed(app))) return;
    if ((await app.act("hooks.add", call.params as any)) !== undefined) {
      addValues = blank(addFields);
      actions = [emptyAction()];
    }
    await read();
  }

  async function remove(params: { id: number } | { all: true }) {
    if (!(await allowed(app))) return;
    await app.act("hooks.remove", params as any);
    await read();
  }

  // --- 保存・読み込み（仕様の欄のまま） ---
  type FileMethod = "hooks.save" | "hooks.load";
  const fileFields = { "hooks.save": methodFields("hooks.save"), "hooks.load": methodFields("hooks.load") };
  let fileValues = $state<Record<FileMethod, FieldValues>>({ "hooks.save": blank(fileFields["hooks.save"]),
                                                             "hooks.load": blank(fileFields["hooks.load"]) });
  let fileErrors = $state<Record<FileMethod, Record<string, FieldError>>>({ "hooks.save": {}, "hooks.load": {} });
  let fileResult = $state<Record<FileMethod, any>>({ "hooks.save": null, "hooks.load": null });
  const missing = (m: FileMethod) => fileFields[m].some((f) => f.required && !(fileValues[m][f.name] ?? "").trim());

  async function file(m: FileMethod) {
    const built = buildParams(fileFields[m], fileValues[m]);
    if ("errors" in built) {
      fileErrors[m] = built.errors;
      return;
    }
    fileErrors[m] = {};
    if (m === "hooks.load" && !(await allowed(app))) return;
    const r = await app.act(m, built.params as any);
    if (r !== undefined) fileResult[m] = r;
    await read();
  }

  const clock = (s: number) => new Date(s * 1000).toLocaleTimeString();
  const err = (p: string) => addErrors[p];
</script>

{#snippet e(p: string)}
  {#if err(p)}<span class="err" role="alert">{$i18n(`verify.err.${err(p)}` as Key)}</span>{/if}
{/snippet}

<section class="vsec">
  <h3>{$i18n("verify.hooks")} <span class="mono small muted">hooks.list</span>
    <span class="spacer"></span><button class="small" onclick={() => void read()}>{$i18n("common.refresh")}</button></h3>
  <div class="muted small">{$i18n("verify.hooksSummary")}</div>
  {#if readError}<div class="err">{readError}</div>{/if}
  {#if hooks && !hooks.length}<div class="muted">{$i18n("verify.noHooks")}</div>{/if}
  {#if hooks?.length}
    <div class="scroll">
      <table class="grid">
        <thead><tr><th>id</th><th>event</th><th>phase</th><th>name</th><th>required</th><th>enabled</th><th>actions</th><th></th></tr></thead>
        <tbody>
          {#each hooks as h (h.id)}
            <tr class:off={!h.enabled}>
              <td class="mono">{h.id}</td><td class="mono">{h.event}</td><td class="mono">{h.phase}</td><td>{h.name}</td>
              <td class="mono">{String(h.required)}</td><td class="mono">{String(h.enabled)}</td>
              <td class="mono small">{#each h.actions as a, i (i)}<div>{actionText(a)}</div>{/each}</td>
              <td><button class="small" onclick={() => void remove({ id: h.id })}>{$i18n("common.remove")}</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <div class="bar"><button class="small" onclick={() => void remove({ all: true })} title="hooks.remove all=true">{$i18n("verify.removeAll")}</button></div>
  {/if}
</section>

<section class="vsec">
  <h3>{$i18n("verify.addHook")} <span class="mono small muted">hooks.add</span></h3>
  <div class="bar"><ApiFields fields={addFields} bind:values={addValues} errors={fieldErrors(addFields, addErrors)} /></div>
  <div class="row"><span class="mono">actions<span class="ast" aria-hidden="true">*</span></span>
    <button class="small" onclick={() => actions.push(emptyAction())}>{$i18n("common.add")}</button>{@render e("actions")}</div>
  {#each actions as a, n (n)}
    <div class="row">
      <select bind:value={a.kind} aria-label="action" aria-invalid={!!err(`actions.${n}.kind`)}>
        <option value=""></option>{#each ACTION_KINDS as k (k)}<option value={k}>{k}</option>{/each}
      </select>
      {@render e(`actions.${n}.kind`)}
      <input class="mono" bind:value={a.text} size="28" aria-label="text" aria-invalid={!!err(`actions.${n}.text`)}
             placeholder={a.kind === "call" ? "method" : a.kind === "mark" ? "{event} {phase} {pc} {where}" : a.kind === "evaluate" ? "expression" : ""} />
      {@render e(`actions.${n}.text`)}
      {#if a.kind === "call"}
        <input class="mono" bind:value={a.params} size="24" aria-label="params" placeholder={'params {"...": ...}'} />
        {@render e(`actions.${n}.params`)}
      {/if}
      <button class="small" onclick={() => actions.splice(n, 1)}>{$i18n("common.remove")}</button>
    </div>
  {/each}
  <div class="bar"><button class="primary" onclick={() => void add()}>{$i18n("verify.addHookRun")}</button></div>
</section>

<section class="vsec">
  <h3>{$i18n("verify.hookLog")} <span class="mono small muted">hooks.log</span></h3>
  <div class="bar"><ApiFields fields={logFields} bind:values={logValues} errors={logErrors} /></div>
  {#if log && !log.length}<div class="muted">{$i18n("verify.noHookLog")}</div>{/if}
  {#if log?.length}
    <div class="scroll">
      <table class="grid">
        <thead><tr><th>seq</th><th>{$i18n("verify.clock")}</th><th>hook</th><th>event</th><th>method</th><th>client</th>
          <th>results</th><th>seconds</th></tr></thead>
        <tbody>
          {#each [...log].reverse() as x (x.seq)}
            <tr>
              <td class="mono">{x.seq}</td><td class="mono small">{clock(x.time)}</td>
              <td class="mono nw">{x.hook}{#if x.name}<span class="muted gap">{x.name}</span>{/if}</td>
              <td class="mono">{x.event} {x.phase}</td><td class="mono small">{x.method ?? "—"}</td><td class="small">{x.client}</td>
              <td class="mono small">{#each x.results ?? [] as r, i (i)}<div class:bad={!r.ok}>{r.action} {r.target}: ok={r.ok}
                {r.ok ? valueText(r.value) : `error=${r.error}`}</div>{/each}</td>
              <td class="mono small">{x.seconds}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>

<section class="vsec">
  <h3>{$i18n("verify.hookFiles")}</h3>
  {#each ["hooks.save", "hooks.load"] as const as m (m)}
    <form class="bar" onsubmit={(ev) => { ev.preventDefault(); void file(m); }}>
      <span class="mono small muted">{m}</span>
      <ApiFields fields={fileFields[m]} bind:values={fileValues[m]} errors={fileErrors[m]} />
      <button type="submit" disabled={missing(m)} title={missing(m) ? $i18n("xt.fillRequired") : m}>
        {$i18n(m === "hooks.save" ? "verify.save" : "verify.load")}</button>
      {#if fileResult[m]}<span class="mono small">{JSON.stringify(m === "hooks.save" ? fileResult[m] : { loaded: fileResult[m].loaded })}</span>{/if}
    </form>
  {/each}
</section>
