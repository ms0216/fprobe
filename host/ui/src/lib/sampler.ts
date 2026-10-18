// DAP サンプラ（変数を止めずに読む FPGA の回路。1 つだけ）の取り合いを画面の言葉で: いま使っている道具をパネルの名前に、
// 使い始めた利用者を操作の記録（activity）から引き、止める操作（API）を添える。Svelte に依らない。
import type { DaemonConnection } from "./api";
import type { Key } from "./i18n";
import type { ActivityEntry } from "./state";

export type SamplerUser = "watch" | "rtos" | "profile" | "rtt" | "clock";

export interface SamplerStatus {
  user: SamplerUser | null;
  release: string | null;            // 止める API（clock は自分で終わる）
  since: number | null;
}

/** 道具 → それを使うパネル（dock の id と表示の名前）と、使い始める API */
export const SAMPLER_USERS: Record<SamplerUser, { panel: string | null; title: Key; start: string }> = {
  watch: { panel: "dataSampling", title: "panel.dataSampling", start: "watch.set" },
  rtos: { panel: "cpuLoad", title: "panel.cpuLoad", start: "rtos.timeline.start" },
  profile: { panel: "profile", title: "panel.profile", start: "profile.start" },
  rtt: { panel: "terminal", title: "panel.terminal", start: "rtt.start" },
  clock: { panel: null, title: "sampler.clock", start: "cycles.clock" },
};

export interface Holder {
  user: SamplerUser;
  panel: string | null;
  title: Key;
  release: string | null;
  client: string | null;             // 使い始めた利用者（記録から。分からなければ null）
  role: string | null;
}

/** 使っている道具の様子（空きなら null）。activity は操作の記録（新しいものが後ろ） */
export function holderOf(status: SamplerStatus | null, activity: ActivityEntry[]): Holder | null {
  const user = status?.user;
  if (!user || !(user in SAMPLER_USERS)) return null;
  const info = SAMPLER_USERS[user];
  const started = [...activity].reverse().find((e) => e.method === info.start && !e.error);
  return { user, panel: info.panel, title: info.title, release: status!.release,
           client: started?.client ?? null, role: started?.role ?? null };
}

/** 自分（mine）が使いたいのに、ほかの道具が使っている */
export function blockedBy(holder: Holder | null, mine: SamplerUser): Holder | null {
  return holder && holder.user !== mine ? holder : null;
}

type Conn = Pick<DaemonConnection, "call">;

export async function readSampler(conn: Conn): Promise<SamplerStatus | null> {
  try {
    return await conn.call("probe.sampler.status", {}, { quiet: true });
  } catch {
    return null;                                          // 古いデーモン
  }
}
