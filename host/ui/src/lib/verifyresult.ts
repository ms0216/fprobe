// 試す窓の結果の行（故障注入・規則・状態遷移）と値の短い表示。API の応答をそのまま行にするだけで、判定しない。
// Svelte に依らない。
import type { Reveal } from "./timeline/state";
import { display, kindOf } from "./vartree";
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
