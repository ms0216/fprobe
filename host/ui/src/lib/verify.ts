// 試す窓の計算（docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §3.3 の作る順の 3 番目、
// docs/reports/2026-10-24-fprobe-value-analysis.md §5.4 の P7・P10）:
// - 故障注入（inject.run）: 台本を欄から組む（host/fprobe/inject.py の台本の形そのまま）。timeout_s・observe_s は必須で
//   既定を画面に持たない。空の省ける欄は送らない（既定はデーモンが決める）。JSON の文・デーモンの PC のファイル（JSON / YAML）
//   でも渡せる。結果は試行ごとに、API が返した判定（verdict・checks の ok）をそのまま描く
// - 規則（rules.check・rules.states）: 規則集は JSON の文かファイル、ほかの引数は API の仕様の欄。期待値と実測は API の
//   名前と値の組のまま
// - フック（hooks.add）: event・phase 等は API の仕様の欄、動作（actions）は行で組む
// 画面では判定しない（pass / fail を計算しない）。Svelte に依らない。
import type { Key } from "./i18n";
import { buildParams, type Field, type FieldError, type FieldValues } from "./apiform";
import { methodFields } from "./crosstrigger";
import type { Reveal } from "./timeline/state";
import { display, kindOf } from "./vartree";

/** 欄の誤り（欄の道筋 → 文言のキーの末尾。画面が verify.err.<code> にする） */
export type Errors = Record<string, string>;
export type Built<T> = { params: T } | { errors: Errors };

const INTEGER = /^(0x[0-9a-f]+|\d+)$/i;

/** 誤りを置く（同じ欄の最初の誤りだけ）。値の代わりに返せるよう undefined を返す */
function put(errors: Errors, path: string, code: string): undefined {
  if (!(path in errors)) errors[path] = code;
  return undefined;
}

/** 0 以上の整数（10 進・0x）。空なら undefined */
function uint(text: string, path: string, errors: Errors, required = false): number | undefined {
  const s = text.trim();
  if (!s) {
    if (required) put(errors, path, "required");
    return undefined;
  }
  if (!INTEGER.test(s) || !Number.isSafeInteger(Number(s))) {
    put(errors, path, "integer");
    return undefined;
  }
  return Number(s);
}

/** 0 以上の数（秒）。空なら undefined */
function seconds(text: string, path: string, errors: Errors, required = false): number | undefined {
  const s = text.trim();
  if (!s) {
    if (required) put(errors, path, "required");
    return undefined;
  }
  const v = Number(s);
  if (!Number.isFinite(v)) {
    put(errors, path, "number");
    return undefined;
  }
  if (v < 0) {
    put(errors, path, "min");
    return undefined;
  }
  return v;
}

/** 比べる値: 整数（0x も）・数・true / false */
function scalar(text: string, path: string, errors: Errors): unknown {
  const s = text.trim();
  if (!s) return put(errors, path, "required");
  if (/^-?(0x[0-9a-f]+)$/i.test(s)) return s.startsWith("-") ? -Number(s.slice(1)) : Number(s);
  if (s === "true" || s === "false") return s === "true";
  const v = Number(s);
  if (Number.isFinite(v)) return v;
  return put(errors, path, "number");
}

function json(text: string, path: string, errors: Errors, shape: "object" | "array" | "any" = "any"): unknown {
  const s = text.trim();
  if (!s) return put(errors, path, "required");
  let v: unknown;
  try {
    v = JSON.parse(s);
  } catch {
    return put(errors, path, "json");
  }
  const ok = shape === "any" || (shape === "array" ? Array.isArray(v)
    : typeof v === "object" && v !== null && !Array.isArray(v));
  return ok ? v : put(errors, path, shape);
}

/** 番地: 数（0x・10 進）なら数、ほかは ELF の変数名 */
function address(text: string, path: string, errors: Errors): number | string | undefined {
  const s = text.trim();
  if (!s) return put(errors, path, "required");
  return INTEGER.test(s) ? Number(s) : s;
}

const done = <T>(errors: Errors, params: T): Built<T> => (Object.keys(errors).length ? { errors } : { params });

// --- 故障注入の台本（inject.run） ------------------------------------------------------------------

