/// <reference lib="webworker" />
// 間引きの Web Worker: タイルを持ち、視点ごとに 1 フレームの材料を作って返す（描画のスレッドを止めない）。
// タイルへの問い合わせ（pick・seek・anomalies）も答える。通信の形は decimator.ts の WorkerRequest / WorkerReply。
import { TileStore, type WorkerReply, type WorkerRequest } from "./decimator";
import type { TimelineFrame } from "./compose";

const store = new TileStore();
const scope = self as unknown as DedicatedWorkerGlobalScope;

scope.onmessage = (e: MessageEvent<WorkerRequest>) => {
  const m = e.data;
  const result = store.answer(m);
  if (!("seq" in m)) return;                                  // タイル・捨てる: 返事は要らない
  if (m.type === "frame" && result) {
    const frame = result as TimelineFrame;
    scope.postMessage({ seq: m.seq, result: frame } satisfies WorkerReply, [frame.data.buffer, frame.modes.buffer]);
    return;
  }
  scope.postMessage({ seq: m.seq, result: result ?? null } satisfies WorkerReply);
};
