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

export async function readUsage(conn: Pick<DaemonConnection, "call">, bounds: Record<string, number | string> = {}):
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
const SYMBOL = /^[A-Za-z_.$][\w.$]*$/;
const SYMBOL_PARAM = { bottom: "limit_symbol", top: "top_symbol" } as const;

/** 範囲（下端・上端）の欄から target.stack.paint / usage の引数。番地（16 進・10 進）か、リンカのシンボルの名前
 *  （下端 → limit_symbol、上端 → top_symbol）。空の欄は送らない（fprobe は名前の慣習でシンボルを選ばないので、
 *  上端が無ければホストが誤りを返す。下端が無ければ上端の下の RAM の区画の終わり） */
export function boundsParams(bottom: string, top: string):
    { params: Record<string, number | string> } | { error: "bottom" | "top" } {
  const params: Record<string, number | string> = {};
  for (const [name, text] of [["bottom", bottom], ["top", top]] as const) {
    const t = text.trim();
    if (t === "") continue;
    if (INTEGER.test(t)) params[name] = Number(t);
    else if (SYMBOL.test(t)) params[SYMBOL_PARAM[name]] = t;
    else return { error: name };
  }
  return { params };
}
