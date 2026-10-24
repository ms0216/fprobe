// Power（J-Link の Power・Ozone の Power Graph に当たる）の見せ方。Svelte に依らない。統計・いちばん低い点・
// 指定した電圧以下の区間はデーモン（probe.voltage_history・recording.voltage）が出す。ここは描く系列・CSV・書式だけ。
import type { Series } from "./chart";

export interface VoltageReading {
  volts: number | null;              // 測れない（over_range・未計測）なら null
  over_range: boolean;
  live: boolean;                     // 補助マイコンが計測値を送っている
  samples?: number;
  errors?: number;
}

export const SPANS_S = [10, 60, 300, 1800];                 // 窓の長さ（秒）

export interface HistoryReading {
  time: number;                      // デーモンが読んだ時刻（UNIX 秒）
  volts: number | null;
  over_range: boolean;
  live: boolean;
}

/** probe.voltage_history の読みを描く系列（x = 最初の読みからの秒。測れなかった読みで線を切る） */
export function historySeries(readings: HistoryReading[], name: string): Series {
  const t0 = readings.length ? readings[0].time : 0;
  return { name, x: readings.map((r) => r.time - t0), y: readings.map((r) => measuredVolts(r) ?? NaN), unit: "V" };
}

/** CSV（時刻は ISO、電圧 V。測れなかった読みは空） */
export function historyCsv(readings: HistoryReading[]): string {
  const lines = ["time,volts,over_range,live"];
  for (const r of readings) lines.push(`${new Date(r.time * 1000).toISOString()},${measuredVolts(r) ?? ""},${r.over_range},${r.live}`);
  return `${lines.join("\n")}\n`;
}

/** 記録の電圧の出来事（recording.find の stream aux・kind mv。b = mV）を時刻（タイムベース）と V の列に */
export function recordingVolts(events: { t: number; b: number; kind?: string }[]): { x: number[]; y: number[] } {
  const rows = events.filter((e) => e.kind === undefined || e.kind === "mv").slice().sort((a, b) => a.t - b.t);
  return { x: rows.map((e) => e.t), y: rows.map((e) => e.b / 1000) };
}

/** いまの電圧（補助マイコンが計測値を送っていて、範囲の中のときだけ。ほかは null = 「データなし」。
 *  補助の Pico が無いと volts = 0・live = false が返る。0 V と見せない） */
export function measuredVolts(r: Pick<VoltageReading, "volts" | "over_range" | "live"> | null | undefined): number | null {
  return r && r.live && !r.over_range && typeof r.volts === "number" ? r.volts : null;
}

/** 電圧の表し方（3 桁の V） */
export function formatVolts(v: number | null): string {
  return v !== null && Number.isFinite(v) ? `${v.toFixed(3)} V` : "—";
}

type Call = (method: string, params: Record<string, unknown>, opts?: { quiet?: boolean }) => Promise<any>;

/** recording.find を t0 で読み足す。API の count（条件に合う数）を読み終えるまで、maxPages ページまで。
 *  total = 最初の count、complete = 全部読んだ（ページの上限で止めたら false） */
export async function findPages(call: Call, query: Record<string, unknown>, maxPages: number):
  Promise<{ events: { t: number; b: number; kind: string }[]; total: number; complete: boolean }> {
  const events: { t: number; b: number; kind: string }[] = [];
  let total = 0, t0: number | undefined;
  for (let page = 0; page < maxPages; page++) {
    const r = await call("recording.find", { query: { ...query, ...(t0 !== undefined ? { t0 } : {}) } }, { quiet: true });
    const count = Number(r?.count ?? 0);
    if (page === 0) total = count;
    const got = r?.events ?? [];
    events.push(...got);
    if (!got.length || got.length >= count) return { events, total, complete: true };
    t0 = got[got.length - 1].t + 1;
  }
  return { events, total, complete: false };
}
