// 試す窓（lib/verify.ts）: 故障注入の台本を欄から組む（inject.run の引数）・規則の検査の引数（rules.check）・フックの引数
// （hooks.add）と、応答から描く値（判定は API が返したものだけ。画面で判定しない）
import { describe, expect, it } from "vitest";
import {
  actionText, checkText, emptyAction, fieldErrors, emptyExpect, emptyScript, emptyTrial, hookAddCall, hookFields, injectCall, injectRows,
  pairs, ruleRows, rulesCall, rulesFields, stateRows, transitionRows, valueText, wordText, type ScriptForm, type TrialForm, lastRunText, readLastRun } from "../src/lib/verify";
import { vint } from "./value-kinds";

function trial(over: Partial<TrialForm> = {}): TrialForm {
  return { ...emptyTrial(), id: "t1", when: "host", delay: "0", address: "counter", writeMode: "flip", word: "0x1",
           timeout: "1", observe: "0.5", ...over };
}

function script(over: Partial<ScriptForm> = {}): ScriptForm {
  return { ...emptyScript(), name: "demo", trials: [trial()], ...over };
}

describe("故障注入の台本を欄から組む（inject.run の script）", () => {
  it("欄の値をそのまま台本に。0x は数に、空の省ける欄は送らない", () => {
    const r = injectCall("form", script(), "", "");
    expect(r).toEqual({ params: { script: { name: "demo", trials: [
      { id: "t1", when: { host: { delay_s: 0 } }, write: { address: "counter", flip: 1 }, timeout_s: 1, observe_s: 0.5 },
    ] } } });
  });

  it("timeout_s・observe_s は必須（既定を画面で埋めない）", () => {
    const r = injectCall("form", script({ trials: [trial({ timeout: "", observe: " " })] }), "", "");
    expect(r).toEqual({ errors: { "trials.0.timeout_s": "required", "trials.0.observe_s": "required" } });
  });

  it("id・when・番地・value か flip も必須", () => {
    const r = injectCall("form", script({ trials: [{ ...emptyTrial(), timeout: "1", observe: "1" }] }), "", "");
    expect("errors" in r && Object.keys(r.errors).sort()).toEqual(
      ["trials.0.id", "trials.0.when", "trials.0.write.address", "trials.0.write.mode", "trials.0.write.word"].sort());
  });

  it("数でない・負の秒は誤り", () => {
    const r = injectCall("form", script({ trials: [trial({ timeout: "abc", observe: "-1" })] }), "", "");
    expect(r).toEqual({ errors: { "trials.0.timeout_s": "number", "trials.0.observe_s": "min" } });
  });

  it("番地は数（0x）なら数、ほかは変数名の文字列", () => {
    const r = injectCall("form", script({ trials: [trial({ address: "0x20000010", writeMode: "value", word: "5" })] }), "", "");
    expect("params" in r && (r.params.script as any).trials[0].write).toEqual({ address: 0x20000010, value: 5 });
  });

  it("要件（ID: 文 の行）・試行の要件（カンマ区切り）・説明・repeat・ap", () => {
    const r = injectCall("form", script({ requirements: "R1: 止まらない\nR2: 戻る", trials: [
      trial({ requirement: "R1, R2", description: "d", repeat: "3", ap: "1" })] }), "", "");
    const s = "params" in r ? (r.params.script as any) : null;
    expect(s.requirements).toEqual({ R1: "止まらない", R2: "戻る" });
    expect(s.trials[0]).toMatchObject({ requirement: ["R1", "R2"], description: "d", repeat: 3, write: { ap: 1 } });
  });

  it("要件の行に : が無ければ誤り", () => {
    expect(injectCall("form", script({ requirements: "R1 no colon" }), "", "")).toEqual({ errors: { requirements: "requirementLine" } });
  });

  it("when の種類ごとの欄: pattern（空の欄は送らない）・dap_match・sequence（JSON）", () => {
    const p = injectCall("form", script({ trials: [trial({ when: "pattern", pattern: { mask: "0x3", value: "1", rise: "", fall: "" } })] }), "", "");
    expect("params" in p && (p.params.script as any).trials[0].when).toEqual({ pattern: { mask: 3, value: 1 } });
    const d = injectCall("form", script({ trials: [trial({ when: "dap_match",
      dapMatch: { address: "0x20000000", address_mask: "", data: "", data_mask: "", on_write: "true", on_read: "" } })] }), "", "");
    expect("params" in d && (d.params.script as any).trials[0].when).toEqual({ dap_match: { address: 0x20000000, on_write: true } });
    const s = injectCall("form", script({ trials: [trial({ when: "sequence", sequence: '{"stages":[{"when":["host"]}]}' })] }), "", "");
    expect("params" in s && (s.params.script as any).trials[0].when).toEqual({ sequence: { stages: [{ when: ["host"] }] } });
    const bad = injectCall("form", script({ trials: [trial({ when: "sequence", sequence: "{" })] }), "", "");
    expect(bad).toEqual({ errors: { "trials.0.when.sequence": "json" } });
    const none = injectCall("form", script({ trials: [trial({ when: "dap_match" })] }), "", "");
    expect(none).toEqual({ errors: { "trials.0.when.dap_match.address": "required" } });
  });

  it("expect: state・expression・memory・rules。比べ方と値は必須", () => {
    const r = injectCall("form", script({ trials: [trial({ expect: [
      { ...emptyExpect(), kind: "state", state: "running" },
      { ...emptyExpect(), kind: "expression", expression: "counter", op: "min", value: "0x10" },
      { ...emptyExpect(), kind: "memory", address: "0x20000000", size: "2", op: "not_equals", value: "0" },
      { ...emptyExpect(), kind: "rules", rules: '[{"id":"a","kind":"never","event":{"stream":"x"}}]' },
    ] })] }), "", "");
    expect("params" in r && (r.params.script as any).trials[0].expect).toEqual([
      { state: "running" }, { expression: "counter", min: 16 },
      { memory: { address: 0x20000000, size: 2 }, not_equals: 0 },
      { rules: [{ id: "a", kind: "never", event: { stream: "x" } }] },
    ]);
    const e = injectCall("form", script({ trials: [trial({ expect: [{ ...emptyExpect(), kind: "expression", expression: "x" }] })] }), "", "");
    expect(e).toEqual({ errors: { "trials.0.expect.0.op": "required", "trials.0.expect.0.value": "required" } });
  });

  it("restore: write（JSON の列）・reset・resume", () => {
    const r = injectCall("form", script({ trials: [trial({ restoreWrite: '[{"address":"counter","value":0}]', restoreReset: "system",
                                                           restoreResume: "true" })] }), "", "");
    expect("params" in r && (r.params.script as any).trials[0].restore).toEqual(
      { write: [{ address: "counter", value: 0 }], reset: "system", resume: true });
  });

  it("試行が無ければ誤り", () => {
    expect(injectCall("form", script({ trials: [] }), "", "")).toEqual({ errors: { trials: "required" } });
  });

  it("JSON の文から script、ファイルなら path（デーモンの PC の。JSON / YAML はデーモンが読む）", () => {
    expect(injectCall("json", emptyScript(), '{"name":"x","trials":[]}', "")).toEqual({ params: { script: { name: "x", trials: [] } } });
    expect(injectCall("json", emptyScript(), "[1]", "")).toEqual({ errors: { text: "object" } });
    expect(injectCall("json", emptyScript(), "{", "")).toEqual({ errors: { text: "json" } });
    expect(injectCall("path", emptyScript(), "", " a.yaml ")).toEqual({ params: { path: "a.yaml" } });
    expect(injectCall("path", emptyScript(), "", "")).toEqual({ errors: { path: "required" } });
  });
});

