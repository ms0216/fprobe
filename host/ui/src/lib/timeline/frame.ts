// バイナリの応答のフレーム（fprobe.api.binary と同じ形）: "FPB1" + u32 LE の JSON の長さ + JSON-RPC の応答 + 8 バイト境界の区画。
// 区画は ArrayBuffer を複写せずに型付きの配列で見る（Worker へはバッファごと transfer する）。Svelte・DOM に依らない。
// 仕様: docs/superpowers/specs/2026-10-18-timeline-tiles-design.md §3

export type Section = Float64Array | BigUint64Array | Uint32Array | Uint16Array | Uint8Array;
export interface SectionInfo {
  dtype: "f64" | "u64" | "u32" | "u16" | "u8";
  offset: number;
  length: number;
}

const MAGIC = 0x31425046;            // "FPB1"（LE で読んだ値）

export function isFrame(buf: ArrayBuffer): boolean {
  return buf.byteLength >= 8 && new DataView(buf).getUint32(0, true) === MAGIC;
}

/** フレーム → JSON-RPC の応答（result.data に区画の型付きの配列を入れる） */
export function decodeFrame(buf: ArrayBuffer): any {
  if (!isFrame(buf)) throw new Error("fprobe のバイナリのフレームでない");
  const n = new DataView(buf).getUint32(4, true);
  const message = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 8, n)));
  if (message.result && typeof message.result === "object") message.result.data = sectionsOf(buf, message.result.sections ?? {});
  return message;
}

/** 区画の配置から、同じバッファの上の型付きの配列を作る（Worker が受け取ったバッファにも使う） */
export function sectionsOf(buf: ArrayBuffer, sections: Record<string, SectionInfo>): Record<string, Section> {
  const out: Record<string, Section> = {};
  for (const [name, s] of Object.entries(sections)) {
    const T = { f64: Float64Array, u64: BigUint64Array, u32: Uint32Array, u16: Uint16Array, u8: Uint8Array }[s.dtype];
    if (!T) throw new Error(`区画 ${name} の型 ${s.dtype} を知らない`);
    out[name] = new T(buf, s.offset, s.length);
  }
  return out;
}
