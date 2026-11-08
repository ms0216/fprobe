// JTAG で全コアを止めて Linux のアプリをデバッグする（API の linux.halt.*。CLI python -m fprobe.linux_halt と一対一）の欄 → 引数。
// 既定の値を持たない: 要る欄が空なら、その欄の名前を返して送らない。書いた欄だけを送る。値の判断（上限の良し悪し等）はしない。
// 止めずに読む口の JTAG（linux.memap.connect_jtag）の欄もここ。Svelte に依らない

import type { Methods, Params } from "./api";
import { exampleOf } from "./apispec";

// 組み立てた引数（メソッド M の引数の型）か、空・誤りの欄の名前。欄を 1 つずつ確かめて詰める（required が必ず送る欄を
// 全部確かめた後だけ params を返す）ので、詰め終えた表を M の引数の型として返す
// error は先頭の 1 件（呼び出しの互換）、errors は空・誤りの欄の全部（画面が全部まとめて教える）
type Built<M extends keyof Methods> = { params: Params<M> } | { error: string; errors: string[] };
const fail = (errors: string[]): { error: string; errors: string[] } => ({ error: errors[0], errors });

export const UNMEASURED = "unmeasured";

export interface HaltForm {
  backend: "" | "fprobe" | "openocd";               // "" = まだ選んでいない
  jtagKhz: string;
  offsets: string;
  pageOffset: string;
  physOffset: string;
  linearMapEnd: string;
  maxTasks: string;
  maxThreads: string;
  mmuFormat: "" | "short" | "lpae";
  journal: string;
  fpgaRequestTimes: "" | "true" | "false";          // 使う人が選ぶ（チェックの外れを false として送らない）
  part: string;
  watchBoundNs: string;                              // ns か unmeasured
  watchPollS: string;
  ctiBoard: "" | "zynq7000";
  ctiRequestBoundNs: string;
  ctiEventBoundNs: string;                           // ns か unmeasured
  ctiHwWaitS: string;
  ctiAckPolls: string;
  ocdBoard: string;
  ocdPort: string;
  ocdTimeoutS: string;
  hwSlots: string;
  watchSlots: string;
}

export const EMPTY_HALT_FORM: HaltForm = {
  backend: "", jtagKhz: "", offsets: "", pageOffset: "", physOffset: "", linearMapEnd: "", maxTasks: "", maxThreads: "",
  mmuFormat: "", journal: "", fpgaRequestTimes: "", part: "", watchBoundNs: "", watchPollS: "", ctiBoard: "",
  ctiRequestBoundNs: "", ctiEventBoundNs: "", ctiHwWaitS: "", ctiAckPolls: "", ocdBoard: "", ocdPort: "", ocdTimeoutS: "",
  hwSlots: "", watchSlots: "",
};

/** 仕様の「例・推奨の値」で欄を埋めた最初の値（無い欄は空）。欄に見えて、書き換えられ、送るときは欄の値を明示して送る */
const hexOr = (v: unknown, fallback: string): string => (typeof v === "number" && Number.isInteger(v) && v >= 0
  ? "0x" + v.toString(16).toUpperCase() : fallback);

const eg = (method: string, param: string): string => {
  const v = exampleOf(method, param);
  return v === undefined ? "" : String(v);
};

export function exampleHaltForm(): HaltForm {
  const o = (p: string) => eg("linux.halt.open", p);
  return { ...EMPTY_HALT_FORM, backend: o("backend") as HaltForm["backend"], jtagKhz: o("jtag_khz"),
    pageOffset: hexOr(exampleOf("linux.halt.open", "page_offset"), o("page_offset")),
    physOffset: hexOr(exampleOf("linux.halt.open", "phys_offset"), o("phys_offset")),
    linearMapEnd: hexOr(exampleOf("linux.halt.open", "linear_map_end"), o("linear_map_end")), maxTasks: o("max_tasks"), maxThreads: o("max_threads"),
    mmuFormat: o("mmu_format") as HaltForm["mmuFormat"], fpgaRequestTimes: o("fpga_request_times") as HaltForm["fpgaRequestTimes"],
    part: o("part") };
}

export interface GdbServeForm {
  pid: string;
  port: string;
  skewLimitNs: string;
  threadId: "" | "stack" | "contextidr";
  kernelIsa: "" | "arm" | "thumb";
  maxStepRetries: string;
  bpUnmapped: "" | "error" | "hw";
  log: string;
}