describe("故障注入の結果を描く（API の判定をそのまま）", () => {
  const result = {
    name: "demo", verdict: "fail", counts: { pass: 1, fail: 1 }, trials: [
      { id: "t1", repeat: 0, requirement: ["R1"], injected: true, t: 1000, address: 0x20000000, value: 3, verdict: "pass",
        checks: [{ check: "state", ok: true, value: "running", expected: "running" }] },
      { id: "t1", repeat: 1, requirement: [], injected: false, t: null, verdict: "not_triggered" },
      { id: "t2", repeat: 0, injected: true, t: 2000, address: 0x20000004, value: 1, verdict: "fail", previous: 0,
        checks: [{ check: "rules", ok: false, value: { verdict: "fail" } }, { check: "expression x", ok: false, error: "E" }],
        restore_error: "R" },
    ],
  };

  it("行は試行 × 繰り返し。時刻は注入した試行だけ（時間軸へ飛ぶ）", () => {
    const rows = injectRows(result);
    expect(rows.map((r) => [r.id, r.repeat, r.verdict])).toEqual([["t1", 0, "pass"], ["t1", 1, "not_triggered"], ["t2", 0, "fail"]]);
    expect(rows[0].to).toEqual({ source: "recording", t: 1000 });
    expect(rows[1].to).toBeNull();
    expect(rows[2].restoreError).toBe("R");
  });

  it("判定の文は API の値そのまま（ok・value・expected・error）", () => {
    const rows = injectRows(result);
    expect(checkText(rows[0].checks[0])).toBe("state: ok=true value=running expected=running");
    expect(checkText(rows[2].checks[0])).toBe("rules: ok=false verdict=fail");
    expect(checkText(rows[2].checks[1])).toBe("expression x: ok=false error=E");
  });

  it("規則・判定の値は大きさで書き方を変えない（120000 µs・70000 回を番地とみなして 16 進にしない）", () => {
    expect([valueText(120000), valueText(70000), valueText(5), valueText("x"), valueText(null)])
      .toEqual(["120000", "70000", "5", "x", "null"]);
    expect(checkText({ check: "memory 0x20000000", ok: true, value: 120000, expected: 70000 } as any))
      .toBe("memory 0x20000000: ok=true value=120000 expected=70000");
    expect(checkText({ check: "expression x", ok: true, value: vint(120000), expected: { min: 70000 } } as any))
      .toBe('expression x: ok=true value=120000 (0x1D4C0) expected={"min":70000}');      // 式の値は kind で表す
  });

  it("番地・語はいつも 16 進", () => {
    expect([wordText(43), wordText(0xdead), wordText(0)]).toEqual(["0x2B", "0xDEAD", "0x0"]);
  });

  it("応答に trials が無ければ空", () => {
    expect(injectRows(null)).toEqual([]);
    expect(injectRows({})).toEqual([]);
  });
});

