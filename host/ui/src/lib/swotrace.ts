// SWO Trace のパネル（IAR の SWO Trace・STM32CubeIDE の SWV Data Trace / Exception Trace の型）の計算。Svelte に依らない。
// 設定 = swo.configure（例外トレース・ローカルのタイムスタンプ・データトレース = DWT の比較器で変数へのアクセス）、
// 結果 = swo.trace（アクセスの一覧・例外の回数）、戻す = swo.unconfigure。
import { errorText, isNoData } from "./api";
import { display } from "./vartree";

export interface TraceSettings {
  exceptions: boolean;
  timestamps: boolean;
  variables: { expression: string; access: "read" | "write" | "rw" }[];
  swoBaud: number | null;                 // null = まだ選んでいない（2 MHz 等を既定にしない）
  coreClockHz: number | null;
}

/** 設定 → swo.configure の引数（空の式は除く）。比較器の数で黙って捨てない: 使える数はターゲットの DWT（NUMCOMP と
 *  使用中のもの）で決まり、足りなければ swo.configure が数と使用中の比較器を誤りで返す。SWO の速さを選んでいなければ誤り */
export function configureParams(s: TraceSettings): Record<string, unknown> | { error: "swoBaud" } {
  if (s.swoBaud === null) return { error: "swoBaud" };
  const data = s.variables.filter((v) => v.expression.trim())
    .map((v) => ({ expression: v.expression.trim(), access: v.access }));
  return {
    exception_trace: s.exceptions, timestamps: s.timestamps, data_trace: data.length ? data : null,
    swo_baud: s.swoBaud, core_clock_hz: s.coreClockHz,
  };
}

export interface Access {
  t: number;
  name: string;
  write: boolean;
  address: number | null;
  value: unknown;              // fprobe.dwarf.values の形（値の無いアクセスは null）
  pc?: number;
  function?: string;
  file?: string;
  line?: number;
}

/** 一覧の終わりの limit 件を読む offset（新しいものを見せる） */
export function tailOffset(total: number, limit: number): number {
  return Math.max(0, total - limit);
}

/** 値（fprobe.dwarf.values の形。型の分からないアクセスは int）の表し方: 変数の木と同じ（整数は 10 進と 16 進） */
export function valueText(v: unknown): string {
  return display(v, Infinity);
}

/** 例外の回数（API の並びのまま。画面で回数の多い順に並べ替えない） */
export function exceptionRows(counts: Record<string, number>): { name: string; count: number }[] {
  return Object.entries(counts).map(([name, count]) => ({ name, count }));
}

/** swo.trace の誤り → 出す文。まだ設定していない（E_NO_DATA。誤りの符号で見分ける）は誤りでないので null */
export function traceError(e: unknown): string | null {
  return isNoData(e) ? null : errorText(e);
}