export const EMPTY_GDB_FORM: GdbServeForm = { pid: "", port: "", skewLimitNs: "", threadId: "", kernelIsa: "",
  maxStepRetries: "", bpUnmapped: "", log: "" };

export interface MemapJtagForm {
  chainIdcode: string;
  ap: string;
  frequencyHz: string;
}

/** 10 進か 0x の 16 進の lo 以上 hi 以下の整数（それ以外・空は null） */
function int(s: string, lo: number, hi: number): number | null {
  const t = s.trim();
  if (!/^(0x[0-9a-fA-F]+|\d+)$/.test(t)) return null;
  const v = Number(t);
  return v >= lo && v <= hi ? v : null;
}

/** 0 以上（strict なら正）の有限の数（それ以外・空は null） */
function num(s: string, strict = false): number | null {
  const t = s.trim();
  const v = Number(t);
  return t !== "" && Number.isFinite(v) && (strict ? v > 0 : v >= 0) ? v : null;
}

/** ns の整数か unmeasured */
function bound(s: string): number | string | null {
  return s.trim() === UNMEASURED ? UNMEASURED : int(s, 0, Number.MAX_SAFE_INTEGER);
}

const U32 = 0xFFFFFFFF;

/** 必ず送る欄（空・誤りはその欄の名前。全部を返す） */
function requiredAll(out: Record<string, unknown>, fields: [string, unknown][]): string[] {
  const bad: string[] = [];
  for (const [k, v] of fields) {
    if (v === null || v === "") bad.push(k);
    else out[k] = v;
  }
  return bad;
}

/** 書いたときだけ送る欄（書いて誤りならその欄の名前。全部を返す） */
function optionalAll(out: Record<string, unknown>, fields: [string, string, (s: string) => unknown][]): string[] {
  const bad: string[] = [];
  for (const [k, text, parse] of fields) {
    if (!text.trim()) continue;
    const v = parse(text);
    if (v === null) bad.push(k);
    else out[k] = v;
  }
  return bad;
}

/** 最初の 1 件だけ返す版（欄の少ない口。memap・gdb・threads） */
function required(out: Record<string, unknown>, fields: [string, unknown][]): string | null {
  return requiredAll(out, fields)[0] ?? null;
}
function optional(out: Record<string, unknown>, fields: [string, string, (s: string) => unknown][]): string | null {
  return optionalAll(out, fields)[0] ?? null;
}

/** linux.halt.open の引数。組（CTI・見張り・OpenOCD）の欠けはデーモンが CLI と同じ文で断る（画面で同じ判断を二重に持たない） */
export function haltOpenParams(f: HaltForm): Built<"linux.halt.open"> {
  const out: Record<string, unknown> = {};
  const bad = requiredAll(out, [
    ["backend", f.backend || null], ["jtag_khz", int(f.jtagKhz, 1, 100000)], ["offsets", f.offsets.trim() || null],
    ["page_offset", int(f.pageOffset, 0, U32)], ["phys_offset", int(f.physOffset, 0, U32)],
    ["linear_map_end", int(f.linearMapEnd, 0, U32)], ["max_tasks", int(f.maxTasks, 1, Number.MAX_SAFE_INTEGER)],
    ["max_threads", int(f.maxThreads, 1, Number.MAX_SAFE_INTEGER)], ["mmu_format", f.mmuFormat || null],
    ["journal", f.journal.trim() || null],
    ["fpga_request_times", f.fpgaRequestTimes === "" ? null : f.fpgaRequestTimes === "true"],
    ]).concat(optionalAll(out, [
    ["part", f.part, (s) => s.trim()],
    ["fpga_halt_watch_bound_ns", f.watchBoundNs, bound], ["fpga_halt_watch_poll_s", f.watchPollS, (s) => num(s)],
    ["cti_board", f.ctiBoard, (s) => s],
    ["cti_request_bound_ns", f.ctiRequestBoundNs, (s) => int(s, 0, Number.MAX_SAFE_INTEGER)],
    ["cti_event_bound_ns", f.ctiEventBoundNs, bound], ["cti_hw_wait_s", f.ctiHwWaitS, (s) => num(s)],
    ["cti_ack_polls", f.ctiAckPolls, (s) => int(s, 1, Number.MAX_SAFE_INTEGER)],
    ["ocd_board", f.ocdBoard, (s) => s.trim()], ["ocd_port", f.ocdPort, (s) => int(s, 1, 65535)],
    ["ocd_timeout_s", f.ocdTimeoutS, (s) => num(s, true)], ["hw_slots", f.hwSlots, (s) => int(s, 1, Number.MAX_SAFE_INTEGER)],
    ["watch_slots", f.watchSlots, (s) => int(s, 0, Number.MAX_SAFE_INTEGER)],
  ]));
  return bad.length ? fail(bad) : { params: out as Params<"linux.halt.open"> };
}

