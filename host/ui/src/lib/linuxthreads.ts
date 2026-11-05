// Threads のパネルの Linux のプロセス（linux.halt.debug.*。attach している間）: 全スレッドの行と、今のスレッドの選択。
// 考えない層: 一覧・今のスレッド（tid と決まり方）は API の値のまま（target.state の linux 欄と linux.halt.debug.threads）。
// 選ぶと linux.halt.debug.select を呼ぶ（人の選択と AI の選択は同じ API。今のスレッドは target.state で全窓に届く）。
// Svelte に依らない（store の約束 subscribe だけを満たす）
import { errorText, type DaemonConnection, type Result } from "./api";
import type { AppState } from "./state";
import { addListener } from "./store";

export type LinuxThreadRows = Result<"linux.halt.debug.threads">;

export interface LinuxThreadsSnapshot {
  rows: LinuxThreadRows | null;        // 止まっているときに読んだ全スレッド（走っていれば最後に止まったときの値で stale）
  stale: boolean;
  error: string | null;
}

type Conn = Pick<DaemonConnection, "call">;

export class LinuxThreads {
  private snap: LinuxThreadsSnapshot = { rows: null, stale: false, error: null };
  private listeners = new Set<(s: LinuxThreadsSnapshot) => void>();
  private key: string | undefined;
  private seq = 0;

  constructor(private conn: Conn, private app: Pick<AppState, "act">) {}

  /** target.state を受ける: 止まり直したら全スレッドを読み直す（pc と run が同じなら読まない）。走れば古い印 */
  onTarget(target: Record<string, any> | null): void {
    if (!target?.linux) {
      this.key = undefined;
      this.set({ rows: null, stale: false, error: null });
      return;
    }
    if (target.state !== "HALTED") {
      this.key = undefined;
      if (this.snap.rows && !this.snap.stale) this.set({ stale: true });
      return;
    }
    const key = `${target.pc}/${target.run}/${target.linux.tid}`;
    if (key === this.key) return;
    this.key = key;
    void this.read();
  }

  private async read(): Promise<void> {
    const seq = ++this.seq;
    try {
      const rows = await this.conn.call("linux.halt.debug.threads", { variables: false, expressions: [], max_elements: 1 },
                                        { quiet: true });
      if (seq === this.seq) this.set({ rows, stale: false, error: null });
    } catch (e) {
      if (seq === this.seq) this.set({ rows: null, stale: false, error: errorText(e) });
    }
  }

  /** 今のスレッドを選ぶ（linux.halt.debug.select）。レジスタ・呼び出し履歴・変数は target.state の読み直しで変わる */
  async select(tid: number): Promise<void> {
    await this.app.act("linux.halt.debug.select", { tid });
  }

  subscribe(fn: (s: LinuxThreadsSnapshot) => void): () => void {
    return addListener(this.listeners, fn, this.snap);
  }

  private set(patch: Partial<LinuxThreadsSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    this.listeners.forEach((fn) => fn(this.snap));
  }
}
