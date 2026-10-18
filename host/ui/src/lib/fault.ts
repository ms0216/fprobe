// Fault Analyzer（STM32CubeIDE）・Fault exception viewer（IAR）の計算（Svelte に依らない）: 止まった場所が故障の
// ハンドラかの判定、故障で止まったら開く見張り、target.fault の結果の整理（原因のレジスタ・積まれたレジスタ・文字の要約）。
import type { DaemonConnection } from "./api";

export interface FaultCause {
  bit: string;
  meaning: string;
  hint?: string;
}

export interface FaultPlace {
  address: number;
  where?: string;
}

export interface FaultFrame {
  stack: "MSP" | "PSP";
  sp: number;
  exc_return: number;
  fp_context: boolean;
  registers: Record<string, number>;     // r0〜r3・r12・lr・pc・xpsr（例外に入ったときに積まれた値）
  caller_sp: number;
  pc: FaultPlace;
  lr: FaultPlace;
}

/** target.fault の結果（fprobe.fault.analyze） */
export interface FaultReport {
  architecture: string;
  exception: { number: number; name: string } | null;
  causes: FaultCause[];
  faulted: boolean;
  halt_reasons?: FaultCause[];
  registers?: Record<string, number | null>;   // CFSR・HFSR・DFSR・MMFAR・BFAR・CPACR（ARMv7-M / v8-M Mainline）
  fault_address?: FaultPlace;
  frame?: FaultFrame | null;
}

/** 故障の例外（IPSR 3〜7） */
export const FAULT_EXCEPTIONS = new Set(["HardFault", "MemManage", "BusFault", "UsageFault", "SecureFault"]);

const norm = (name: string) => name.toLowerCase().replace(/[_\s]/g, "");
// CMSIS の名前（HardFault_Handler 等）、pico-sdk（isr_hardfault）、Zephyr・小文字の流儀（hard_fault_handler）
const FAULT_HANDLERS = ["hardfault", "memmanage", "memmanagefault", "busfault", "usagefault", "securefault"];
// 故障にも割り込みにも使われる共通のハンドラ（ベクタ表の空き。ここで止まったら target.fault で確かめる）
const SHARED_HANDLERS = ["defaulthandler", "defaultisr", "unhandledinterrupt", "unusedhandler", "dummyhandler",
                         "isrinvalid", "blockinghandler", "defaultirqhandler"];

/** 止まった関数の名前で: "fault" = 故障のハンドラ、"maybe" = 共通のハンドラ（故障かは target.fault で確かめる）、
 *  null = ふつうの関数 */
export function handlerKind(fn: string | undefined | null): "fault" | "maybe" | null {
  if (!fn) return null;
  const n = norm(fn).replace(/(irq)?handler$/, "").replace(/^isr/, "");
  if (FAULT_HANDLERS.includes(n)) return "fault";
  return SHARED_HANDLERS.includes(norm(fn)) ? "maybe" : null;
}

/** target.fault の結果が故障を示す（故障のビットが立っている、または故障の例外の中） */
export function isFault(report: FaultReport | null | undefined): boolean {
  return !!report && (report.faulted || (!!report.exception && FAULT_EXCEPTIONS.has(report.exception.name)));
}

type Call = Pick<DaemonConnection, "call">["call"];
interface TargetSource {
  subscribe(fn: (s: { target: Record<string, any> | null }) => void): () => void;
}

/** 故障で止まったら open() を呼ぶ（IAR の Fault exception viewer の自動で開く動き）。止まった関数が故障のハンドラなら
 *  すぐに、共通のハンドラなら target.fault（quiet）で確かめてから。同じ止まり（pc と実行の番号）では 1 回だけ。
 *  やめる関数を返す */
export function watchFaults(call: Call, app: TargetSource, open: () => void): () => void {
  let last: string | undefined;
  return app.subscribe(({ target }) => {
    if (target?.state !== "HALTED") {
      last = undefined;
      return;
    }
    const key = `${target.pc}/${target.run}`;
    if (key === last) return;
    last = key;
    const kind = handlerKind(target.function);
    if (kind === "fault") open();
    else if (kind === "maybe") {
      call("target.fault", {}, { quiet: true }).then((r: FaultReport) => {
        if (key === last && isFault(r)) open();
      }, () => {});
    }
  });
}

/** 原因のビットが属するレジスタ（HFSR・MMFSR・BFSR・UFSR。CFSR は 3 つに分かれる） */
export function causeRegister(bit: string): string {
  if (["VECTTBL", "FORCED", "DEBUGEVT"].includes(bit)) return "HFSR";
  if (["IACCVIOL", "DACCVIOL", "MUNSTKERR", "MSTKERR", "MLSPERR", "MMARVALID"].includes(bit)) return "MMFSR";
  if (["IBUSERR", "PRECISERR", "IMPRECISERR", "UNSTKERR", "STKERR", "LSPERR", "BFARVALID"].includes(bit)) return "BFSR";
  if (["UNDEFINSTR", "INVSTATE", "INVPC", "NOCP", "STKOF", "UNALIGNED", "DIVBYZERO"].includes(bit)) return "UFSR";
  return "";
}

/** 故障の番地が入っていることを示すだけのビット（MMARVALID・BFARVALID）は原因の一覧では控えめに出す */
export const VALID_BITS = new Set(["MMARVALID", "BFARVALID"]);

/** 積まれたレジスタの並び（例外のフレームの順） */
export const STACKED = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"] as const;

const hex = (v: number | null | undefined) => (v == null ? "-" : `0x${(v >>> 0).toString(16).toUpperCase().padStart(8, "0")}`);

/** 見出し: 例外の名前と主な原因（"HardFault: PRECISERR"）。故障でなければ null */
export function headline(report: FaultReport): string | null {
  if (!isFault(report)) return null;
  const main = report.causes.filter((c) => !VALID_BITS.has(c.bit) && c.bit !== "FORCED").map((c) => c.bit);
  const name = report.exception?.name ?? "Fault";
  return main.length ? `${name}: ${main.join(", ")}` : name;
}

/** 文字の要約（コピー用。IAR の「PC・LR をコピー」に加え、原因・番地・レジスタ。AI やチケットに貼る） */
export function reportText(report: FaultReport): string {
  const lines: string[] = [headline(report) ?? "No fault"];
  for (const c of report.causes) lines.push(`  ${c.bit}: ${c.meaning}${c.hint ? ` (${c.hint})` : ""}`);
  if (report.fault_address) lines.push(`Fault address: ${hex(report.fault_address.address)} ${report.fault_address.where ?? ""}`.trimEnd());
  const f = report.frame;
  if (f) {
    lines.push(`PC: ${hex(f.pc.address)} ${f.pc.where ?? ""}`.trimEnd());
    lines.push(`LR: ${hex(f.lr.address)} ${f.lr.where ?? ""}`.trimEnd());
    lines.push(`Stacked (${f.stack} ${hex(f.sp)}): ${STACKED.map((r) => `${r}=${hex(f.registers[r])}`).join(" ")}`);
  }
  if (report.registers) {
    lines.push(Object.entries(report.registers).filter(([, v]) => v != null).map(([k, v]) => `${k}=${hex(v)}`).join(" "));
  }
  return lines.join("\n");
}
