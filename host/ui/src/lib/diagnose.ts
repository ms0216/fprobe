// 接続の診断（段階 B §3 の 1）: target.diagnose（電源 → ピン → SWD/JTAG → DP → AP → コア → 部品 → 保護の手順ごとの
// status と message、結論、提案の接続の引数）と probe.pins.read（ピンの状態）を読む。順も中身も API の事実のまま。Svelte に依らない
import { errorText, type DaemonConnection } from "./api";

export interface DiagnoseStep {
  name: string;
  status: "ok" | "warn" | "fail" | "skip";
  message: string;
}

export interface DiagnoseReport {
  ok: boolean;
  verdict: string;
  frequency_hz: number;
  suggested_connect: Record<string, unknown> | null;
  steps: DiagnoseStep[];
}

export interface Pins {
  swclk: number;
  swdio: number;
  nreset: number;
  not_reported?: string[];
}

export async function diagnose(conn: Pick<DaemonConnection, "call">, frequencyHz: number):
  Promise<{ report: DiagnoseReport | null; pins: Pins | null; error: string | null }> {
  let report: DiagnoseReport | null = null;
  let error: string | null = null;
  try {
    report = await conn.call("target.diagnose" as never, { frequency_hz: frequencyHz } as never);
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

/** 接続の周波数の欄（MHz）を Hz に。正の数でなければ null（欄の誤りとして出し、デーモンに送らない） */
export function mhzToHz(text: string): number | null {
  const t = text.trim();
  const v = t === "" ? NaN : Number(t);
  return Number.isFinite(v) && v > 0 ? Math.round(v * 1e6) : null;
}
