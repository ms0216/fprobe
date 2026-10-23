// ヒープ（段階 B §3 の 6）: heap.scan（使用量・空き・最大の空き・断片化・壊れ）と heap.trace.*（malloc / free の記録:
// 二重の free・呼び出し元ごとの生きている確保）。値は API の事実のまま。Svelte に依らない
import { errorText, type DaemonConnection } from "./api";

export interface ScanForm {
  allocator: "" | "freertos" | "newlib_nano" | "newlib";
  live: boolean;
  compare: boolean;
}

/** heap.scan の引数（既定の値は送らない） */
export function scanParams(form: ScanForm): Record<string, unknown> {
  return { ...(form.allocator ? { allocator: form.allocator } : {}), ...(form.live ? { live: true } : {}),
           ...(form.compare ? { compare: true } : {}) };
}

export async function readReport(conn: Pick<DaemonConnection, "call">): Promise<{ report: any; error: string | null }> {
  try {
    return { report: await conn.call("heap.trace.report" as never, {} as never, { quiet: true }), error: null };
  } catch (e) {
    return { report: null, error: errorText(e) };
  }
}

const hex = (n: number) => `0x${(n >>> 0).toString(16).toUpperCase().padStart(8, "0")}`;

/** 走査（kind・address・detail）と記録（kind・pointer・caller・detail）の問題を 1 行に。場所が無ければ出さない */
export function issueText(i: { kind: string; address?: number | null; pointer?: number | null; detail?: string;
                               caller?: string | null }): string {
  const where = i.address ?? i.pointer;
  return [i.kind, where != null ? hex(where) : null, i.detail ?? null, i.caller ? `(${i.caller})` : null]
    .filter((x) => x !== null && x !== "").join(" ");
}
