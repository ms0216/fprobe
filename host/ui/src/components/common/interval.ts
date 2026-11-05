// 周期の読み: $effect の中から呼び、返り値をそのまま $effect の後始末として返す。
// 旧: `void read(); const timer = setInterval(() => void read(), MS); return () => clearInterval(timer);`
// fn は呼んだ場所（$effect の実行中）で同期に始まるので、fn の同期部分が読む状態は $effect の依存になる（従来と同じ）。

/** 周期で fn を呼ぶ（すぐには呼ばない）。後始末の関数を返す */
export function interval(fn: () => unknown, ms: number): () => void {
  const timer = setInterval(() => void fn(), ms);
  return () => clearInterval(timer);
}

/** すぐに 1 回呼んでから、周期で fn を呼ぶ。後始末の関数を返す */
export function intervalNow(fn: () => unknown, ms: number): () => void {
  void fn();
  return interval(fn, ms);
}
