// 接続の窓（Connect to Target）の欄の最初の値と、前に使ったターゲット名の一覧。Svelte に依らない。
// 最初の値の出どころは API の仕様（target.connect の examples）。画面のコードに値を書かない（CLAUDE.md）

/** 仕様の値 → 欄の文字（ターゲット名・MHz）。仕様に無ければ空 */
export function connectDefaults(suggested: (method: string, param: string) => unknown): { target: string; mhz: string } {
  const target = suggested("target.connect", "target");
  const hz = suggested("target.connect", "frequency_hz");
  return { target: typeof target === "string" ? target : "",
           mhz: typeof hz === "number" && hz > 0 ? String(hz / 1e6) : "" };
}

/** 前に接続に使ったターゲット名（新しい順・重複なし・上限 RECENT_TARGETS） */
export const RECENT_TARGETS = 8;

export function recentTargets(saved: string): string[] {
  try {
    const v = JSON.parse(saved);
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string" && x.trim() !== "").slice(0, RECENT_TARGETS) : [];
  } catch {
    return [];
  }
}

export function rememberTarget(list: string[], target: string): string[] {
  const t = target.trim();
  if (!t) return list;
  return [t, ...list.filter((x) => x !== t)].slice(0, RECENT_TARGETS);
}
