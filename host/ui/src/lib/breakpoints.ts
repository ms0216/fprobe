// Breakpoints のパネルの計算（Svelte に依らない）: ブレークポイントの動作（VS Code の Edit Condition / Hit Count /
// Log Message、Ozone の Breakpoint Properties）の編集と debug.breakpoint.set の引数、ログ・ブレークポイントの記録
// （debug.breakpoint.log）の読み続けと表示、ベクタキャッチ（target.vector_catch / .get）の名前。
import type { DaemonConnection } from "./api";
import type { Mark } from "./timeline/state";

/** debug.breakpoint.list の action（fprobe.probe_breakpoints._Action.describe） */
export interface BreakpointAction {
  condition: string | null;
  ignore_count: number;
  log: string[] | null;
  message: string | null;
  hits: number;
  last_error: string | null;
}

/** 編集の欄（文字のまま。VS Code の 3 つの欄と同じ） */
export interface ActionEdit {
  condition: string;          // C の式。空 = 条件なし
  hitCount: string;           // 最初の n 回は止まらない（ignore_count）。空 = 0
  logMessage: string;         // "milestone {value} at {counter}"。{式} の値を残し、止まらない。空 = ログなし
}

const BRACES = /\{([^{}]+)\}/g;

/** Log Message の文 → 読む式の一覧と、添える文（式の場所は {式} のまま。表示で値に置き換える）。空なら null */
export function parseLogMessage(text: string): { log: string[]; message: string } | null {
  const message = text.trim();
  if (!message) return null;
  const log = [...new Set([...message.matchAll(BRACES)].map((m) => m[1].trim()).filter(Boolean))];
  return { log, message };
}

/** いまの動作 → 編集の欄。message に無い式（AI が log だけで置いた等）は {式} として後ろに足す */
export function editOf(action: BreakpointAction | null | undefined): ActionEdit {
  if (!action) return { condition: "", hitCount: "", logMessage: "" };
  let logMessage = "";
  if (action.log !== null) {
    const message = action.message ?? "";
    const inMessage = new Set([...message.matchAll(BRACES)].map((m) => m[1].trim()));
    const missing = action.log.filter((e) => !inMessage.has(e)).map((e) => `{${e}}`);
    logMessage = [message, ...missing].filter(Boolean).join(" ");
  }
  return { condition: action.condition ?? "", hitCount: action.ignore_count ? String(action.ignore_count) : "",
           logMessage };
}

/** 編集の欄 → debug.breakpoint.set の引数（同じ場所に置き直すと動作が替わる。全部空なら動作なし）。
 *  Hit Count が 0 以上の整数でなければ投げる */
export function setParams(location: string, edit: ActionEdit) {
  const count = edit.hitCount.trim();
  if (count && !/^\d+$/.test(count)) throw new RangeError(count);
  const log = parseLogMessage(edit.logMessage);
  return { location, condition: edit.condition.trim() || null, ignore_count: count ? Number(count) : 0,
           log: log?.log ?? null, message: log?.message ?? null };
}

/** 式の値の表示（数・列挙・文字の配列・構造体） */
export function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "?";
  if (typeof v === "number") return String(v);
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    if ("error" in o) return `<${o.error}>`;
    if ("string" in o) return JSON.stringify(o.string);
    if ("name" in o && "value" in o) return `${o.name} (${o.value})`;
    if ("pointer" in o && typeof o.pointer === "number") return `0x${(o.pointer >>> 0).toString(16)}`;
    return JSON.stringify(v);
  }
  return String(v);
}

/** debug.breakpoint.log の 1 件 */
export interface LogEntry {
  seq: number;
  host_s: number;
  address: number;
  where: string;
  function?: string;
  file?: string;
  line?: number;
  hit: number;
  message: string | null;
  values: Record<string, unknown>;
  t?: number;                 // 記録中なら記録のタイムベースの時刻
}

/** ログの 1 件の文: message の {式} を値に置き換える。message が無ければ "式 = 値, …" */
export function formatLog(e: LogEntry): string {
  if (e.message) {
    const shown = e.message.replace(BRACES, (m, expr: string) => (expr.trim() in e.values ? formatValue(e.values[expr.trim()]) : m));
    const rest = Object.keys(e.values).filter((k) => !e.message!.includes(`{${k}}`));
    return rest.length ? `${shown} ${rest.map((k) => `${k} = ${formatValue(e.values[k])}`).join(", ")}` : shown;
  }
  return Object.entries(e.values).map(([k, v]) => `${k} = ${formatValue(v)}`).join(", ");
}

/** 時間軸の印（記録の時刻のある項だけ） */
export function logMarks(entries: LogEntry[]): Mark[] {
  return entries.filter((e) => e.t !== undefined).map((e) => ({ t: e.t!, label: formatLog(e) }));
}

