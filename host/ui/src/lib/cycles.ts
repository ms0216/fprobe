// 2 点の間のサイクル数（段階 B §3 の 7）: 始点から終点までを cycles.measure で測る（count 回。終わるとターゲットは終点で
// 止まっている）。値は API の要約のまま。Svelte に依らない

/** cycles.measure の引数（誤りは欄の名前）。始点・終点は関数名・「ファイル:行」・「0x…」の番地 */
export function measureParams(start: string, end: string, count: string):
  { params: { start: string; end: string; count: number } } | { error: "start" | "end" | "count" } {
  const s = start.trim();
  const e = end.trim();
  if (!s) return { error: "start" };
  if (!e) return { error: "end" };
  const c = count.trim() === "" ? NaN : Number(count);             // 空を 1 回とみなさない（画面の既定を持たない）
  if (!Number.isInteger(c) || c < 1) return { error: "count" };
  return { params: { start: s, end: e, count: c } };
}

/** Source で選んだ行を cycles.measure の「ファイル:行」に（ブレークポイントと同じく、ソースの名前をそのまま） */
export function lineSpec(sel: { file: string; line: number }): string {
  return `${sel.file}:${sel.line}`;
}
