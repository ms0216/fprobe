// 記録の行の表示用の整形
import { t } from "./i18n";
import { formatDuration } from "./time";
import type { ActivityEntry } from "./state";

export function clock(iso: string): string {
  return iso.slice(11, 19);
}

/** 所要時間（lib/time.ts の時間の長さ、有効数字 3 桁） */
export function seconds(s: number | undefined): string {
  return s === undefined ? "" : formatDuration(s, 3);
}

/** 周波数（Hz）→ "100.286 MHz"・"200.571 kHz"・"900 Hz"。有効数字 6 桁で丸め、末尾の 0 は出さない
 *  （100.285714 MHz ÷ 500 等を割ったそのままだと浮動小数の誤差の桁が出る） */
export function frequency(hz: number): string {
  const [v, unit] = hz >= 1e6 ? [hz / 1e6, "MHz"] : hz >= 1e3 ? [hz / 1e3, "kHz"] : [hz, "Hz"];
  return `${Number(v.toPrecision(6))} ${unit}`;
}

/** 値の普通の表記（時間軸の値の段・ツールチップ）: 整数はそのまま、小数は有効数字 6 桁（整数部はすべて）。指数にしない */
export function plainNumber(v: number): string {
  if (!Number.isFinite(v)) return String(v);
  if (Number.isInteger(v)) return v.toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: 0 });
  const abs = Math.abs(v);
  if (abs >= 1) {
    const whole = Math.floor(Math.log10(abs)) + 1;                  // 整数部の桁
    return v.toLocaleString("en-US", { useGrouping: false, maximumFractionDigits: Math.max(0, 6 - whole) });
  }
  return v.toLocaleString("en-US", { useGrouping: false, maximumSignificantDigits: 6 });
}

export function outcome(e: ActivityEntry): string {
  if (e.error !== undefined) return `${t("activity.error")}: ${e.error}`;
  if (e.result === null || e.result === undefined) return t("activity.done");
  const text = JSON.stringify(e.result);
  return text.length > 80 ? `${text.slice(0, 80)}…` : text;            // 切ったら印を付ける（全部は詳細の欄に出る）
}