export const WHEN_KINDS = ["pattern", "sequence", "dap_match", "host"] as const;
export type WhenKind = (typeof WHEN_KINDS)[number];
export const EXPECT_KINDS = ["state", "expression", "memory", "rules"] as const;
export type ExpectKind = (typeof EXPECT_KINDS)[number];
export const COMPARE = ["equals", "not_equals", "min", "max"] as const;
export type Compare = (typeof COMPARE)[number];

export interface ExpectForm {
  kind: ExpectKind | "";
  state: "running" | "halted" | "";
  expression: string;
  address: string;
  size: string;
  op: Compare | "";
  value: string;
  rules: string;                       // JSON の規則の列
}

export interface TrialForm {
  id: string;
  requirement: string;                 // カンマ区切り
  description: string;
  when: WhenKind | "";
  pattern: { mask: string; value: string; rise: string; fall: string };
  sequence: string;                    // JSON（xtrig.set_sequence と同じ stages・pattern_a・pattern_b）
  dapMatch: { address: string; address_mask: string; data: string; data_mask: string; on_write: string; on_read: string };
  delay: string;                       // host の delay_s
  address: string;
  writeMode: "value" | "flip" | "";
  word: string;
  ap: string;
  expect: ExpectForm[];
  restoreWrite: string;                // JSON の列 [{address, value, size}]
  restoreReset: string;
  restoreResume: "true" | "false" | "";
  timeout: string;
  observe: string;
  repeat: string;
}

export interface ScriptForm {
  name: string;
  requirements: string;                // 「ID: 文」の行
  trials: TrialForm[];
}

export const emptyExpect = (): ExpectForm =>
  ({ kind: "", state: "", expression: "", address: "", size: "", op: "", value: "", rules: "" });

export const emptyTrial = (): TrialForm => ({
  id: "", requirement: "", description: "", when: "", pattern: { mask: "", value: "", rise: "", fall: "" }, sequence: "",
  dapMatch: { address: "", address_mask: "", data: "", data_mask: "", on_write: "", on_read: "" }, delay: "", address: "",
  writeMode: "", word: "", ap: "", expect: [], restoreWrite: "", restoreReset: "", restoreResume: "", timeout: "", observe: "",
  repeat: "",
});

export const emptyScript = (): ScriptForm => ({ name: "", requirements: "", trials: [] });

