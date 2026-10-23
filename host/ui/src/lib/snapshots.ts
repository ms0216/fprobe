// 故障の状態の保存（段階 B §3 の 2）: 保存した状態（snapshot.*）の一覧と、いま開いている状態。読み取りは quiet
// （操作の記録に入れない）。Svelte に依らない
import { errorText, type DaemonConnection } from "./api";

export interface SnapshotRow {
  name: string;
  path: string;
  bytes?: number;
  created?: string;
  pc?: number;
  where?: string;
  reason?: string;
  note?: string;
  error?: string;
}

export interface OpenSnapshot {
  open: true;
  path: string;
  [key: string]: unknown;
}

export async function readSnapshots(conn: Pick<DaemonConnection, "call">):
  Promise<{ rows: SnapshotRow[]; open: OpenSnapshot | null; error: string | null }> {
  let rows: SnapshotRow[] = [];
  let error: string | null = null;
  try {
    rows = ((await conn.call("snapshot.list" as never, {} as never, { quiet: true })) as any).snapshots ?? [];
  } catch (e) {
    error = errorText(e);
  }
  let open: OpenSnapshot | null = null;
  try {
    const info = (await conn.call("snapshot.info" as never, {} as never, { quiet: true })) as any;
    open = info?.open ? info : null;
  } catch {
    open = null;
  }
  return { rows, open, error };
}
