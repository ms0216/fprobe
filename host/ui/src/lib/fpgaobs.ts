// FPGA 観測（パネル FpgaObsPanel）の計算。Svelte に依らない。
// 信号の定義はデーモンの fprobe.fpga_signals と同じ形。規則（検査）はデーモンだけが持つ（fpga_obs.signals_set が断る）。
// 仕様: docs/superpowers/specs/2026-10-20-fpga-observation-ui-design.md §4
import type { Key } from "./i18n";
import { parseInteger, type Built } from "./valuewrite";

export const HALF_MIN = 2, HALF_MAX = 65535;      // TCK の半周期（sys の周期数）
export const FORMATS = ["hex", "dec", "bin", "enum"] as const;

export interface SignalDef {
  name: string;
  lsb: number;
  width: number;
  format: (typeof FORMATS)[number];
  enum: Record<string, string>;
}

export type Groups = Record<string, SignalDef[]>;

/** 「0=IDLE, 1=RUN, 0x5=ERR」→ {"0": "IDLE", "1": "RUN", "5": "ERR"}。形が崩れていれば null */
export function parseEnum(text: string): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const part of text.split(",").map(s => s.trim()).filter(Boolean)) {
    const m = /^(0x[0-9a-f]+|\d+)\s*=\s*(.+)$/i.exec(part);
    if (!m || !m[2].trim()) return null;
    out[String(Number(m[1]))] = m[2].trim();
  }
  return out;
}

export function formatEnum(e: Record<string, string>): string {
  return Object.entries(e).sort((a, b) => Number(a[0]) - Number(b[0])).map(([k, v]) => `${k}=${v}`).join(", ");
}

/** 「7:4」→ {lsb: 4, width: 4}、「3」→ {lsb: 3, width: 1}。形が崩れていれば null */
export function parseBits(text: string): { lsb: number; width: number } | null {
  const m = /^\s*(\d+)\s*(?::\s*(\d+)\s*)?$/.exec(text);
  if (!m) return null;
  const hi = Number(m[1]), lo = m[2] === undefined ? hi : Number(m[2]);
  return hi < lo ? null : { lsb: lo, width: hi - lo + 1 };
}

export function formatBits(lsb: number, width: number): string {
  return width === 1 ? String(lsb) : `${lsb + width - 1}:${lsb}`;
}

/** 求めた TCK（MHz）を超えない最も速い半周期と、その実際の速さ（TCK = sys / (2 × 半周期)） */
export function tckToHalfPeriod(mhz: number, sysHz: number): { half: number; mhz: number } | null {
  if (!(Number(mhz) > 0)) return null;                                   // 欄が空・0 以下: 決まらない
  const half = Math.min(HALF_MAX, Math.max(HALF_MIN, Math.ceil(sysHz / (2 * mhz * 1e6) - 1e-9)));   // 誤差で 1 つ遅くしない
  return { half, mhz: sysHz / (2 * half) / 1e6 };
}

/** fpga_obs_open の引数（デーモンが覚えて fpga_obs.status の config で返す） */
export interface OpenConfig {
  chain: { ir_len?: number; ir_before?: number; ir_after?: number; dr_before?: number; dr_after?: number } | null;
  user_ir: number;
  half_period: number;
  words: number;
  sync_period: number;
  id_scans?: number;                 // ID の応答を待つスキャンの上限（0 = 上限なし）
}

/** 接続の欄の値。チェーンの欄の null = 入れていない（送らない）。tckMhz はプローブがシステムのクロックを申告したときの
 *  欄、halfPeriod は申告が無いときの欄（半周期を sys の周期数で直接） */
export interface ConnectionFields {
  irLen: number | null;
  irBefore: number | null;
  irAfter: number | null;
  drBefore: number | null;
  drAfter: number | null;
  userIr: string;
  tckMhz: number | null;
  halfPeriod: number | null;
  words: number | null;              // null = 入れていない（開かない。画面の既定を持たない）
  syncPeriod: number | null;
  idScans: number | null;            // null = 入れていない（送らない = 上限なし）
}

const given = (v: number | null | undefined): number | null => (v === null || v === undefined || Number.isNaN(v) ? null : v);

/** 開いた設定を接続の欄の値に（別の窓・開き直した画面が、いま動いている設定を見せる）。開いた設定に無い欄は空
 *  （値を仮定して見せない）。sysHz が分からなければ TCK も分からない（null） */
export function configToFields(c: OpenConfig, sysHz: number | null): ConnectionFields {
  const chain = c.chain ?? {};
  return { irLen: given(chain.ir_len), irBefore: given(chain.ir_before), irAfter: given(chain.ir_after),
           drBefore: given(chain.dr_before), drAfter: given(chain.dr_after),
           userIr: `0x${c.user_ir.toString(16).toUpperCase()}`,
           tckMhz: sysHz ? sysHz / (2 * c.half_period) / 1e6 : null, halfPeriod: c.half_period,
           words: c.words, syncPeriod: c.sync_period, idScans: given(c.id_scans) };
}

