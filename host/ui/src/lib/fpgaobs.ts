// FPGA 観測（パネル FpgaObsPanel）の計算。Svelte に依らない。
// 信号の定義はデーモンの fprobe.fpga_signals と同じ形・同じ規則（保存の前に画面で知らせる。最後はデーモンが確かめる）。
// 仕様: docs/superpowers/specs/2026-10-20-fpga-observation-ui-design.md §4
import type { Key } from "./i18n";

export const NAME_MAX = 64;                       // 信号の名前の長さの上限（fpga_signals.py の NAME_MAX と同じ）
export const WIDTH = 16;                          // 観測の回路の組の幅
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

export interface SignalError {
  key: Key;
  params: Record<string, string>;
}

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

/** 定義の誤り（重なり・16 ビットの外・名前の重複・空の名前・列挙の値が幅の外） */
export function validateSignals(groups: Groups): SignalError[] {
  const errors: SignalError[] = [];
  for (const [group, signals] of Object.entries(groups)) {
    let used = 0;
    const names = new Set<string>();
    for (const s of signals) {
      const name = s.name.trim();
      if (!name) {
        errors.push({ key: "fpgaObs.err.emptyName", params: { group } });
        continue;
      }
      if (name.length > NAME_MAX) errors.push({ key: "fpgaObs.err.longName", params: { group, name: name.slice(0, 16) } });
      if (names.has(name)) errors.push({ key: "fpgaObs.err.duplicate", params: { group, name } });
      names.add(name);
      if (s.lsb < 0 || s.width < 1 || s.lsb + s.width > WIDTH) {
        errors.push({ key: "fpgaObs.err.range", params: { group, name } });
        continue;
      }
      const bits = ((1 << s.width) - 1) << s.lsb;
      if (used & bits) errors.push({ key: "fpgaObs.err.overlap", params: { group, name } });
      used |= bits;
      if (s.format === "enum") {
        const keys = Object.keys(s.enum);
        for (const k of keys) {                     // デーモン（fpga_signals.py）と同じ: 0 以上の整数・名前は空でない
          if (!/^\d+$/.test(k.trim())) errors.push({ key: "fpgaObs.err.enumValue", params: { group, name } });
          else if (!s.enum[k].trim()) errors.push({ key: "fpgaObs.err.enumName", params: { group, name } });
        }
        if (keys.some(k => Number(k) >= 1 << s.width))
          errors.push({ key: "fpgaObs.err.enumRange", params: { group, name } });
      }
    }
  }
  return errors;
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
}

/** 開いた設定を接続の欄の値に（別の窓・開き直した画面が、いま動いている設定を見せる） */
export function configToFields(c: OpenConfig, sysHz: number) {
  const chain = c.chain ?? {};
  return { irLen: chain.ir_len ?? 4, irBefore: chain.ir_before ?? 0, irAfter: chain.ir_after ?? 0,
           drBefore: chain.dr_before ?? 0, drAfter: chain.dr_after ?? 0, userIr: `0x${c.user_ir.toString(16).toUpperCase()}`,
           tckMhz: sysHz / (2 * c.half_period) / 1e6, words: c.words, syncPeriod: c.sync_period };
}

export interface ObsStatus {
  running: boolean;
  stuck: boolean;
  id_ok: boolean;
  overflow_seen: boolean;
  driving: boolean;
}

/** 状態の文言のキー（答えない → 溢れ → 動いている → 止まっている） */
export function statusKey(s: ObsStatus): Key {
  if (s.stuck) return "fpgaObs.state.stuck";
  if (s.running && s.overflow_seen) return "fpgaObs.state.overflow";
  if (s.running) return "fpgaObs.state.running";
  if (s.driving) return "fpgaObs.state.starting";
  return "fpgaObs.state.off";
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