describe("規則の検査（rules.check）", () => {
  it("欄は仕様の引数から rules・path を除いたもの", () => {
    const names = rulesFields().map((f) => f.name);
    expect(names).toContain("baseline");
    expect(names).toContain("mark");
    expect(names).not.toContain("rules");
    expect(names).not.toContain("path");
  });

  it("JSON の文なら rules、ファイルなら path。空の欄は送らない", () => {
    expect(rulesCall("json", '{"rules":[]}', "", { t0: "", mark: "false" })).toEqual({ params: { rules: { rules: [] }, mark: false } });
    expect(rulesCall("path", "", "r.yaml", { limit: "10" })).toEqual({ params: { path: "r.yaml", limit: 10 } });
    expect(rulesCall("json", "", "", {})).toEqual({ errors: { text: "required" } });
    expect(rulesCall("json", '{"rules":[]}', "", { limit: "x" })).toEqual({ errors: { limit: "integer" } });
  });

  it("規則の行: 期待値と実測は API の名前と値の組のまま、違反の時刻から時間軸へ", () => {
    const rows = ruleRows({ rules: [
      { id: "p", kind: "period", requirement: ["R1"], severity: "error", verdict: "fail", expected: { period_us: 1000 },
        measured: { count: 3, min_us: 900 }, violations_total: 1, violations_limit: 100,
        violations: [{ t: 50, t_end: 60, at_us: 1.5, kind: "too_short", message: "m", value: 900 }],
        baseline: { measured: {}, delta: { min_us: -10 } } },
    ] });
    expect(rows[0].expected).toEqual([["period_us", "1000"]]);
    expect(rows[0].measured).toEqual([["count", "3"], ["min_us", "900"]]);
    expect(rows[0].delta).toEqual([["min_us", "-10"]]);
    expect(rows[0].violations[0].to).toEqual({ source: "recording", t: 50, tEnd: 60 });
    expect(rows[0].total).toBe(1);
    expect(rows[0].limit).toBe(100);                       // 切った上限は API が返した値（表示した件数ではない）
  });

  it("組の値: 数は大きさによらず 10 進、配列・辞書は JSON", () => {
    expect(pairs({ a: 0x20000000, b: [1, 2], c: "s", d: null })).toEqual([["a", "536870912"], ["b", "[1,2]"], ["c", "s"], ["d", "null"]]);
    expect(pairs(undefined)).toEqual([]);
  });

  it("状態遷移（rules.states）の行: 状態と遷移、最初の時刻から時間軸へ", () => {
    const r = { states: { A: { entries: 2, first_t: 5, dwell_us: { min: 1, max: 2, mean: 1.5, total: 3 } },
                          B: { entries: 1, first_t: 9, dwell_us: null } },
                transitions: [{ from: "A", to: "B", count: 1, first_t: 9 }] };
    expect(stateRows(r)).toEqual([
      { name: "A", entries: 2, to: { source: "recording", t: 5 }, dwell: { min: 1, max: 2, mean: 1.5, total: 3 } },
      { name: "B", entries: 1, to: { source: "recording", t: 9 }, dwell: null }]);
    expect(transitionRows(r)).toEqual([{ from: "A", to: "B", count: 1, at: { source: "recording", t: 9 } }]);
  });
});

