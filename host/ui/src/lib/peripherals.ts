// Peripherals のパネルの計算（Svelte に依らない）: SVD のフィールドの定義から、値を書く部品の種類を決める。

export interface EnumDef {
  name: string;
  value: number | null;       // isDefault は null
}

export interface FieldDef {
  name: string;
  bit_offset: number;
  bit_width: number;
  access: string;             // read-only / write-only / read-write / writeOnce / read-writeOnce
  description?: string;
  enums: EnumDef[];
}

export interface RegisterDef {
  path: string;
  name: string;
  address: number;
  size: number;
  access: string;
  description?: string;
  read_side_effects: boolean;
  fields: FieldDef[];
}

/** 書く部品: 読むだけ / チェックボックス（1 ビット）/ 選択（値の名前がある）/ 数 */
export type FieldControl = "readonly" | "check" | "select" | "number";

export function fieldControl(f: FieldDef | undefined, register?: RegisterDef): FieldControl {
  if (!f) return "readonly";
  const access = f.access || register?.access || "read-write";
  if (access === "read-only") return "readonly";
  if (f.enums.some((e) => e.value !== null)) return "select";
  return f.bit_width === 1 ? "check" : "number";
}

/** 数の欄の文字 → フィールドの値（10 進・0x・0b）。幅に入らなければ null */
export function parseField(text: string, width: number): number | null {
  const s = text.trim().toLowerCase();
  const v = /^0x[0-9a-f]+$/.test(s) ? parseInt(s.slice(2), 16) : /^0b[01]+$/.test(s) ? parseInt(s.slice(2), 2)
    : /^\d+$/.test(s) ? parseInt(s, 10) : NaN;
  return Number.isFinite(v) && v >= 0 && v < 2 ** width ? v : null;
}

/** レジスタの欄の文字（16 進）→ 値。幅に入らなければ null */
export function parseRegister(text: string, size: number): number | null {
  const s = text.trim().replace(/^0x/i, "");
  if (!/^[0-9a-f]{1,16}$/i.test(s)) return null;
  const v = parseInt(s, 16);
  return v < 2 ** size ? v : null;
}

/** target.svd.load の引数: デバイス名（Pack・同梱の SVD から探す）かファイルのパスかは使う人が選ぶ（文字の形で推さない）。
 *  空なら null */
export function svdLoadParams(kind: "device" | "path", text: string): { device: string } | { path: string } | null {
  const s = text.trim();
  if (!s) return null;
  return kind === "path" ? { path: s } : { device: s };
}