/** 欄 → fpga_obs.open の引数。半周期は、プローブがシステムのクロック（sysHz）を申告していれば TCK（MHz）から、無ければ
 *  半周期の欄から（クロックを仮定しない）。決まらない・読めない欄があれば null。チェーンは入れた欄だけ（全部空なら null） */
export function openParams(f: ConnectionFields & { autoReopen: boolean }, sysHz: number | null) {
  const half = sysHz ? tckToHalfPeriod(f.tckMhz ?? NaN, sysHz)?.half ?? null
    : Number.isInteger(f.halfPeriod) && f.halfPeriod! >= HALF_MIN && f.halfPeriod! <= HALF_MAX ? f.halfPeriod : null;
  const ir = Number(f.userIr);
  if (half === null || !f.userIr.trim() || !Number.isInteger(ir) || ir < 0) return null;
  const words = given(f.words), syncPeriod = given(f.syncPeriod);
  if (words === null || syncPeriod === null) return null;
  const entries = ([["ir_len", f.irLen], ["ir_before", f.irBefore], ["ir_after", f.irAfter], ["dr_before", f.drBefore],
                    ["dr_after", f.drAfter]] as const).filter(([, v]) => given(v) !== null);
  return { chain: entries.length ? Object.fromEntries(entries) : null, user_ir: ir, half_period: half, words,
           sync_period: syncPeriod, ...(given(f.idScans) !== null ? { id_scans: f.idScans! } : {}), auto_reopen: f.autoReopen };
}

export interface ObsStatus {
  running: boolean;
  stuck: boolean;
  id_ok: boolean;
  overflow_seen: boolean;
  driving: boolean;
  id_timeout?: boolean;              // stuck の理由: id_scans スキャン待って ID の応答が来なかった
}

/** 状態の文言のキー: 立っている事実を決まった並び（答えない・溢れ・動いている / 始めている / 止まっている）で全部。
 *  優先順で 1 つに畳まない（答えなくなっても、それまでの溢れを落とさない） */
export function statusKeys(s: ObsStatus): Key[] {
  const out: Key[] = [];
  if (s.stuck) out.push("fpgaObs.state.stuck");
  if (s.overflow_seen) out.push("fpgaObs.state.overflow");
  if (s.running) out.push("fpgaObs.state.running");
  else if (s.driving) out.push("fpgaObs.state.starting");
  else if (!s.stuck) out.push("fpgaObs.state.off");
  return out;
}

/** 信号の表で編集できる組: 定義のある組・観測の回路の申告の組の数（つないでいれば）・いま選んでいる組の和
 *  （観測とは別。つないでいなくても、開いている記録の組に名前を付けられる） */
export function editableGroups(defined: string[], declaredGroups: number | null, current: number): number[] {
  const out = new Set<number>(defined.map(Number).filter((g) => Number.isInteger(g)));
  for (let g = 0; g < (declaredGroups ?? 0); g++) out.add(g);
  out.add(current);
  return [...out].sort((a, b) => a - b);
}

/** 画面の状態の通知で、信号の定義が変わったか（別の窓・AI の変更を読み直す） */
export function signalsChanged(n: { changed?: string[] }): boolean {
  return Boolean(n.changed?.includes("fpga_signals"));
}

/** 観測の回路の型ごとの接続の設定。記録型（obs_core。2 クロック版は TCK 12.5 MHz まで）は語数を多く・印を定期的に。
 *  標本型（obs_snap_core）と 1 クロック版は TCK ≤ 対象の clk / 8（Shrike の 25 MHz なら 3.1 MHz）・語 0 だけ・印なし */
export const PRESETS = {
  stream: { tckMhz: 10, words: 64, syncPeriod: 20 },
  snapshot: { tckMhz: 2.5, words: 1, syncPeriod: 0 },
} as const;
export type PresetName = keyof typeof PRESETS;

/** 観測の回路（記録型）のレジスタのうち、窓から書くもの（番地は fpga_obs.register_read / register_write の説明: 14 CONTROL の
 *  bit0 = 記録する、15 SELECT = 観測する組）。標本型にはレジスタが無い */
export const OBS_REGISTERS = [{ name: "CONTROL", addr: 14 }, { name: "SELECT", addr: 15 }] as const;

/** レジスタ（16 ビット）に書く fpga_obs.register_write */
export function registerWriteCall(addr: number, text: string): Built {
  const v = parseInteger(text);
  if (v === null) return { error: "value" };
  if (v < 0n || v > 0xFFFFn) return { error: "range" };
  return { method: "fpga_obs.register_write", params: { addr, value: Number(v) } };
}
