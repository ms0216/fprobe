// パネル・メニューを名前で探す: 画面が持つ名前（メニューの名前・項目名・View の中身の説明）に対する語の一致だけ。
// 語は空白で区切り、すべて含むもの（大文字小文字は区別しない）。結果はメニューの並びのまま（得点・並べ替え・語ごとの案内は持たない）

export interface FindEntry {
  menu: string;     // 属するメニューの名前（View / Debug / Target / File / Help）
  label: string;    // 項目名（View のパネルは中身の説明つき）
}

export function matchEntries<T extends FindEntry>(entries: readonly T[], query: string): T[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  return entries.filter((e) => {
    const text = `${e.menu} ${e.label}`.toLowerCase();
    return words.every((w) => text.includes(w));
  });
}
