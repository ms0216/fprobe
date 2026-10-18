// ほかの利用者（AI・別の窓・スクリプト）の変更を知る口。ビューの読み直しのきっかけはここを通す。Svelte に依らない。
//
// - onCalls: 操作の記録の通知（activity）で、挙げたメソッド（完全一致）の操作が終わったとき。前方一致（"recording."
//   等）は使わない: 読み直しの読みが記録に残るメソッドに当たると、それがまた通知になって読み直しが止まらない。
//   きっかけにするのは状態を変える操作だけにし、読み直しは quiet（記録に残らない）で読む。
// - onRecording: 記録を開いた・始めた・止めた（通知 recording。だれが変えても届く。操作の記録の文字列に頼らない）。
import type { DaemonConnection } from "./api";

type Conn = Pick<DaemonConnection, "on">;

/** 通知 recording の中身（action = open / start / stop、name = 記録の名前、client = 変えた利用者） */
export interface RecordingChange {
  action: "open" | "start" | "stop";
  name: string | null;
  client?: { id: number; name: string; role: string };
}

export interface CallDone {
  method: string;
  params?: unknown;
  client?: string;
  error?: string;
}

/** methods のどれかの操作が終わったら fn（購読をやめる関数を返す。$effect の後始末にそのまま使える） */
export function onCalls(conn: Conn, methods: readonly string[], fn: (e: CallDone) => void): () => void {
  const wanted = new Set(methods);
  return conn.on("activity", (e: CallDone) => {
    if (wanted.has(e.method)) fn(e);
  });
}

/** 記録を開いた・始めた・止めたら fn */
export function onRecording(conn: Conn, fn: (e: RecordingChange) => void): () => void {
  return conn.on("recording", fn);
}
