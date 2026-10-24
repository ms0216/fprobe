<script lang="ts">
  // 試す › 規則: 規則集（JSON の文かデーモンの PC のファイル）を開いている記録に当て（rules.check）、規則ごとの判定・期待値と
  // 実測（API の名前と値のまま）・違反の時刻（時間軸へ飛ぶ）を表に描く。状態遷移（rules.states）と証跡の書き出し
  // （rules.report: 最後の rules.check と inject.run）。ほかの引数は API の仕様の欄（ApiFields）
  import type { FieldError, FieldValues } from "../../lib/apiform";
  import { untrack } from "svelte";
  import { shared } from "../../lib/context";
  import { onCalls } from "../../lib/notify";
  import { lastRunText, readLastRun, type LastRun } from "../../lib/verify";
  import { methodFields } from "../../lib/crosstrigger";
  import { i18n, type Key } from "../../lib/i18n";
  import {
    fieldErrors, ruleRows, rulesCall, rulesFields, stateRows, transitionRows, valueText, type Errors, type RulesSource,
  } from "../../lib/verify";
  import { buildParams } from "../../lib/apiform";
  import ApiFields from "./ApiFields.svelte";
  import TimeLink from "./TimeLink.svelte";

  const { app, conn } = shared();
  // 最後の rules.check（だれが走らせても。rules.last）を開いたとき・だれかが走らせたときに読み直す
  let lastRun = $state<LastRun | null>(null);
  async function readLast() {
    try {
      lastRun = await readLastRun((m, p, o) => conn.call(m as never, p as never, o), "rules");
      if (lastRun) result = lastRun.result;
    } catch {
      /* つながっていない: つながってからの通知で読む */
    }
  }
  $effect(() => {
    if ($app.link !== "open") return;
    untrack(() => void readLast());
    return onCalls(conn, ["rules.check"], () => void readLast());
  });
  const blank = (fields: { name: string }[]): FieldValues => Object.fromEntries(fields.map((f) => [f.name, ""]));

  // --- rules.check ---
  const checkFields = rulesFields();
  let source = $state<RulesSource>("json");
  let text = $state("");
  let path = $state("");
  let values = $state<FieldValues>(blank(checkFields));
  let errors = $state<Errors>({});
  let result = $state<any>(null);
  const rows = $derived(ruleRows(result));

  async function check() {
    const call = rulesCall(source, text, path, values);
    if ("errors" in call) {
      errors = call.errors;
      return;
    }
    errors = {};
    const r = await app.act("rules.check", call.params as any);
    if (r !== undefined) result = r;
  }

  // --- rules.states・rules.report（仕様の欄のまま） ---
  type Plain = "rules.states" | "rules.report";
  const plainFields = { "rules.states": methodFields("rules.states"), "rules.report": methodFields("rules.report") };
  let plainValues = $state<Record<Plain, FieldValues>>({ "rules.states": blank(plainFields["rules.states"]),
                                                          "rules.report": blank(plainFields["rules.report"]) });
  let plainErrors = $state<Record<Plain, Record<string, FieldError>>>({ "rules.states": {}, "rules.report": {} });
  let states = $state<any>(null);
  let report = $state<any>(null);
  const missing = (m: Plain) => plainFields[m].some((f) => f.required && !(plainValues[m][f.name] ?? "").trim());

  async function plain(m: Plain) {
    const built = buildParams(plainFields[m], plainValues[m]);
    if ("errors" in built) {
      plainErrors[m] = built.errors;
      return;
    }
    plainErrors[m] = {};
    const r = await app.act(m, built.params as any);
    if (r === undefined) return;
    if (m === "rules.states") states = r;
    else report = r;
  }

  const VERDICT_CLASS: Record<string, string> = { pass: "ok", fail: "bad" };
  const vclass = (v: string) => VERDICT_CLASS[v] ?? "other";
  const pairText = (p: [string, string][]) => p.map(([k, v]) => `${k}=${v}`).join(" ");
</script>

