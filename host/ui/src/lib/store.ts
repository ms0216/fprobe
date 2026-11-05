// Svelte の store の約束（subscribe）の共通部分: 購読者を足し、すぐ今の値を 1 回渡し、やめる関数を返す。
// 各 lib の状態の持ち手（Svelte に依らない）が同じ 3 行を持っていたのを 1 か所にした。

export function addListener<T>(listeners: Set<(value: T) => void>, fn: (value: T) => void, current: T): () => void {
  listeners.add(fn);
  fn(current);
  return () => {
    listeners.delete(fn);
  };
}
