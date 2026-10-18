// Flash のパネル（J-Flash Lite の型）の計算（Svelte に依らない）: 書き込みの設定と target.flash の引数、ファイルの形式と
// 番地、進捗の通知（flash.progress）、結果の段ごとの時間・速度の表、Flash の領域のセクタの並び。
import { formatDuration } from "./time";

export type EraseMode = "auto" | "sector" | "chip";

/** 書き込みの設定（画面に覚える） */
export interface FlashOptions {
  erase: EraseMode;
  verify: boolean;
  smart: boolean;           // 同じ中身のページは書かない
  reset: boolean;           // 書いた後にリセットして走らせる
}

export const DEFAULT_OPTIONS: FlashOptions = { erase: "auto", verify: true, smart: true, reset: true };

/** 覚えた設定を読む（壊れていれば既定） */
export function loadOptions(text: string): FlashOptions {
  try {
    const o = JSON.parse(text);
    return {
      erase: ["auto", "sector", "chip"].includes(o.erase) ? o.erase : DEFAULT_OPTIONS.erase,
      verify: typeof o.verify === "boolean" ? o.verify : DEFAULT_OPTIONS.verify,
      smart: typeof o.smart === "boolean" ? o.smart : DEFAULT_OPTIONS.smart,
      reset: typeof o.reset === "boolean" ? o.reset : DEFAULT_OPTIONS.reset,
    };
  } catch {
    return { ...DEFAULT_OPTIONS };
  }
}

/** ファイルの形式（拡張子で。デーモンは中身でも決める）。BIN は番地が要る */
export function formatOf(path: string): "elf" | "hex" | "bin" {
  const ext = /\.([^.\\/]+)$/.exec(path)?.[1]?.toLowerCase() ?? "";
  if (["elf", "axf", "out"].includes(ext)) return "elf";
  if (["hex", "ihex", "ihx"].includes(ext)) return "hex";
  return ext === "bin" ? "bin" : "elf";
}

/** 番地・長さの欄: 0x… と数字だけの並びは 16 進（Memory の番地と同じ）、末尾 K / M はバイト数の倍。空・誤りは null */
export function parseNumber(text: string): number | null {
  const s = text.trim().replace(/_/g, "");
  const unit = /^(\d+)\s*([kKmM])$/.exec(s);
  if (unit) return Number(unit[1]) * (unit[2].toLowerCase() === "k" ? 1024 : 1 << 20);
  if (/^0x[0-9a-f]+$/i.test(s)) return parseInt(s.slice(2), 16);
  if (/^[0-9a-f]+$/i.test(s)) return parseInt(s, 16);
  return null;
}

/** target.flash の引数。BIN で番地が無ければ投げる（BIN 以外は番地を渡さない: ファイルが持つ） */
export function programParams(path: string, address: string, o: FlashOptions) {
  const at = formatOf(path) === "bin" ? parseNumber(address) : null;
  if (formatOf(path) === "bin" && at === null) throw new RangeError("address");
  return { path, ...(at !== null ? { address: at } : {}), erase: o.erase, verify: o.verify, smart: o.smart,
           reset: o.reset };
}

/** target.flash.verify の引数 */
export function verifyParams(path: string, address: string) {
  const at = formatOf(path) === "bin" ? parseNumber(address) : null;
  if (formatOf(path) === "bin" && at === null) throw new RangeError("address");
  return { path, ...(at !== null ? { address: at } : {}) };
}

/** バイト数の表示（1.5 KB・2.0 MB） */
export function size(n: number | null | undefined): string {
  if (n == null) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1 << 20) return `${(n / 1024).toFixed(n % 1024 ? 1 : 0)} KB`;
  return `${(n / (1 << 20)).toFixed(1)} MB`;
}

/** 速さの表示（KB/s） */
export function speed(bps: number | null | undefined): string {
  if (!bps) return "-";
  return bps >= 1 << 20 ? `${(bps / (1 << 20)).toFixed(2)} MB/s` : `${(bps / 1024).toFixed(1)} KB/s`;
}

/** 所要時間（lib/time.ts の時間の長さ、有効数字 3 桁） */
export function seconds(s: number | null | undefined): string {
  return s == null ? "-" : formatDuration(s, 3);
}

interface Rate {
  bytes: number;
  seconds: number;
  bytes_per_s: number | null;
}

/** 結果の 1 段（表の 1 行） */
export interface Stage {
  stage: "erase" | "program" | "verify" | "read" | "ram" | "total" | "write";
  bytes: number;
  seconds: number;
  bps: number | null;
  sectors?: number;         // erase: 消したセクタの数
  method?: string;          // erase: sector / chip
  skipped?: number;         // program: 同じ中身で書かなかったバイト（smart）
}

