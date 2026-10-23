// 主スタック（MSP）の使用量（段階 B §3 の 5）: 使っていない領域を塗り（target.stack.paint）、塗りの残りから使用量を読む
// （target.stack.usage）。値は API の事実のまま（割合も API の fraction）。Svelte に依らない
import { errorText, type DaemonConnection } from "./api";

export interface StackUsage {
  bottom?: number;
  top?: number;
  size?: number;
  used?: number;
  untouched?: number;
  fraction?: number;
  sp?: number | null;                    // 走行中は null（読めない）
  current?: number | null;               // 止まっているときの今の深さ
  overflow?: boolean;
  reached_bottom?: boolean;
  warning?: string | null;
  notes?: string[];
  painted?: boolean;
  [key: string]: unknown;
}

export async function readUsage(conn: Pick<DaemonConnection, "call">, bounds: Record<string, number> = {}):
  Promise<{ usage: StackUsage | null; error: string | null }> {
  try {
    return { usage: (await conn.call("target.stack.usage" as never, bounds as never, { quiet: true })) as StackUsage, error: null };
  } catch (e) {
    return { usage: null, error: errorText(e) };
  }
}

export function usagePercent(u: Pick<StackUsage, "fraction">): number | null {
  return typeof u.fraction === "number" ? Math.round(u.fraction * 1000) / 10 : null;
}

const INTEGER = /^(0x[0-9a-f]+|\d+)$/i;

/** 範囲（下端・上端）の欄から target.stack.paint / usage の引数。空の欄は送らない（ELF の _estack 等から）。
 *  ELF にその名前が無い（Zephyr 等）ときは、人が明示して渡す */
export function boundsParams(bottom: string, top: string): { params: Record<string, number> } | { error: "bottom" | "top" } {
  const params: Record<string, number> = {};
  for (const [name, text] of [["bottom", bottom], ["top", top]] as const) {
    const t = text.trim();
    if (t === "") continue;
    if (!INTEGER.test(t)) return { error: name };
    params[name] = Number(t);
  }
  return { params };
}
