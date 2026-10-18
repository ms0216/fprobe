// パネルの値の表し方（数は 10 進と 16 進、配列・構造体は短く）
export function formatValue(v: unknown): string {
  if (typeof v === "number") return Number.isInteger(v) ? `${v} (0x${(v >>> 0).toString(16).toUpperCase()})` : String(v);
  if (typeof v === "string") return v;
  if (Array.isArray(v)) return `[${v.map(formatValue).join(", ")}]`;
  if (v && typeof v === "object") return `{${Object.entries(v).map(([k, x]) => `${k}: ${formatValue(x)}`).join(", ")}}`;
  return String(v);
}

export const hex32 = (n: number) => `0x${(n >>> 0).toString(16).toUpperCase().padStart(8, "0")}`;

export { placeOf } from "../../lib/debugger";