const rate = (stage: Stage["stage"], r: Rate | null | undefined, extra: Partial<Stage> = {}): Stage[] =>
  r ? [{ stage, bytes: r.bytes, seconds: r.seconds, bps: r.bytes_per_s, ...extra }] : [];

/** target.flash の結果 → 段の表（消去・書き込み・RAM・検証・全体） */
export function programStages(result: any): Stage[] {
  const p = result?.program;
  if (!p) return [];
  return [
    ...rate("erase", p.erase, { sectors: p.erase?.sectors ?? 0, method: p.erase_method }),
    ...rate("program", p.program, p.skipped_bytes ? { skipped: p.skipped_bytes } : {}),
    ...(p.ram_bytes ? [{ stage: "ram" as const, bytes: p.ram_bytes, seconds: 0, bps: null }] : []),
    ...rate("verify", result.verify?.read),
    { stage: "total", bytes: p.bytes + (p.ram_bytes ?? 0), seconds: result.seconds ?? p.seconds, bps: p.bytes_per_s },
  ];
}

/** 進捗の通知（flash.progress）: operation = program / erase / verify / read、phase = erase / program / verify / read */
export interface Progress {
  operation: string;
  phase: string;
  fraction: number;
  elapsed_s: number;
}

/** 進捗の帯の値（0..1）。program は段（erase → program → verify）の続きとして 1 本で見せる */
export function overall(p: Progress, verify = true): number {
  if (p.operation !== "program") return p.fraction;
  const phases = verify ? ["erase", "program", "verify"] : ["erase", "program"];
  const i = Math.max(0, phases.indexOf(p.phase));
  return Math.min(1, (i + p.fraction) / phases.length);
}

/** 残りの時間の見積もり（秒）: 経過 × (1 − 進み) / 進み。段ごとの速さの違いは見ない目安。まだ進んでいなければ null */
export function remaining(p: Progress, verify = true): number | null {
  const f = overall(p, verify);
  if (f >= 1) return 0;
  return f > 0 ? (p.elapsed_s * (1 - f)) / f : null;
}

/** 消去の方法（sector / chip / mass）の文言のキー。知らない方法は null（そのまま出す） */
export function eraseMethodKey(method: string | null | undefined): "flash.eraseSector" | "flash.eraseChip"
  | "flash.eraseMass" | null {
  return method === "sector" ? "flash.eraseSector" : method === "chip" ? "flash.eraseChip"
    : method === "mass" ? "flash.eraseMass" : null;
}

/** 外付けの書き込みアルゴリズム（.FLM）の欄 */
export interface AlgoForm {
  flm: string;
  address: string;
  size: string;
  ramAddress: string;
  ramSize: string;
  name: string;
}

export const emptyAlgo = (): AlgoForm => ({ flm: "", address: "", size: "", ramAddress: "", ramSize: "", name: "" });

/** target.flash.algo.add の引数。空の欄は省く（番地・大きさは FLM の値、RAM は既定の RAM、名前は FLM のファイル名）。
 *  FLM が空・数が読めなければ、その欄の名前（flm / address / size / ram_address / ram_size）で RangeError を投げる */
export function algoParams(f: AlgoForm): Record<string, string | number> {
  const flm = f.flm.trim();
  if (!flm) throw new RangeError("flm");
  const out: Record<string, string | number> = { flm };
  for (const [key, text] of [["address", f.address], ["size", f.size], ["ram_address", f.ramAddress],
                             ["ram_size", f.ramSize]] as const) {
    if (!text.trim()) continue;
    const n = parseNumber(text);
    if (n === null) throw new RangeError(key);
    out[key] = n;
  }
  if (f.name.trim()) out.name = f.name.trim();
  return out;
}

/** Flash の領域のセクタの並び（同じ大きさの連続）→ "4 × 16 KB, 1 × 64 KB, 3 × 128 KB" */
export function sectorsText(sectors: { start: number; count: number; size: number }[]): string {
  return sectors.map((s) => `${s.count} × ${size(s.size)}`).join(", ");
}

/** ファイルの名前（パスの最後） */
export function baseName(path: string): string {
  return path.split(/[\\/]/).pop() ?? path;
}

/** 読み出しの保存先の既定: 読み込んだ ELF と同じ場所の readback.hex（無ければ名前だけ = デーモンの作業場所） */
export function defaultReadPath(elf: string | null): string {
  if (!elf) return "readback.hex";
  const i = Math.max(elf.lastIndexOf("/"), elf.lastIndexOf("\\"));
  return i < 0 ? "readback.hex" : `${elf.slice(0, i + 1)}readback.hex`;
}
