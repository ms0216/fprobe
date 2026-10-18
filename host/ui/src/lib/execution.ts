// 実行の設定（IAR の Step 中の割り込みの扱い・Ozone / J-Link のリセットの種類）: ステップ中に割り込みを止めるか
// （debug.step の mask_interrupts。DHCSR.C_MASKINTS）と、リセットの種類（target.reset の kind）。ブラウザに覚える。
// Svelte に依らない（store の約束 subscribe だけを満たす）。設定の窓（Settings）で変え、コマンドと Debugger が使う。
import { load, save } from "./prefs";

/** target.reset の kind: 既定（pyOCD の既定）/ system（AIRCR.SYSRESETREQ）/ core（AIRCR.VECTRESET。ARMv7-M だけ）/
 *  hardware（nRESET の線）/ emulated（デバッガがレジスタを初期値にする） */
export const RESET_KINDS = ["default", "system", "core", "hardware", "emulated"] as const;
export type ResetKind = (typeof RESET_KINDS)[number];

export interface ExecSettings {
  maskInterrupts: boolean;          // ステップ中は割り込みを止める（API の既定と同じ真）
  resetKind: ResetKind;
}

export type StepMode = "over" | "into" | "out" | "until";

/** debug.step の引数（割り込みを止めるのは API の既定なので、止めないときだけ渡す） */
export function stepParams(mode: StepMode, s: ExecSettings, location?: string):
  { mode: StepMode; location?: string; mask_interrupts?: boolean } {
  return { mode, ...(location ? { location } : {}), ...(s.maskInterrupts ? {} : { mask_interrupts: false }) };
}

/** target.reset の引数（リセットして止める。既定以外の種類は kind で） */
export function resetParams(s: ExecSettings): { halt: true; kind?: ResetKind } {
  return { halt: true, ...(s.resetKind !== "default" ? { kind: s.resetKind } : {}) };
}

const isKind = (v: unknown): v is ResetKind => RESET_KINDS.includes(v as ResetKind);

export class ExecutionSettings {
  private value: ExecSettings;
  private listeners = new Set<(s: ExecSettings) => void>();

  constructor() {
    const kind = load("exec.resetKind", "default");
    this.value = { maskInterrupts: load("exec.maskInterrupts", "1") !== "0", resetKind: isKind(kind) ? kind : "default" };
  }

  get(): ExecSettings {
    return this.value;
  }

  /** 変える（知らないリセットの種類は既定に戻す） */
  set(patch: Partial<ExecSettings>): void {
    const next = { ...this.value, ...patch };
    if (!isKind(next.resetKind)) next.resetKind = "default";
    this.value = next;
    save("exec.maskInterrupts", next.maskInterrupts ? "1" : "0");
    save("exec.resetKind", next.resetKind);
    this.listeners.forEach((fn) => fn(next));
  }

  subscribe(fn: (s: ExecSettings) => void): () => void {
    this.listeners.add(fn);
    fn(this.value);
    return () => this.listeners.delete(fn);
  }
}

/** 画面で一つの設定 */
export const execution = new ExecutionSettings();
