// 故障の直前の命令（段階 B §3 の 4）: トレースのバイトの置き場所（ターゲットのメモリのリング・デーモンの PC のファイル）
// から trace.etm.decode で命令の流れを作り、trace.etm.history で止まる直前の命令を読む。中身は API の事実のまま。
// Svelte に依らない
import { errorText, type DaemonConnection } from "./api";

export type EtmSourceForm =
  | { kind: "memory"; address: string; length: string; oldest: string }
  | { kind: "file"; path: string };

export interface EtmRow {
  index: number;
  address: number;
  size?: number;
  mnemonic: string;
  operands?: string;
  function?: string;
  offset?: number;
  file?: string;
  line?: number;
}

export interface EtmMarker {
  kind: string;
  index: number;
  [key: string]: unknown;
}

export interface EtmHistory {
  total: number;
  instructions: EtmRow[];
  markers: EtmMarker[];
  next_address: number | null;
  errors: string[];
  note?: string;
}

const INTEGER = /^(0x[0-9a-f]+|\d+)$/i;

export function parseInteger(text: string): number | null {
  const t = text.trim();
  return INTEGER.test(t) ? Number(t) : null;
}

/** 置き場所の欄から trace.etm.decode の引数（誤りは欄の名前） */
export function decodeParams(form: EtmSourceForm): { params: Record<string, unknown> } | { error: string } {
  if (form.kind === "file") return form.path.trim() ? { params: { path: form.path.trim() } } : { error: "path" };
  const address = parseInteger(form.address);
  if (address === null) return { error: "address" };
  const length = parseInteger(form.length);
  if (length === null || length < 1) return { error: "length" };
  const oldest = form.oldest.trim() === "" ? 0 : parseInteger(form.oldest);
  if (oldest === null) return { error: "oldest" };
  return { params: { address, length, oldest } };
}

export async function readHistory(conn: Pick<DaemonConnection, "call">, params: Record<string, unknown>, count: number):
  Promise<{ history: EtmHistory | null; error: string | null; decodeErrors?: string[]; decodeNote?: string }> {
  try {
    const decoded = (await conn.call("trace.etm.decode" as never, params as never)) as any;
    const history = (await conn.call("trace.etm.history" as never, { count } as never)) as EtmHistory;
    // 解読の誤り（トレース ID の流れが無い 等）は decode の結果にだけある
    return { history, error: null, decodeErrors: decoded?.errors ?? [], decodeNote: decoded?.note };
  } catch (e) {
    return { history: null, error: errorText(e) };
  }
}

/** 最後の命令の後の出来事（その前に実行した命令の数 = 最後の index + 1。止まる原因の例外がここに来る） */
export function trailingMarkers(h: Pick<EtmHistory, "instructions" | "markers">): EtmMarker[] {
  const last = h.instructions.at(-1);
  return last ? markersAt(h.markers, last.index + 1) : [];
}

/** 命令 index の前に起きた出来事（例外・同期 等） */
export function markersAt(markers: EtmMarker[], index: number): EtmMarker[] {
  return markers.filter((m) => m.index === index);
}

/** 命令の数の欄: 1 以上の整数だけ（ほかは null。黙って既定の値に置き換えない） */
export function parseCount(text: string): number | null {
  const v = parseInteger(text);
  return v !== null && v >= 1 ? v : null;
}
