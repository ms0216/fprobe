// Data Sampling の周期の読み 1 回分: 観測している間、値（watch.values）と描く式の列（watch.series）を読み、サンプル
// の入れ物（SampleStore）に足す。XY の組（watch.xy）・条件での取得の途中の状態（watch.capture）は、要るときだけ読む。
// Svelte に依らない。呼び出しの間隔・重ならない約束・失敗の扱いは呼び出し元。
import type { DaemonConnection } from "./api";
import type { SampleStore } from "./sampling";

export const SERIES_READ = 4096;          // watch.series の last の最大（API の上限）

export interface PollPlan {
  names: string[];                        // 観測していて、この画面の式
  plotted: string[];                      // グラフに描く式（列を読む）
  xy: { x: string; y: string; maxDtUs: string } | null;   // XY を描くとき。maxDtUs が空なら渡さない
  captureOpen: boolean;                   // 条件での取得がまだ終わっていない（状態を読み直す）
}

export async function pollWatch(call: DaemonConnection["call"], store: SampleStore, plan: PollPlan):
    Promise<{ xy?: any; capture?: any }> {
  const last = SERIES_READ;                      // API の最大。読まなかった区間は from_us で分かる（gaps）
  const [values, ...series] = await Promise.all([
    call("watch.values", {}, { quiet: true }),
    ...plan.plotted.map((name) => call("watch.series", { name, last, after_us: store.lastTime(name) }, { quiet: true })),
  ]);
  series.forEach((s: any) => store.merge(s.name, s.samples ?? [], Array.isArray(s.gap) ? (s.gap as [number, number]) : null));
  for (const v of (values as any)?.values ?? []) {
    if (plan.names.includes(v.name) && v.time_us !== null) store.note(v.name, { ...v, time_us: v.time_us });
  }
  const out: { xy?: any; capture?: any } = {};
  if (plan.xy) {
    const { x, y, maxDtUs: dt } = plan.xy;
    out.xy = await call("watch.xy", { x, y, last, ...(dt !== "" && Number.isFinite(Number(dt)) ? { max_dt_us: Number(dt) } : {}) },
                        { quiet: true });
  }
  if (plan.captureOpen) out.capture = await call("watch.capture", {}, { quiet: true });
  return out;
}
