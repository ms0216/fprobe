// 時間軸の視点まわりの純関数（状態を持たない）: 時刻 0 の位置・記録中に端を追う視点・描く段の指定・時刻へ飛ぶときの視点。
// 見せ方の位置合わせだけで、計算・判定の基準は持たない。state.ts（TimelineState）が使う。
import type { View } from "../waveform";
import type { RowSpec } from "./compose";
import type { GroupDef, VisibleRow } from "./rows";
import type { Reveal, TimelineSnapshot } from "./types";

/** 今の源の時刻 0 の位置（捕捉・記録の始め。lib/time.ts） */
export function originOf(s: Pick<TimelineSnapshot, "source" | "origins">): number {
  return s.origins[s.source]?.t0 ?? 0;
}

/** 伸びた範囲で端を追いかける視点: 全体を見ていれば新しい全体、そうでなければ幅を保って終わりを端に */
export function followView(view: View, before: [number, number], extent: [number, number]): View {
  if (view.start <= before[0] && view.end >= before[1]) return { start: extent[0], end: extent[1] };
  const width = view.end - view.start;
  return { start: Math.max(extent[0], extent[1] - width), end: extent[1] };
}

/** 描く段 → Worker への段の指定 */
export function specsOf(rows: VisibleRow[]): RowSpec[] {
  return rows.map((r) => ({ id: r.def.id, type: r.def.type, color: r.def.color }));
}

/** 縮図の段: 記録の stream のまとまり（無ければロジアナのチャンネル全部。切らない） */
export function overviewSpecs(groups: GroupDef[]): RowSpec[] {
  const streams = groups.filter((g) => g.id !== "logic").map((g) => ({ id: `group:${g.id}`, type: "group" as const,
                                                                         color: 0 }));
  if (streams.length) return streams;
  return (groups.find((g) => g.id === "logic")?.rows ?? [])
    .map((r) => ({ id: r.id, type: r.type, color: r.color }));
}

/** 移動先の時刻（源の単位。µs で指定されていれば換算。無ければ -∞） */
export function revealTicks(r: Reveal, tickS: number): number {
  if (r.t !== undefined) return r.t;
  return r.us !== undefined && tickS > 0 ? Math.round(r.us * 1e-6 / tickS) : -Infinity;
}

/** 時刻へ飛ぶ計画: 選ぶ時刻 t と、視点の変え方（区間なら前後に余白を足した視点、画面の外か視点が空なら "center"、
 *  そのままで足りれば null）。移動先が分からなければ null 全体 */
export function revealPlan(r: Reveal, extent: [number, number], tickS: number, view: View):
    { t: number; view: View | "center" | null } | null {
  const ticksOf = (us: number) => (tickS > 0 ? Math.round(us * 1e-6 / tickS) : NaN);
  const raw = r.t ?? (r.us !== undefined ? ticksOf(r.us) : NaN);
  if (!Number.isFinite(raw)) return null;
  const rawEnd = r.tEnd ?? (r.usEnd !== undefined ? ticksOf(r.usEnd) : raw);
  const clamp = (x: number) => Math.min(extent[1] - 1, Math.max(extent[0], Math.round(x)));
  const t = clamp(raw), tEnd = Math.max(t, clamp(rawEnd));
  if (tEnd > t) {
    // 区間: 前後に区間の半分ずつの余白（幅は 1 単位 × 画素の数を下限に）
    const pad = Math.max((tEnd - t) / 2, 1);
    const start = Math.max(extent[0], Math.floor(t - pad)), end = Math.min(extent[1], Math.ceil(tEnd + pad));
    return { t, view: { start, end: Math.max(end, start + 1) } };
  }
  return { t, view: t < view.start || t >= view.end || view.end <= view.start ? "center" : null };
}
