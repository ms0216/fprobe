// 16 進の表記（0x + 大文字。32 ビットの符号なしとして書く）。画面の見せ方だけ。
// 各 lib が同じ式を持っていたのを 1 か所にした。番地の API 引数（小文字・桁数の違い）は各所のまま。

/** 0x1A2B（桁は詰めない） */
export const hex = (n: number): string => `0x${(n >>> 0).toString(16).toUpperCase()}`;

/** 0x00001A2B（8 桁に揃える） */
export const hex8 = (n: number): string => `0x${(n >>> 0).toString(16).toUpperCase().padStart(8, "0")}`;