function requirementsOf(text: string, errors: Errors): Record<string, string> | undefined {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return undefined;
  const out: Record<string, string> = {};
  for (const line of lines) {
    const i = line.indexOf(":");
    if (i <= 0) {
      put(errors, "requirements", "requirementLine");
      return undefined;
    }
    out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

function whenOf(t: TrialForm, at: string, errors: Errors): Record<string, unknown> | undefined {
  if (!t.when) return put(errors, `${at}.when`, "required");
  const p: Record<string, unknown> = {};
  if (t.when === "host") {
    const d = seconds(t.delay, `${at}.when.host.delay_s`, errors);
    if (d !== undefined) p.delay_s = d;
  } else if (t.when === "pattern") {
    for (const k of ["mask", "value", "rise", "fall"] as const) {
      const v = uint(t.pattern[k], `${at}.when.pattern.${k}`, errors);
      if (v !== undefined) p[k] = v;
    }
  } else if (t.when === "dap_match") {
    for (const k of ["address", "address_mask", "data", "data_mask"] as const) {
      const v = uint(t.dapMatch[k], `${at}.when.dap_match.${k}`, errors, k === "address");
      if (v !== undefined) p[k] = v;
    }
    for (const k of ["on_write", "on_read"] as const) if (t.dapMatch[k]) p[k] = t.dapMatch[k] === "true";
  } else {
    const v = json(t.sequence, `${at}.when.sequence`, errors, "object");
    if (v === undefined) return undefined;
    return { sequence: v };
  }
  return { [t.when]: p };
}

function expectOf(e: ExpectForm, at: string, errors: Errors): Record<string, unknown> | undefined {
  if (!e.kind) return put(errors, `${at}.kind`, "required");
  if (e.kind === "state") return e.state ? { state: e.state } : put(errors, `${at}.state`, "required");
  if (e.kind === "rules") {
    const rules = json(e.rules, `${at}.rules`, errors, "array");
    return rules === undefined ? undefined : { rules };
  }
  const out: Record<string, unknown> = {};
  if (e.kind === "expression") {
    if (!e.expression.trim()) put(errors, `${at}.expression`, "required");
    out.expression = e.expression.trim();
  } else {
    const mem: Record<string, unknown> = { address: address(e.address, `${at}.address`, errors) };
    const size = uint(e.size, `${at}.size`, errors);
    if (size !== undefined) mem.size = size;
    out.memory = mem;
  }
  if (!e.op) put(errors, `${at}.op`, "required");
  const v = scalar(e.value, `${at}.value`, errors);
  if (e.op) out[e.op] = v;
  return out;
}

function trialOf(t: TrialForm, at: string, errors: Errors): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (!t.id.trim()) put(errors, `${at}.id`, "required");
  out.id = t.id.trim();
  const reqs = t.requirement.split(",").map((s) => s.trim()).filter(Boolean);
  if (reqs.length) out.requirement = reqs;
  if (t.description.trim()) out.description = t.description.trim();
  out.when = whenOf(t, at, errors);
  const write: Record<string, unknown> = { address: address(t.address, `${at}.write.address`, errors) };
  if (!t.writeMode) put(errors, `${at}.write.mode`, "required");
  const word = uint(t.word, `${at}.write.word`, errors, true);
  if (t.writeMode) write[t.writeMode] = word;
  const ap = uint(t.ap, `${at}.write.ap`, errors);
  if (ap !== undefined) write.ap = ap;
  out.write = write;
  if (t.expect.length) out.expect = t.expect.map((e, n) => expectOf(e, `${at}.expect.${n}`, errors));
  const restore: Record<string, unknown> = {};
  if (t.restoreWrite.trim()) restore.write = json(t.restoreWrite, `${at}.restore.write`, errors, "array");
  if (t.restoreReset.trim()) restore.reset = t.restoreReset.trim();
  if (t.restoreResume) restore.resume = t.restoreResume === "true";
  if (Object.keys(restore).length) out.restore = restore;
  out.timeout_s = seconds(t.timeout, `${at}.timeout_s`, errors, true);
  out.observe_s = seconds(t.observe, `${at}.observe_s`, errors, true);
  const repeat = uint(t.repeat, `${at}.repeat`, errors);
  if (repeat !== undefined) out.repeat = repeat;
  return out;
}

/** 欄から台本（inject.run の script） */
export function scriptOf(form: ScriptForm): Built<Record<string, unknown>> {
  const errors: Errors = {};
  const out: Record<string, unknown> = {};
  if (form.name.trim()) out.name = form.name.trim();
  const reqs = requirementsOf(form.requirements, errors);
  if (reqs) out.requirements = reqs;
  if (!form.trials.length) put(errors, "trials", "required");
  out.trials = form.trials.map((t, n) => trialOf(t, `trials.${n}`, errors));
  return done(errors, out);
}

/** 台本の渡し方: 欄で組む・JSON の文・ファイル（デーモンの PC の path。JSON / YAML はデーモンが読む） */
export type ScriptSource = "form" | "json" | "path";

function fromText(source: "json" | "path", text: string, path: string, key: string): Built<Record<string, unknown>> {
  const errors: Errors = {};
  if (source === "path") return path.trim() ? { params: { path: path.trim() } } : { errors: { path: "required" } };
  const v = json(text, "text", errors, "object");
  return done(errors, { [key]: v });
}

/** inject.run の引数 */
export function injectCall(source: ScriptSource, form: ScriptForm, text: string, path: string): Built<Record<string, unknown>> {
  if (source !== "form") return fromText(source, text, path, "script");
  const s = scriptOf(form);
  return "errors" in s ? s : { params: { script: s.params } };
}

// --- 故障注入の結果（API の応答をそのまま） ---------------------------------------------------------

export interface Check { check: string; ok: boolean; value?: unknown; expected?: unknown; error?: string }

export interface InjectRow {
  key: string;
  id: string;
  repeat: number;
  requirement: string[];
  verdict: string;
  injected: boolean;
  to: Reveal | null;
  address?: number;
  value?: number;
  previous?: number;
  note?: string;
  error?: string;
  restoreError?: string;
  checks: Check[];
}

const recordingAt = (t: unknown, tEnd?: unknown): Reveal | null =>
  typeof t === "number" ? { source: "recording", t, ...(typeof tEnd === "number" ? { tEnd } : {}) } : null;

export function injectRows(result: any): InjectRow[] {
  const trials: any[] = Array.isArray(result?.trials) ? result.trials : [];
  return trials.map((x, i) => ({
    key: `${i}`, id: String(x.id), repeat: x.repeat ?? 0, requirement: x.requirement ?? [], verdict: String(x.verdict),
    injected: !!x.injected, to: x.injected ? recordingAt(x.t) : null, address: x.address, value: x.value,
    previous: x.previous, note: x.note, error: x.error, restoreError: x.restore_error, checks: x.checks ?? [],
  }));
}

/** 値の短い表示: 数は 10 進（大きさで番地かを推して書き方を変えない）。文字はそのまま、ほかは JSON */
export function valueText(v: unknown): string {
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** 番地・語（割り込み書き込みの番地・値・前の値）: いつも 16 進 */
export function wordText(v: unknown): string {
  return typeof v === "number" && Number.isInteger(v) && v >= 0 ? `0x${v.toString(16).toUpperCase()}` : valueText(v);
}

/** 判定の 1 つ（API の ok・value・expected・error のまま。規則の判定は API が返した verdict） */
export function checkText(c: Check): string {
  const parts = [`${c.check}: ok=${c.ok}`];
  if (c.error !== undefined) parts.push(`error=${c.error}`);
  else if (c.check === "rules") parts.push(`verdict=${(c.value as any)?.verdict}`);
  else {
    // 式の判定の値は fprobe.dwarf.values の形（kind を持つ）: 変数の木と同じ表し方。状態・メモリの語はそのまま
    parts.push(`value=${kindOf(c.value) === "none" ? valueText(c.value) : display(c.value, Infinity)}`);
    if (c.expected !== undefined) parts.push(`expected=${valueText(c.expected)}`);
  }
  return parts.join(" ");
}

// --- 規則（rules.check・rules.states） ---------------------------------------------------------------

/** rules.check の欄（規則集 = rules / path は別に渡すので除く） */
export function rulesFields(): Field[] {
  return methodFields("rules.check").filter((f) => f.name !== "rules" && f.name !== "path");
}

export type RulesSource = "json" | "path";

export function rulesCall(source: RulesSource, text: string, path: string, values: FieldValues): Built<Record<string, unknown>> {
  const set = fromText(source, text, path, "rules");
  const rest = buildParams(rulesFields(), values);
  if ("errors" in set || "errors" in rest) {
    const errors: Errors = { ...("errors" in set ? set.errors : {}) };
    if ("errors" in rest) for (const [k, e] of Object.entries(rest.errors)) errors[k] = e.code;
    return { errors };
  }
  return { params: { ...set.params, ...rest.params } };
}

/** 名前と値の組（API の辞書のまま。値は短い表示） */
export function pairs(obj: Record<string, unknown> | null | undefined): [string, string][] {
  return Object.entries(obj ?? {}).map(([k, v]) => [k, valueText(v)]);
}

export interface Violation { to: Reveal | null; atUs: number | null; kind: string; message: string; value: unknown }
export interface RuleRow {
  id: string; kind: string; requirement: string[]; severity: string; verdict: string;
  expected: [string, string][]; measured: [string, string][]; delta: [string, string][] | null;
  violations: Violation[]; total: number; limit: number | null;     // limit = API が切った上限（violations_limit）
}

export function ruleRows(result: any): RuleRow[] {
  const rules: any[] = Array.isArray(result?.rules) ? result.rules : [];
  return rules.map((r) => ({
    id: String(r.id), kind: String(r.kind), requirement: r.requirement ?? [], severity: String(r.severity), verdict: String(r.verdict),
    expected: pairs(r.expected), measured: pairs(r.measured), delta: r.baseline ? pairs(r.baseline.delta) : null,
    violations: (r.violations ?? []).map((v: any) => ({ to: recordingAt(v.t, v.t_end), atUs: v.at_us ?? null, kind: String(v.kind),
                                                        message: String(v.message ?? ""), value: v.value })),
    total: r.violations_total ?? (r.violations ?? []).length,
    limit: typeof r.violations_limit === "number" ? r.violations_limit : null,
  }));
}

export interface StateRow { name: string; entries: number; to: Reveal | null;
                            dwell: { min: number; max: number; mean: number; total: number } | null }

export function stateRows(result: any): StateRow[] {
  return Object.entries<any>(result?.states ?? {}).map(([name, s]) =>
    ({ name, entries: s.entries, to: recordingAt(s.first_t), dwell: s.dwell_us ?? null }));
}

export function transitionRows(result: any): { from: string; to: string; count: number; at: Reveal | null }[] {
  return (result?.transitions ?? []).map((x: any) => ({ from: String(x.from), to: String(x.to), count: x.count, at: recordingAt(x.first_t) }));
}

// --- フック（hooks.*） -------------------------------------------------------------------------------

export const ACTION_KINDS = ["evaluate", "mark", "call"] as const;
export type ActionKind = (typeof ACTION_KINDS)[number];
export interface ActionForm { kind: ActionKind | ""; text: string; params: string }
export const emptyAction = (): ActionForm => ({ kind: "", text: "", params: "" });

/** hooks.add の欄（動作 = actions は行で組むので除く） */
export function hookFields(): Field[] {
  return methodFields("hooks.add").filter((f) => f.name !== "actions");
}

export function hookAddCall(values: FieldValues, actions: ActionForm[]): Built<Record<string, unknown>> {
  const errors: Errors = {};
  const rest = buildParams(hookFields(), values);
  if ("errors" in rest) for (const [k, e] of Object.entries(rest.errors)) errors[k] = e.code;
  if (!actions.length) put(errors, "actions", "required");
  const list = actions.map((a, n) => {
    const at = `actions.${n}`;
    if (!a.kind) put(errors, `${at}.kind`, "required");
    if (!a.text.trim()) put(errors, `${at}.text`, "required");
    const out: Record<string, unknown> = { [a.kind || "evaluate"]: a.text.trim() };
    if (a.kind === "call" && a.params.trim()) out.params = json(a.params, `${at}.params`, errors, "object");
    return out;
  });
  return done(errors, { ...("params" in rest ? rest.params : {}), actions: list });
}

/** 欄の誤り（道筋 → 誤りの種類）を ApiFields の形に（その欄の上限・下限を添える） */
export function fieldErrors(fields: Field[], errors: Errors): Record<string, FieldError> {
  const out: Record<string, FieldError> = {};
  for (const f of fields) {
    const code = errors[f.name];
    if (!code) continue;
    const limit = code === "min" ? f.min : code === "max" ? f.max : code === "exclusiveMin" ? f.exclusiveMin : undefined;
    out[f.name] = { code: code as FieldError["code"], ...(limit !== undefined ? { limit } : {}) };
  }
  return out;
}

/** 動作の 1 つの文（一覧・記録） */
export function actionText(a: Record<string, unknown>): string {
  const kind = ACTION_KINDS.find((k) => k in a) ?? Object.keys(a)[0] ?? "";
  const params = kind === "call" && a.params !== undefined ? ` ${JSON.stringify(a.params)}` : "";
  return `${kind} ${String(a[kind])}${params}`;
}

/** rules.last の片方（rules・inject）: その呼び出しの応答と、いつ・だれが（デーモンが残した事実） */
export interface LastRun {
  result: any;
  at: number | null;                  // UNIX 秒
  caller: { id: number; name: string; role: string } | null;
}

export async function readLastRun(call: (m: string, p: object, o: { quiet: boolean }) => Promise<any>,
                                  kind: "rules" | "inject"): Promise<LastRun | null> {
  const last = await call("rules.last", {}, { quiet: true });
  return last?.[kind] ?? null;
}

/** 最後の実行の行（時刻はこの PC の時刻の書き方。だれかは呼び出し元の名前と役割） */
export function lastRunText(t: (k: Key, p?: Record<string, string | number>) => string, run: LastRun | null): string {
  if (!run) return "";
  const when = run.at === null ? "—" : new Date(run.at * 1000).toLocaleString();
  return t("verify.lastRun", { when, who: run.caller ? `${run.caller.name} (${run.caller.role})` : "—" });
}