/** linux.halt.gdb.serve の引数（log は書いたときだけ） */
export function haltGdbServeParams(f: GdbServeForm): Built<"linux.halt.gdb.serve"> {
  const out: Record<string, unknown> = {};
  const bad = required(out, [
    ["pid", int(f.pid, 1, 2 ** 31 - 1)], ["port", int(f.port, 0, 65535)],
    ["skew_limit_ns", int(f.skewLimitNs, 0, Number.MAX_SAFE_INTEGER)], ["thread_id", f.threadId || null],
    ["kernel_isa", f.kernelIsa || null], ["max_step_retries", int(f.maxStepRetries, 0, Number.MAX_SAFE_INTEGER)],
    ["bp_unmapped", f.bpUnmapped || null],
  ]) ?? optional(out, [["log", f.log, (s) => s.trim()]]);
  return bad ? fail([bad]) : { params: out as Params<"linux.halt.gdb.serve"> };
}

/** linux.halt.threads の引数 */
export function haltThreadsParams(pid: string): Built<"linux.halt.threads"> {
  const v = int(pid, 1, 2 ** 31 - 1);
  return v === null ? fail(["pid"]) : { params: { pid: v } };
}

/** linux.memap.connect_jtag の引数（chain_idcode・ap・frequency_hz はすべて明示） */
export function memapJtagParams(target: string, f: MemapJtagForm): Built<"linux.memap.connect_jtag"> {
  const out: Record<string, unknown> = { target };
  const bad = required(out, [["chain_idcode", int(f.chainIdcode, 0, U32)], ["ap", int(f.ap, 0, 255)],
    ["frequency_hz", int(f.frequencyHz, 1, Number.MAX_SAFE_INTEGER)]]);
  return bad ? fail([bad]) : { params: out as Params<"linux.memap.connect_jtag"> };
}

/** linux.halt.debug.attach の欄（全コア停止で止めて調べる。人も画面から AI と同じ attach をする） */
export interface DebugAttachForm {
  pid: string;
  elf: string;
  skewLimitNs: string;
  threadId: "" | "stack" | "contextidr";
  kernelIsa: "" | "arm" | "thumb";
  maxStepRetries: string;
  bpUnmapped: "" | "error" | "hw";
  maxHaltS: string;
  fetchFromTarget: string;                        // linux.connect のターゲットの名前（ELF・PC に無いソースの取り寄せ元）
  breakpointKind: "" | "sw" | "hw";               // 画面の行のクリックで置く種類
  stepMaxInstructions: string;                    // 画面のステップの上限
  stepTimeoutS: string;
}

export const EMPTY_ATTACH_FORM: DebugAttachForm = { pid: "", elf: "", skewLimitNs: "", threadId: "", kernelIsa: "",
  maxStepRetries: "", bpUnmapped: "", maxHaltS: "", fetchFromTarget: "", breakpointKind: "", stepMaxInstructions: "",
  stepTimeoutS: "" };

export function exampleAttachForm(): DebugAttachForm {
  const a = (p: string) => eg("linux.halt.debug.attach", p);
  return { ...EMPTY_ATTACH_FORM, skewLimitNs: a("skew_limit_ns"), threadId: a("thread_id") as DebugAttachForm["threadId"],
    kernelIsa: a("kernel_isa") as DebugAttachForm["kernelIsa"], maxStepRetries: a("max_step_retries"),
    bpUnmapped: a("bp_unmapped") as DebugAttachForm["bpUnmapped"], maxHaltS: a("max_halt_s"),
    breakpointKind: a("breakpoint_kind") as DebugAttachForm["breakpointKind"], stepMaxInstructions: a("step_max_instructions"),
    stepTimeoutS: a("step_timeout_s") };
}

