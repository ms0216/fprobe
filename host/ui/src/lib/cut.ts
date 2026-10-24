// 読みの打ち切りを画面に出す材料（Svelte に依らない）。打ち切りは切ったことと使った上限を示す（黙って切らない）。

/** recording.query の結果の打ち切り: limit で切った（truncated）か、長期の記録の新しい分割だけを読んだ（partial。
 *  segments = 読んだ分割の数）。切っていなければ null */
export interface QueryCut {
  shown: number;
  limit: number;
  truncated: boolean;
  segments: number | null;
}

export function queryCut(r: { count?: number; rows?: unknown[]; truncated?: boolean; partial?: boolean; segments?: unknown[] },
                         limit: number): QueryCut | null {
  const truncated = !!r.truncated, partial = !!r.partial;
  if (!truncated && !partial) return null;
  return { shown: r.count ?? r.rows?.length ?? 0, limit, truncated, segments: partial ? r.segments?.length ?? 0 : null };
}

/** 上位 limit で切った表（API が切る前の数 total を返す）。切っていなければ null */
export function topCut(shown: number, total: number | undefined, limit: number): { shown: number; total: number; limit: number } | null {
  return total !== undefined && total > shown ? { shown, total, limit } : null;
}
