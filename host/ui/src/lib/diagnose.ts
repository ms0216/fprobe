// 接続の診断（段階 B §3 の 1）: target.diagnose（電源 → ピン → SWD/JTAG → DP → AP → コア → 部品 → 保護の手順ごとの
// status（ok 読めた / fail 読めなかった / skip）と message、DP に届いたか・応答した速さ・部品の候補）と probe.pins.read
// （ピンの状態）を読む。順も中身も API の事実のまま（良し悪し・結論・おすすめの接続は本体も画面も決めない）。Svelte に依らない
import { errorText, type DaemonConnection } from "./api";

export interface DiagnoseStep {
  name: string;
  status: "ok" | "fail" | "skip";
  message: string;
}

export interface DiagnoseReport {
  reached: boolean;                  // DP に届き、デバッグ電源の要求に応答があった
  frequencies_hz: number[];          // 試した速さ
  protocol: string | null;           // 応答した線
  frequency_hz: number | null;       // 応答した速さ
  part: { name: string; candidates: string[] } | null;
  steps: DiagnoseStep[];
}

export interface Pins {
  swclk: number;
  swdio: number;
  nreset: number;
  not_reported?: string[];
}

export async function diagnose(conn: Pick<DaemonConnection, "call">, frequenciesHz: number[]):
  Promise<{ report: DiagnoseReport | null; pins: Pins | null; error: string | null }> {
  let report: DiagnoseReport | null = null;
  let error: string | null = null;
  try {
    report = await conn.call("target.diagnose" as never, { frequencies_hz: frequenciesHz } as never);
  } catch (e) {
    error = errorText(e);
  }
  let pins: Pins | null = null;
  try {
    pins = await conn.call("probe.pins.read" as never, {} as never);
  } catch {
    pins = null;                                       // ピンを返さない機器（古い回路）: 出さない
  }
  return { report, pins, error };
}

export function pinsText(p: Pins): string {
  return `SWCLK ${p.swclk} · SWDIO ${p.swdio} · nRESET ${p.nreset}`;
}

/** 診断で試す速さの欄（MHz のカンマ区切り。書いた順に試す）を Hz の並びに。どれかが正の数でなければ null */
export function mhzListToHz(text: string): number[] | null {
  const out = text.split(",").map(mhzToHz);
  return out.every((v): v is number => v !== null) ? out : null;
}

/** 接続の周波数の欄（MHz）を Hz に。正の数でなければ null（欄の誤りとして出し、デーモンに送らない） */
export function mhzToHz(text: string): number | null {
  const t = text.trim();
  const v = t === "" ? NaN : Number(t);
  return Number.isFinite(v) && v > 0 ? Math.round(v * 1e6) : null;
}
