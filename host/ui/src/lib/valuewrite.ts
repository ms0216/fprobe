// 見えている値をその窓で書く（仕様 docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §3.6）: どの API で書くかを
// 決める規則と、欄の文字から API の引数を組み立てる計算。Svelte に依らない。
// 規則は事実だけで決める（ターゲットの状態・API の結果が返した番地と大きさ・target.flash.regions の領域）。推定しない:
// 番地や大きさが結果に無い値は書けない（null）と返し、画面はそれを示す。
import type { AppState } from "./state";
import { allowed } from "./guard";
import type { Confirmer } from "./confirm";
import type { Key } from "./i18n";
import { wrap } from "./vartree";

export interface ApiCall {
  method: string;
  params: Record<string, unknown>;
}

/** 欄の値から引数を作れなかった理由（value = 読めない、range = 大きさに収まらない、length = 長さが違う、
 *  condition = 条件が無い、choice = 選ぶ欄が空） */
export type WriteError = "value" | "range" | "length" | "condition" | "choice";
export type Built = ApiCall | { error: WriteError };

export const isWriteCall = (b: Built): b is ApiCall => "method" in b;

// --- 式（変数）---------------------------------------------------------------------------------------

export type ExprWriteMethod = "watch.write" | "debug.write_expression";

/** 式に書く API: 走行中（RUNNING・SLEEPING）は止めずに書く watch.write、止まっていれば debug.write_expression。
 *  それ以外（未接続・リセット中等）は書けない（null） */
export function exprWriteMethod(state: string | undefined): ExprWriteMethod | null {
  if (state === "RUNNING" || state === "SLEEPING") return "watch.write";
  if (state === "HALTED") return "debug.write_expression";
  return null;
}

/** 式に書く呼び出し。debug.write_expression は真偽を受けないので数に、frame は渡したときだけ付ける */
export function exprWriteCall(state: string | undefined, expression: string, value: number | string | boolean,
                              frame?: number): ApiCall | null {
  const method = exprWriteMethod(state);
  if (method === "watch.write") return { method, params: { expression, value } };
  if (method === "debug.write_expression") {
    return { method, params: { expression, value: typeof value === "boolean" ? Number(value) : value,
                               ...(frame !== undefined ? { frame } : {}) } };
  }
  return null;
}

/** 配列の要素（とそのメンバー）の式: `a[3]`・`s.items[0].value`・`(*p)[1]` */
export function elementExpr(array: string, index: number, member = ""): string {
  return `${wrap(array.trim())}[${index}]${member.trim() ? `.${member.trim()}` : ""}`;
}

// --- メモリの値（番地と大きさは API の結果の事実）---------------------------------------------------------

export interface Location {
  address: number;
  size: number;
}

/** 整数の文字（16 進 0x・2 進 0b・10 進、符号付き）。読めなければ null */
export function parseInteger(text: string): bigint | null {
  const m = /^([-+]?)(0x[0-9a-f]+|0b[01]+|\d+)$/i.exec(text.trim());
  if (!m) return null;
  const n = BigInt(m[2]);
  return m[1] === "-" ? -n : n;
}

/** size バイトに小さい方から並べた 16 進（負は 2 の補数）。収まらなければ null */
export function encodeLE(value: bigint, size: number): string | null {
  const bits = BigInt(size * 8);
  if (value < -(1n << (bits - 1n)) || value >= 1n << bits) return null;
  let v = value < 0n ? (1n << bits) + value : value;
  let out = "";
  for (let i = 0; i < size; i++) {
    out += Number(v & 0xFFn).toString(16).padStart(2, "0");
    v >>= 8n;
  }
  return out;
}

/** 場所に整数を書く target.memory.write */
export function memoryWriteCall(at: Location, text: string): Built {
  const value = parseInteger(text);
  if (value === null) return { error: "value" };
  const data = encodeLE(value, at.size);
  if (data === null) return { error: "range" };
  return { method: "target.memory.write", params: { address: at.address, data_hex: data } };
}

/** API の結果の locations の欄（{address, size}）。番地・大きさが数で無ければ書けない（null） */
export function locationOf(result: { locations?: Record<string, unknown> } | null | undefined, field: string): Location | null {
  const at = result?.locations?.[field] as { address?: unknown; size?: unknown } | undefined;
  if (!at || typeof at.address !== "number" || typeof at.size !== "number" || !(at.size > 0)) return null;
  return { address: at.address, size: at.size };
}

/** 1 を書いたビットが消えるレジスタか（target.fault の write_one_to_clear） */
export function writeOneToClear(result: { write_one_to_clear?: string[] } | null | undefined, name: string): boolean {
  return !!result?.write_one_to_clear?.includes(name);
}

// --- 命令のバイト（逆アセンブル）---------------------------------------------------------------------

export interface FlashRegion {
  start: number;
  size: number;
}

/** 命令を書く経路: Flash の領域（target.flash.regions）に掛かれば flash（target.flash が要る。ここでは書かない）、
 *  掛からなければ memory（target.memory.write）。領域を読めていなければ unknown（書かない） */
export function instructionWriteKind(regions: FlashRegion[] | null, address: number, length: number):
  "memory" | "flash" | "unknown" {
  if (!regions) return "unknown";
  const end = address + Math.max(1, length);
  return regions.some((r) => address < r.start + r.size && r.start < end) ? "flash" : "memory";
}

/** 命令のバイト（メモリの順の 16 進。空白は無視）を書く target.memory.write。長さはいまの命令と同じ */
export function bytesWriteCall(address: number, current: string, text: string): Built {
  const hex = text.replace(/\s+/g, "").toLowerCase();
  if (!/^([0-9a-f]{2})+$/.test(hex)) return { error: "value" };
  if (hex.length !== current.length) return { error: "length" };
  return { method: "target.memory.write", params: { address, data_hex: hex } };
}

// --- 書く -------------------------------------------------------------------------------------------

/** 書く: AI の作業中なら確かめてから（lib/guard の allowed）。結果（断った・失敗なら undefined）を返す */
export async function performWrite(app: Pick<AppState, "aiConnectedUnpaused"> & { act(method: string, params?: any): Promise<any> },
                                   call: ApiCall, ask?: Confirmer): Promise<any> {
  if (!(await allowed(app, ask))) return undefined;
  return app.act(call.method, call.params);
}

/** 欄から組み立てた呼び出しを書く。組み立てられなければその理由の文（write.error.*）を返す（書いた・断った・API の
 *  誤り（app.act が出す）は null） */
export async function writeBuilt(app: Parameters<typeof performWrite>[0], built: Built, t: (key: Key) => string,
                                 ask?: Confirmer): Promise<string | null> {
  if (!isWriteCall(built)) return t(`write.error.${built.error}`);
  await performWrite(app, built, ask);
  return null;
}