/** linux.halt.debug.attach の引数（既定なし: 必須の欄が空ならその名前。画面の道の欄は書いたときだけ送る） */
export function haltDebugAttachParams(f: DebugAttachForm): Built<"linux.halt.debug.attach"> {
  const out: Record<string, unknown> = {};
  const bad = requiredAll(out, [
    ["pid", int(f.pid, 1, 2 ** 31 - 1)], ["elf", f.elf.trim() || null],
    ["skew_limit_ns", int(f.skewLimitNs, 0, Number.MAX_SAFE_INTEGER)], ["thread_id", f.threadId || null],
    ["kernel_isa", f.kernelIsa || null], ["max_step_retries", int(f.maxStepRetries, 1, Number.MAX_SAFE_INTEGER)],
    ["bp_unmapped", f.bpUnmapped || null], ["max_halt_s", num(f.maxHaltS, true)],
    ]).concat(optionalAll(out, [
    ["fetch_from_target", f.fetchFromTarget, (s) => s.trim()],
    ["breakpoint_kind", f.breakpointKind, (s) => s],
    ["step_max_instructions", f.stepMaxInstructions, (s) => int(s, 1, Number.MAX_SAFE_INTEGER)],
    ["step_timeout_s", f.stepTimeoutS, (s) => num(s)],
  ]));
  return bad.length ? fail(bad) : { params: out as Params<"linux.halt.debug.attach"> };
}

/** 使っている設定（API が返した options）を欄の値にする（欄に無い・null の設定は今の欄の値のまま）。AI が open / attach したときも、
 *  人の画面の欄に同じ値が見える。数・真偽は欄の文字にする */
const pick = (o: Record<string, unknown>, key: string, fallback: string): string => {
  const v = o[key];
  return v === undefined || v === null ? fallback : String(v);
};

export function haltFormOf(f: HaltForm, o: Record<string, unknown>): HaltForm {
  return { ...f, backend: pick(o, "backend", f.backend) as HaltForm["backend"], jtagKhz: pick(o, "jtag_khz", f.jtagKhz),
    offsets: pick(o, "offsets", f.offsets), pageOffset: hexOr(o.page_offset, f.pageOffset),
    physOffset: hexOr(o.phys_offset, f.physOffset), linearMapEnd: hexOr(o.linear_map_end, f.linearMapEnd),
    maxTasks: pick(o, "max_tasks", f.maxTasks), maxThreads: pick(o, "max_threads", f.maxThreads),
    mmuFormat: pick(o, "mmu_format", f.mmuFormat) as HaltForm["mmuFormat"], journal: pick(o, "journal", f.journal),
    fpgaRequestTimes: pick(o, "fpga_request_times", f.fpgaRequestTimes) as HaltForm["fpgaRequestTimes"],
    part: pick(o, "part", f.part) };
}

export function attachFormOf(f: DebugAttachForm, o: Record<string, unknown>): DebugAttachForm {
  return { ...f, pid: pick(o, "pid", f.pid), elf: pick(o, "elf", f.elf), skewLimitNs: pick(o, "skew_limit_ns", f.skewLimitNs),
    threadId: pick(o, "thread_id", f.threadId) as DebugAttachForm["threadId"],
    kernelIsa: pick(o, "kernel_isa", f.kernelIsa) as DebugAttachForm["kernelIsa"],
    maxStepRetries: pick(o, "max_step_retries", f.maxStepRetries),
    bpUnmapped: pick(o, "bp_unmapped", f.bpUnmapped) as DebugAttachForm["bpUnmapped"], maxHaltS: pick(o, "max_halt_s", f.maxHaltS),
    fetchFromTarget: pick(o, "fetch_from_target", f.fetchFromTarget),
    breakpointKind: pick(o, "breakpoint_kind", f.breakpointKind) as DebugAttachForm["breakpointKind"],
    stepMaxInstructions: pick(o, "step_max_instructions", f.stepMaxInstructions),
    stepTimeoutS: pick(o, "step_timeout_s", f.stepTimeoutS) };
}

/** パネルが状態を読み直す操作か。linux.halt.* のうち、状態を変える操作だけ（パネル自身の読み status・debug.status・threads 等の
 *  読みの記録では読み直さない。読み直しが記録を生み、通知がまた読み直しを呼ぶ循環になる） */
const HALT_READS = new Set(["linux.halt.status", "linux.halt.debug.status", "linux.halt.debug.threads", "linux.halt.debug.read",
  "linux.halt.debug.memory", "linux.halt.threads", "linux.halt.processes", "linux.halt.gdb.status",
  "linux.halt.profile.save", "linux.halt.profile.load"]);
export function shouldRefreshOn(method: string): boolean {
  return method.startsWith("linux.halt.") && !HALT_READS.has(method);
}
