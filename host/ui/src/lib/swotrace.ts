// SWO Trace のパネル（IAR の SWO Trace・STM32CubeIDE の SWV Data Trace / Exception Trace の型）の計算。Svelte に依らない。
// 設定 = swo.configure（例外トレース・ローカルのタイムスタンプ・データトレース = DWT の比較器で変数へのアクセス）、
// 結果 = swo.trace（アクセスの一覧・例外の回数）、戻す = swo.unconfigure。

export interface TraceSettings {
  exceptions: boolean;
  timestamps: boolean;
  variables: { expression: string; access: "read" | "write" | "rw" }[];   // 比較器は 4 つまで
  swoBaud: number;
  coreClockHz: number | null;
}

export const MAX_COMPARATORS = 4;

/** 設定 → swo.configure の引数（空の式は除く） */
export function configureParams(s: TraceSettings): Record<string, unknown> {
  const data = s.variables.filter((v) => v.expression.trim()).slice(0, MAX_COMPARATORS)
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
  value: unknown;
  pc?: number;
  function?: string;
  file?: string;
  line?: number;
}

/** 一覧の終わりの limit 件を読む offset（新しいものを見せる） */
export function tailOffset(total: number, limit: number): number {
  return Math.max(0, total - limit);
}

/** 値の表し方（数は 10 進と 16 進） */
export function valueText(v: unknown): string {
  if (typeof v === "number" && Number.isInteger(v)) return `${v} (0x${(v >>> 0).toString(16).toUpperCase()})`;
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** 例外の回数を多い順に */
export function exceptionRows(counts: Record<string, number>): { name: string; count: number }[] {
  return Object.entries(counts).map(([name, count]) => ({ name, count })).sort((a, b) => b.count - a.count);
}