const KEEP = 2000;            // 手元に持つログの件数

type Call = Pick<DaemonConnection, "call">["call"];

/** ログ・ブレークポイントの記録を読み続ける（since で続きだけ）。デーモンが起動し直して番号が戻ったら読み直す。
 *  Svelte に依らない store（subscribe）。poll() を周期で呼ぶのは使う側 */
export class BreakpointLog {
  private entries: LogEntry[] = [];
  private since = 0;
  private listeners = new Set<(e: LogEntry[]) => void>();

  constructor(private call: Call) {}

  subscribe(fn: (e: LogEntry[]) => void): () => void {
    this.listeners.add(fn);
    fn(this.entries);
    return () => this.listeners.delete(fn);
  }

  async poll(): Promise<void> {
    let page: { entries: LogEntry[]; next: number };
    try {
      page = await this.call("debug.breakpoint.log", { since: this.since, limit: 500 }, { quiet: true });
    } catch {
      return;                                           // 未接続・古いデーモン: そのまま
    }
    if (page.next < this.since) {                       // 起動し直した: 番号が戻った
      this.since = 0;
      this.entries = [];
      return this.poll();
    }
    if (!page.entries.length) return;
    this.since = page.next;
    this.entries = [...this.entries, ...page.entries].slice(-KEEP);
    this.listeners.forEach((fn) => fn(this.entries));
  }

  /** 画面の表示だけを消す（デーモンの記録は残る。続きから読む） */
  clear(): void {
    this.entries = [];
    this.listeners.forEach((fn) => fn(this.entries));
  }
}

/** ベクタキャッチの名前（target.vector_catch）と、IAR・J-Link の表示の名前（DEMCR のビット VC_*） */
export const VECTOR_CATCH: { name: string; label: string }[] = [
  { name: "core_reset", label: "CORERESET" },
  { name: "hard_fault", label: "HARDERR" },
  { name: "mem_manage", label: "MMERR" },
  { name: "bus_fault", label: "BUSERR" },
  { name: "state_error", label: "STATERR" },
  { name: "check_error", label: "CHKERR" },
  { name: "no_coprocessor", label: "NOCPERR" },
  { name: "interrupt_error", label: "INTERR" },
  { name: "secure_fault", label: "SFERR" },
];

/** 故障で止めるベクタキャッチ（Fault Analyzer の「故障で止める」。コアに無いものは unsupported で返る） */
export const FAULT_CATCH = ["hard_fault", "mem_manage", "bus_fault", "state_error", "check_error", "no_coprocessor",
                            "interrupt_error", "secure_fault"];

/** ベクタキャッチの様子: 有効なもの、コアに無いもの（設定したときに分かる） */
export interface VectorCatchState {
  enabled: string[];
  unsupported: string[];
}

/** 1 つを切り替えた集合（target.vector_catch の catch） */
export function toggled(enabled: string[], name: string, on: boolean): string[] {
  const set = new Set(enabled);
  if (on) set.add(name);
  else set.delete(name);
  return VECTOR_CATCH.map((v) => v.name).filter((n) => set.has(n));
}

/** 結果を様子にまとめる（unsupported は覚えて足していく: 読むだけの結果には無く、設定した名前の分しか分からない） */
export function catchState(result: { enabled: string[]; unsupported?: string[] }, before?: VectorCatchState): VectorCatchState {
  const unsupported = new Set([...(before?.unsupported ?? []), ...(result.unsupported ?? [])]);
  return { enabled: result.enabled, unsupported: VECTOR_CATCH.map((v) => v.name).filter((n) => unsupported.has(n)) };
}

/** 操作の記録（activity）の 1 件 */
interface Activity {
  method: string;
  params?: unknown;
  error?: string;
}

const given = (params: unknown) => Object.values((params ?? {}) as Record<string, unknown>).some((v) => v !== null && v !== undefined);

/** ベクタ キャッチを変えた操作か（設定は target.vector_catch、読みは target.vector_catch.get で記録に残らない） */
export function changesCatch(e: Activity): boolean {
  return e.method === "target.vector_catch" && !e.error;
}

/** ブレークポイント（Flash の BKPT の様子）を変えた操作か（debug.breakpoint.flash は設定を渡したときだけ） */
export function changesBreakpoints(e: Activity): boolean {
  if (e.method === "debug.breakpoint.flash") return given(e.params);
  return ["debug.breakpoint.set", "debug.breakpoint.clear", "debug.breakpoint.flash_restore", "target.connect",
          "target.disconnect", "target.flash", "target.flash.erase"].includes(e.method);
}
