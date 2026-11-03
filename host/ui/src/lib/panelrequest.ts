// パネルを開く・前に出す頼み（ui.state.set の panel。AI も人の View メニューと同じことができる）: 通知 ui.state の
// changed に panel があれば、その窓のドックで開く（閉じていれば既定の場所の近くに開き、前に出す）。名前はデーモンが
// 確かめた registry の id（仕様の enum）。Svelte に依らない
import type { DaemonConnection } from "./api";

type Conn = Pick<DaemonConnection, "on">;

export function followPanelRequests(conn: Conn, show: (id: string) => void): () => void {
  return conn.on("ui.state", (n: { changed?: string[]; state?: { panel?: { id: string; seq: number } | null } }) => {
    const panel = n.state?.panel;
    if (n.changed?.includes("panel") && panel) show(panel.id);
  });
}