<section class="vsec">
  <h3>{$i18n("verify.rulesCheck")} <span class="mono small muted">rules.check</span></h3>
  <div class="muted small">{$i18n("verify.rulesSummary")}</div>
  <div class="bar">
    <label>{$i18n("verify.rulesFrom")}
      <select bind:value={source}>
        <option value="json">{$i18n("verify.fromJson")}</option>
        <option value="path">{$i18n("verify.fromPath")}</option>
      </select>
    </label>
  </div>
  {#if source === "json"}
    <textarea class="mono" rows="7" bind:value={text} aria-required="true" aria-invalid={!!errors.text}
              placeholder={'{"name": "...", "requirements": {"R1": "..."}, "rules": [{"id": "...", "kind": "period", ...}]}'}></textarea>
    {#if errors.text}<span class="err" role="alert">{$i18n(`verify.err.${errors.text}` as Key)}</span>{/if}
  {:else}
    <label><span class="mono">path<span class="ast" aria-hidden="true">*</span></span>
      <input class="mono wide" bind:value={path} aria-required="true" title={$i18n("verify.pathTip")} /></label>
    {#if errors.path}<span class="err" role="alert">{$i18n(`verify.err.${errors.path}` as Key)}</span>{/if}
  {/if}
  <form class="bar" onsubmit={(e) => { e.preventDefault(); void check(); }}>
    <ApiFields fields={checkFields} bind:values errors={fieldErrors(checkFields, errors)} />
    <button type="submit" class="primary">{$i18n("verify.check")}</button>
  </form>

  {#if result}
    <div class="facts">
      {#if lastRun}<span class="small muted">{lastRunText($i18n, lastRun)}</span>{/if}
      <span>verdict <b class="verdict {vclass(result.verdict)}">{result.verdict}</b></span>
      {#each Object.entries(result.counts ?? {}) as [k, v] (k)}<span class="mono small">{k}={v}</span>{/each}
      {#if result.ruleset}<span class="mono small">{result.ruleset.name}{#if result.ruleset.source} · {result.ruleset.source}{/if}</span>
        <span class="mono small muted" title="sha256">{String(result.ruleset.sha256 ?? "").slice(0, 12)}…</span>{/if}
      {#if result.recording}<span class="small">{$i18n("verify.recording")}: <span class="mono">{result.recording.name}</span></span>{/if}
      {#if result.baseline}<span class="small">{$i18n("verify.baseline")}: <span class="mono">{result.baseline.name}</span></span>{/if}
      {#if result.marked !== undefined}<span class="small">{$i18n("verify.marked", { n: valueText(result.marked) })}</span>{/if}
    </div>
    <div class="scroll">
      <table class="grid">
        <thead><tr><th>id</th><th>kind</th><th>requirement</th><th>severity</th><th>verdict</th>
          <th>{$i18n("verify.expected")}</th><th>{$i18n("verify.measured")}</th>
          {#if result.baseline}<th>{$i18n("verify.delta")}</th>{/if}<th>{$i18n("verify.violations")}</th></tr></thead>
        <tbody>
          {#each rows as r (r.id)}
            <tr>
              <td class="mono">{r.id}</td>
              <td class="mono">{r.kind}</td>
              <td class="mono small">{r.requirement.join(", ")}</td>
              <td class="mono small">{r.severity}</td>
              <td><b class="verdict {vclass(r.verdict)}">{r.verdict}</b></td>
              <td class="mono small">{pairText(r.expected)}</td>
              <td class="mono small">{pairText(r.measured)}</td>
              {#if result.baseline}<td class="mono small">{r.delta ? pairText(r.delta) : ""}</td>{/if}
              <td class="small">
                {#if r.total}
                  <details>
                    <summary>{r.violations.length < r.total ? $i18n("common.truncated", { shown: r.violations.length, total: r.total,
                                                                                          limit: r.limit ?? "—" })
                                                            : r.total}</summary>
                    <table class="grid inner">
                      <tbody>
                        {#each r.violations as v, i (i)}
                          <tr><td><TimeLink to={v.to} /></td><td class="mono">{v.kind}</td><td>{v.message}</td>
                            <td class="mono">{v.value === null || v.value === undefined ? "" : valueText(v.value)}</td></tr>
                        {/each}
                      </tbody>
                    </table>
                  </details>
                {:else}<span class="mono">0</span>{/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>

<section class="vsec">
  <h3>{$i18n("verify.states")} <span class="mono small muted">rules.states</span></h3>
  <form class="bar" onsubmit={(e) => { e.preventDefault(); void plain("rules.states"); }}>
    <ApiFields fields={plainFields["rules.states"]} bind:values={plainValues["rules.states"]} errors={plainErrors["rules.states"]} />
    <button type="submit" disabled={missing("rules.states")} title={missing("rules.states") ? $i18n("xt.fillRequired") : ""}>
      {$i18n("verify.statesRun")}</button>
  </form>
  {#if states}
    {@const sr = stateRows(states)}
    {@const tr = transitionRows(states)}
    <div class="facts small"><span>initial <span class="mono">{states.initial ?? "—"}</span></span><span>changes <span class="mono">{states.changes}</span></span></div>
    <div class="cols">
      <table class="grid">
        <thead><tr><th>{$i18n("verify.state")}</th><th>entries</th><th>{$i18n("verify.firstAt")}</th><th>dwell_us min / mean / max / total</th></tr></thead>
        <tbody>{#each sr as s (s.name)}<tr><td class="mono">{s.name}</td><td class="mono">{s.entries}</td><td><TimeLink to={s.to} /></td>
          <td class="mono small">{s.dwell ? `${s.dwell.min} / ${s.dwell.mean} / ${s.dwell.max} / ${s.dwell.total}` : "—"}</td></tr>{/each}</tbody>
      </table>
      <table class="grid">
        <thead><tr><th>from</th><th>to</th><th>count</th><th>{$i18n("verify.firstAt")}</th></tr></thead>
        <tbody>{#each tr as x, i (i)}<tr><td class="mono">{x.from}</td><td class="mono">{x.to}</td><td class="mono">{x.count}</td>
          <td><TimeLink to={x.at} /></td></tr>{/each}</tbody>
      </table>
    </div>
    <details><summary class="small">dot</summary><pre class="args mono">{states.dot}</pre></details>
  {/if}
</section>

<section class="vsec">
  <h3>{$i18n("verify.report")} <span class="mono small muted">rules.report</span></h3>
  <div class="muted small">{$i18n("verify.reportSummary")}</div>
  <form class="bar" onsubmit={(e) => { e.preventDefault(); void plain("rules.report"); }}>
    <ApiFields fields={plainFields["rules.report"]} bind:values={plainValues["rules.report"]} errors={plainErrors["rules.report"]} />
    <button type="submit" disabled={missing("rules.report")} title={missing("rules.report") ? $i18n("xt.fillRequired") : ""}>
      {$i18n("verify.reportRun")}</button>
  </form>
  {#if report}
    <div class="facts">
      <span class="mono small">{report.path}</span>
      <span>verdict <b class="verdict {vclass(report.verdict)}">{report.verdict}</b></span>
      {#each Object.entries(report.requirements ?? {}) as [k, v] (k)}<span class="mono small">{k}: <span class="verdict {vclass(String(v))}">{v}</span></span>{/each}
    </div>
  {/if}
</section>
