// 低レベルの窓（LowLevelPanel）の計算: ほかの窓に出ていない書く操作（target.dp.write・target.ap.write・probe.csr.write・
// probe.pins.write・gpio.set・xtrig.output_set）を、読んだ今の値の横で書くための引数の組み立て。Svelte に依らない。
// 名前・番地・選択肢は API の仕様（説明と列挙）にあるものだけを使う。
import { METHODS } from "./apispec";
import { parseInteger, type ApiCall, type Built } from "./valuewrite";

/** 整数の欄を [min, max] で読む */
function intIn(text: string, min: bigint, max: bigint): number | "value" | "range" {
  const v = parseInteger(text);
  if (v === null) return "value";
  if (v < min || v > max) return "range";
  return Number(v);
}
const U32 = 0xFFFF_FFFFn;
const isErr = (v: number | "value" | "range"): v is "value" | "range" => typeof v === "string";

// --- DP / AP ----------------------------------------------------------------------------------------

/** DP のレジスタ（target.dp.read / write の名前）。番地 0 は読むと DPIDR・書くと ABORT（ADI の定義）なので行を分ける */
export const DP_REGISTERS = [
  { name: "DPIDR", read: true, write: false },
  { name: "ABORT", read: false, write: true },
  { name: "CTRL/STAT", read: true, write: true },
  { name: "SELECT", read: true, write: true },
  { name: "RDBUFF", read: true, write: true },
  { name: "TARGETID", read: true, write: true },
  { name: "DLPIDR", read: true, write: true },
  { name: "EVENTSTAT", read: true, write: true },
  { name: "BASEPTR0", read: true, write: true },
] as const;

export function dpWriteCall(register: string, text: string): Built {
  const value = intIn(text, 0n, U32);
  return isErr(value) ? { error: value } : { method: "target.dp.write", params: { register, value } };
}

/** AP の番号とレジスタの番地の欄（target.ap.read の引数） */
export function apRegisterParams(apText: string, registerText: string): { ap: number; register: number } | { error: "value" | "range" } {
  const ap = intIn(apText, 0n, U32);
  if (isErr(ap)) return { error: ap };
  const register = intIn(registerText, 0n, U32);
  if (isErr(register)) return { error: register };
  return { ap, register };
}

export function apWriteCall(ap: number, register: number, text: string): Built {
  const value = intIn(text, 0n, U32);
  return isErr(value) ? { error: value } : { method: "target.ap.write", params: { ap, register, value } };
}

// --- CSR --------------------------------------------------------------------------------------------

/** 番地と語の数の欄（probe.csr.read の引数） */
export function csrReadParams(addressText: string, countText: string): { address: number; count: number } | { error: "value" | "range" } {
  const address = intIn(addressText, 0n, U32);
  if (isErr(address)) return { error: address };
  const count = intIn(countText, 1n, 256n);
  if (isErr(count)) return { error: count };
  return { address, count };
}

export function csrWriteCall(address: number, text: string): Built {
  const value = intIn(text, 0n, U32);
  return isErr(value) ? { error: value } : { method: "probe.csr.write", params: { address, words: [value] } };
}

// --- ピン・GPIO -------------------------------------------------------------------------------------

export type Pin = "swclk" | "swdio" | "nreset";
export const PINS: readonly Pin[] = ["swclk", "swdio", "nreset"];

/** ピンを今の値の逆にする probe.pins.write */
export function pinsWriteCall(pin: Pin, current: number): ApiCall {
  return { method: "probe.pins.write", params: { [pin]: current ? 0 : 1 } };
}

/** 8 ビットの値をビットの列に（ビット 0 から） */
export function bitsOf(value: number, width = 8): number[] {
  return Array.from({ length: width }, (_, i) => (value >> i) & 1);
}

/** GPIO の 1 本（ch）の出力の値か出力にするかを反転する gpio.set（mask はその 1 本。もう一方は今の値） */
export function gpioSetCall(state: { drive: number; enable: number }, ch: number, field: "drive" | "enable"): ApiCall {
  const bit = 1 << ch;
  const drive = field === "drive" ? state.drive ^ bit : state.drive;
  const enable = field === "enable" ? state.enable ^ bit : state.enable;
  return { method: "gpio.set", params: { drive, enable, mask: bit } };
}

// --- 出力の端子（xtrig.output_set）-------------------------------------------------------------------

/** メソッドの引数の列挙（配列なら要素の列挙）。仕様に無ければ空 */
export function optionsOf(method: string, param: string): string[] {
  const schema = METHODS.find((m) => m.name === method)?.params?.properties?.[param];
  const find = (s: any): unknown[] | null => {
    if (!s) return null;
    if (Array.isArray(s.enum)) return s.enum.filter((v: unknown) => v !== null);
    if (s.items) return find(s.items);
    for (const alt of s.anyOf ?? []) {
      const found = find(alt);
      if (found) return found;
    }
    return null;
  };
  return (find(schema) ?? []).map(String);
}

export interface OutputStatus {
  channel: number;
  configured: boolean;
  enabled?: boolean;
  mode?: string;
  level?: string;
  sources?: string[];
  pattern?: { mask: number; value: number; any: boolean };
  pulse_us?: { min: number; max: number };
}

export interface OutputForm {
  channel: number;
  mode: string;
  level: string;
  sources: string[];
  mask: string;            // 端子の並び（空なら使わない）
  value: string;
  any: boolean;
  pulseUs: string;         // 脈の長さ（µs。空なら API の既定）
  enable: boolean;
}

const hex = (n: number) => `0x${n.toString(16).toUpperCase()}`;

/** 欄の初めの値: 設定済みならその設定（脈の長さは刻みに丸めた範囲しか返らないので空のまま）、無ければ空
 *  （形・出す値も使う人が選ぶ。画面の既定を持たない） */
export function outputForm(s: OutputStatus): OutputForm {
  const p = s.configured && s.pattern && s.pattern.mask ? s.pattern : null;
  return {
    channel: s.channel, mode: s.configured && s.mode ? s.mode : "", level: s.configured && s.level ? s.level : "",
    sources: s.configured ? [...(s.sources ?? [])] : [], mask: p ? hex(p.mask) : "", value: p ? hex(p.value) : "",
    any: p?.any ?? false, pulseUs: "", enable: !!s.enabled,
  };
}

export function outputSetCall(f: OutputForm): Built {
  if (!f.mode || !f.level) return { error: "choice" };
  const params: Record<string, unknown> = { channel: f.channel, mode: f.mode, level: f.level };
  if (f.mask.trim()) {
    const mask = intIn(f.mask, 0n, 255n);
    if (isErr(mask)) return { error: mask };
    const value = intIn(f.value, 0n, 255n);                       // 空を 0 とみなさない
    if (isErr(value)) return { error: value };
    params.pattern = { mask, value, any: f.any };
  } else if (f.sources.length) {
    params.sources = [...f.sources];
  } else {
    return { error: "condition" };
  }
  if (f.mode === "pulse" && f.pulseUs.trim()) {
    const us = Number(f.pulseUs.trim());
    if (!Number.isFinite(us) || us <= 0) return { error: "value" };
    params.pulse_us = us;
  }
  params.enable = f.enable;
  return { method: "xtrig.output_set", params };
}
