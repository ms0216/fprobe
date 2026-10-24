// Activity（AI と人の操作の記録）の計算: 役割の絞り込み（AI / Human / Script / Hook）・誤りだけ、行 ↔ 記録の時刻
// （記録中のデーモンは操作を記録の stream host の call として、操作の番号 seq と一緒に入れる）。Svelte に依らない。
import type { DaemonConnection } from "./api";
import { queryCut, type QueryCut } from "./cut";
import { nearestAtOrBefore } from "./timeline/events";
import type { ActivityEntry } from "./state";

type Call = DaemonConnection["call"];

export const ROLES = ["ai", "human", "script", "hook"] as const;
export type RoleFilter = (typeof ROLES)[number] | "";

/** 行の役割: フックの動作はきっかけの利用者の role で呼ばれるが、別に分ける（デーモンが記録したつながり方 via = hook。
 *  利用者の名前の形では決めない） */
export function roleOf(e: Pick<ActivityEntry, "role" | "via">): (typeof ROLES)[number] | string {
  return e.via === "hook" ? "hook" : e.role;
}

export function filterActivity(entries: ActivityEntry[], f: { who?: string; role?: RoleFilter; errorsOnly?: boolean }):
  ActivityEntry[] {
  return entries.filter((e) => (!f.who || e.client === f.who) && (!f.role || roleOf(e) === f.role)
    && (!f.errorsOnly || e.error !== undefined));
}

/** 操作の鍵: 番号 seq は起動ごとに振り直されるので、記録した時刻（time、ISO）と組にして別の起動の操作と混ぜない */
export const callKey = (e: { seq: number; time: string }) => `${e.seq}@${e.time}`;

/** 記録の操作の索引（操作の鍵 ↔ 記録の時刻）。いま読んでいる記録の host.call の [時刻, seq, time] から作る */
export class HostCallIndex {
  readonly times: number[] = [];
  readonly keys: string[] = [];
  private byKey = new Map<string, number>();

  constructor(rows: [number, unknown, unknown][]) {
    for (const [t, seq, time] of [...rows].sort((a, b) => a[0] - b[0])) {
      if (typeof seq !== "number" || typeof time !== "string") continue;
      const key = callKey({ seq, time });
      this.times.push(t);
      this.keys.push(key);
      this.byKey.set(key, t);
    }
  }

  /** 操作の記録の時刻（記録に無ければ undefined） */
  timeOf(e: { seq: number; time: string }): number | undefined {
    return this.byKey.get(callKey(e));
  }

  /** 時刻 t 以前でいちばん近い操作の鍵（無ければ undefined） */
  keyAt(t: number): string | undefined {
    const i = nearestAtOrBefore(this.times, t);
    return i < 0 ? undefined : this.keys[i];
  }

  get size(): number {
    return this.times.length;
  }
}

export const HOST_CALLS_SQL = "SELECT t, json_extract(body, '$.seq'), json_extract(body, '$.time') FROM events "
  + "WHERE stream = 'host' AND kind = 'call' AND json_extract(body, '$.seq') IS NOT NULL ORDER BY t";
/** 読む操作の上限（recording.query の最大。超えたら cut で示す） */
export const HOST_CALLS_LIMIT = 100_000;

/** いま読んでいる記録の操作の索引を読む（記録が無ければ null）。上限まで（recording.query）。切ったら cut */
export async function loadHostCalls(call: Call): Promise<{ name: string; index: HostCallIndex; cut: QueryCut | null } | null> {
  try {
    const status = await call("recording.status", {}, { quiet: true });
    if (!status?.name) return null;
    const out = await call("recording.query", { sql: HOST_CALLS_SQL, limit: HOST_CALLS_LIMIT }, { quiet: true });
    return { name: status.name, index: new HostCallIndex(out.rows ?? []), cut: queryCut(out, HOST_CALLS_LIMIT) };
  } catch {
    return null;
  }
}
