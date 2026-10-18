// パネルの読み続け: 要求を積もらせない（前の読みが終わるまで次を出さない）・かかった時間に合わせて間をあける・
// 読み込み中と読めなかった誤りを状態に出す（黙って捨てない）。Svelte に依らない（store の約束 subscribe だけを満たす）。
// 画面の部品は components/panels/LoadState.svelte（読み込み中・誤りの表示）。
import { errorText } from "./api";

export interface PollState {
  loading: boolean;          // 読んでいる最中
  loaded: boolean;           // 一度でも読めた（以後は前の結果を出したまま読み直す）
  error: string | null;      // 最後の読みの誤り（次に読めたら消える）
}

export interface PollOptions {
  minMs?: number;            // 周期の読みの最短の間
  factor?: number;           // 間 = かかった時間 × factor（重い読みほど間をあけ、デーモンの作業を埋めない）
  maxMs?: number;            // 間の最長
}

export class Poller {
  private snap: PollState = { loading: false, loaded: false, error: null };
  private listeners = new Set<(s: PollState) => void>();
  private running: Promise<void> | null = null;
  private again = false;
  private polling = false;
  private disposed = false;
  private lastMs = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private readonly minMs: number;
  private readonly factor: number;
  private readonly maxMs: number;

  constructor(private read: () => Promise<void>, opts: PollOptions = {}) {
    this.minMs = opts.minMs ?? 1000;
    this.factor = opts.factor ?? 2;
    this.maxMs = opts.maxMs ?? 10000;
  }

  subscribe(fn: (s: PollState) => void): () => void {
    this.listeners.add(fn);
    fn(this.snap);
    return () => this.listeners.delete(fn);
  }

  get state(): PollState {
    return this.snap;
  }

  /** いま読む。読んでいる最中なら、終わった後に 1 回だけ読み直す（何度頼まれても 1 回）。約束はその読みまで待つ */
  refresh(): Promise<void> {
    if (this.disposed) return Promise.resolve();
    if (this.running) {
      this.again = true;
      return this.running;
    }
    clearTimeout(this.timer);
    this.running = this.loop().finally(() => {
      this.running = null;
      this.schedule();
    });
    return this.running;
  }

  /** 周期的に読む・やめる。読みは前の読みが終わってから max(minMs, かかった時間 × factor) 後 */
  poll(on: boolean): void {
    if (on === this.polling) return;
    this.polling = on;
    clearTimeout(this.timer);
    if (on) void this.refresh();
  }

  dispose(): void {
    this.disposed = true;
    this.polling = false;
    clearTimeout(this.timer);
    this.listeners.clear();
  }

  private async loop(): Promise<void> {
    do {
      this.again = false;
      await this.once();
    } while (this.again && !this.disposed);
  }

  private async once(): Promise<void> {
    this.set({ loading: true });
    const started = Date.now();
    try {
      await this.read();
      if (!this.disposed) this.set({ loading: false, loaded: true, error: null });
    } catch (e) {
      if (!this.disposed) this.set({ loading: false, error: errorText(e) });
    } finally {
      this.lastMs = Date.now() - started;
    }
  }

  private schedule(): void {
    clearTimeout(this.timer);
    if (!this.polling || this.disposed) return;
    const wait = Math.min(this.maxMs, Math.max(this.minMs, this.lastMs * this.factor));
    this.timer = setTimeout(() => void this.refresh(), wait);
  }

  private set(patch: Partial<PollState>): void {
    this.snap = { ...this.snap, ...patch };
    this.listeners.forEach((fn) => fn(this.snap));
  }
}