describe("フック（hooks.add）", () => {
  it("欄は仕様の引数から actions を除いたもの（event・phase は必須）", () => {
    const f = hookFields();
    expect(f.map((x) => x.name)).not.toContain("actions");
    expect(f.filter((x) => x.required).map((x) => x.name).sort()).toEqual(["event", "phase"]);
  });

  it("動作の行から actions。call の params は JSON の辞書", () => {
    const r = hookAddCall({ event: "halt", phase: "after", name: "n" }, [
      { kind: "evaluate", text: "counter", params: "" }, { kind: "mark", text: "stop {pc}", params: "" },
      { kind: "call", text: "recording.status", params: "" }, { kind: "call", text: "memory.read", params: '{"address":1}' }]);
    expect(r).toEqual({ params: { event: "halt", phase: "after", name: "n", actions: [
      { evaluate: "counter" }, { mark: "stop {pc}" }, { call: "recording.status" }, { call: "memory.read", params: { address: 1 } }] } });
  });

  it("動作は 1 つ以上。種類・文は必須。params は辞書", () => {
    expect(hookAddCall({ event: "halt", phase: "after" }, [])).toEqual({ errors: { actions: "required" } });
    expect(hookAddCall({ event: "", phase: "after" }, [emptyAction()])).toEqual(
      { errors: { event: "required", "actions.0.kind": "required", "actions.0.text": "required" } });
    expect(hookAddCall({ event: "halt", phase: "after" }, [{ kind: "call", text: "x", params: "[1]" }])).toEqual(
      { errors: { "actions.0.params": "object" } });
  });

  it("欄の誤りを ApiFields の形に: その欄の上限・下限を添え、欄に無い道筋は除く", () => {
    expect(fieldErrors(rulesFields(), { limit: "max", text: "json", t0: "integer" })).toEqual(
      { limit: { code: "max", limit: 100000 }, t0: { code: "integer" } });
  });

  it("動作の文（一覧・記録の表示）", () => {
    expect(actionText({ evaluate: "counter" })).toBe("evaluate counter");
    expect(actionText({ call: "memory.read", params: { address: 1 } })).toBe('call memory.read {"address":1}');
    expect(actionText({ mark: "m" })).toBe("mark m");
  });
});

describe("最後の実行（rules.last。だれが走らせても画面と AI が同じものを見る）", () => {
  const t = (k: string, p: Record<string, string | number> = {}) => `${k} ${p.when} ${p.who}`;
  it("片方の結果と、いつ・だれ", async () => {
    const run = { result: { verdict: "pass" }, at: 0, caller: { id: 1, name: "claude", role: "ai" } };
    const call = async () => ({ rules: run, inject: null });
    expect(await readLastRun(call, "rules")).toEqual(run);
    expect(await readLastRun(call, "inject")).toBeNull();
    expect(lastRunText(t as never, run)).toContain("claude (ai)");
    expect(lastRunText(t as never, { ...run, at: null, caller: null })).toBe("verify.lastRun — —");
    expect(lastRunText(t as never, null)).toBe("");
  });
});
