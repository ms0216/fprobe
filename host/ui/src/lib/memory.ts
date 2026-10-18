// Memory のパネルの計算（Svelte に依らない）: 番地の式 → 番地、書く値 → バイト列。
import type { DaemonConnection } from "./api";

type Call = DaemonConnection["call"];

/** 番地の欄の式を番地にする（Ozone・Keil の Memory の Address と同じ考え）:
 *  0x… と数字だけの並びは 16 進の番地（J-Link Commander の mem と同じ。20000000 = 0x20000000）。C の式は debug.evaluate で、ポインタなら指す先、メモリ上の変数（配列・構造体・
 *  基本型）ならその番地、それ以外の整数（`0x20000000 + 16` 等）ならその値。式にならないもの（関数名・"ファイル:行"）は
 *  debug.symbol.info で引く。frame は Call Stack で選んだフレーム（ローカル変数を使える） */
export async function resolveAddress(call: Call, text: string, frame = 0): Promise<number> {
  const s = text.trim();
  if (/^0x[0-9a-f]+$/i.test(s)) return parseInt(s.slice(2), 16);
  if (/^\d+$/.test(s)) return parseInt(s, 16);
  const bareHex = /^[0-9a-f]+$/i.test(s) ? parseInt(s, 16) : null;     // 2000A0FC。同じ名前の変数があれば変数
  let evaluated: { address?: number; value?: unknown } | null = null;
  try {
    evaluated = await call("debug.evaluate", { expression: s, frame }, { quiet: true });
  } catch {
    evaluated = null;
  }
  if (evaluated) {
    const v = evaluated.value as any;
    if (v && typeof v === "object" && typeof v.pointer === "number") return v.pointer;
    if (typeof evaluated.address === "number") return evaluated.address;
    if (typeof v === "number" && Number.isInteger(v)) return v >>> 0;
  }
  try {
    const info = await call("debug.symbol.info", { location: s }, { quiet: true });
    return info.address;
  } catch (e) {
    if (bareHex !== null) return bareHex;
    throw e;
  }
}

/** 16 進の欄の文字 → 単位（8/16/32 ビット）の値。範囲の外・16 進でなければ null */
export function parseCell(text: string, unit: number): number | null {
  const s = text.trim().replace(/^0x/i, "");
  if (!/^[0-9a-f]{1,8}$/i.test(s)) return null;
  const v = parseInt(s, 16);
  return v < 2 ** unit ? v : null;
}

/** 単位の値 → target.memory.write の data_hex（リトルエンディアン） */
export function unitHex(value: number, unit: number): string {
  let out = "";
  for (let i = 0; i < unit / 8; i++) out += ((value >>> (8 * i)) & 0xff).toString(16).padStart(2, "0");
  return out;
}

/** 行の単位の値（8/16/32 ビット）→ バイトの並び（リトルエンディアン。文字の列の 1 文字 = 1 バイト） */
export function rowBytes(values: number[], unit: number): number[] {
  return values.flatMap((v) => Array.from({ length: unit / 8 }, (_, i) => (v >>> (8 * i)) & 0xff));
}

/** 文字の列の編集（Ozone・VS Code の Hex Editor の右の列）: 前の表示 shown（表示できないバイトは "."）と編集後の
 *  edited を比べ、変わった文字のバイトだけを書く。"." のまま残した所は元のバイトを保つ。書く範囲（最初に変わった
 *  バイトから最後まで。間は元のバイト）を返す。変わらなければ null。ASCII（0x20〜0x7E）以外の文字は書けない（誤り） */
export function textEdit(bytes: number[], shown: string, edited: string): { offset: number; data_hex: string } | null {
  const next = bytes.slice();
  let first = -1, last = -1;
  for (let i = 0; i < Math.min(bytes.length, edited.length); i++) {
    if (edited[i] === shown[i]) continue;
    const code = edited.charCodeAt(i);
    if (code < 0x20 || code > 0x7e) throw new Error(`0x20-0x7E: ${JSON.stringify(edited[i])}`);
    next[i] = code;
    if (first < 0) first = i;
    last = i;
  }
  if (first < 0) return null;
  const data_hex = next.slice(first, last + 1).map((b) => b.toString(16).padStart(2, "0")).join("");
  return { offset: first, data_hex };
}
