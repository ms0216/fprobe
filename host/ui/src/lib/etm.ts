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
  errors_total?: number;        // 切る前の誤りの数（errors は先頭 errors_limit 件）
  errors_truncated?: boolean;
  errors_limit?: number;
  note?: string;
}

/** 切った一覧の事実（表示した数・総数・上限）。切っていなければ null */
export interface Cut { shown: number; total: number; limit: number }

function errorsCut(r: { errors?: string[]; errors_total?: number; errors_truncated?: boolean; errors_limit?: number } | null): Cut | null {
  return r?.errors_truncated ? { shown: r.errors?.length ?? 0, total: r.errors_total ?? 0, limit: r.errors_limit ?? 0 } : null;
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
  const oldest = parseInteger(form.oldest);                       // 空を 0 とみなさない（画面の既定を持たない）
  if (oldest === null) return { error: "oldest" };
  return { params: { address, length, oldest } };
}

/** ETM の構成の欄（commitOpt1: "" / "1" / "0"、VMID・Context ID のバイト数） */
export interface EtmConfigForm { commitOpt1: string; vmidBytes: string; cidBytes: string }

/** 構成の欄から etm_config。全部空なら渡さない（API は trace.etm.start で読んだ ID レジスタを使い、無ければ断る）。
 *  1 つでも入れたら 3 つとも要る（足りない欄の名前を誤りに） */
export function configParam(form: EtmConfigForm): { params: Record<string, unknown> } | { error: string } {
  if (!form.commitOpt1 && !form.vmidBytes.trim() && !form.cidBytes.trim()) return { params: {} };
  if (form.commitOpt1 !== "1" && form.commitOpt1 !== "0") return { error: "commit_opt1" };
  const vmid = parseInteger(form.vmidBytes);
  if (vmid === null) return { error: "vmid_bytes" };
  const cid = parseInteger(form.cidBytes);
  if (cid === null) return { error: "cid_bytes" };
  return { params: { etm_config: { commit_opt1: form.commitOpt1 === "1", vmid_bytes: vmid, cid_bytes: cid } } };
}

export async function readHistory(conn: Pick<DaemonConnection, "call">, params: Record<string, unknown>, count: number):
  Promise<{ history: EtmHistory | null; error: string | null; decodeErrors?: string[]; decodeErrorsCut?: Cut | null;
            decodeNote?: string }> {
  try {
    const decoded = (await conn.call("trace.etm.decode" as never, params as never)) as any;
    const history = (await conn.call("trace.etm.history" as never, { count } as never)) as EtmHistory;
    // 解読の誤り（トレース ID の流れが無い 等）は decode の結果にだけある。切ったときは総数と上限も
    return { history, error: null, decodeErrors: decoded?.errors ?? [], decodeErrorsCut: errorsCut(decoded),
             decodeNote: decoded?.note };
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
