// 画面の中の確認（ブラウザ標準の confirm の代わり。標準の窓は画面の作りから外れ、自動の操作では黙って「いいえ」に
// なる）。問いは 1 つずつ順に出し、答えで約束を解く。窓は components/shell/ConfirmDialog.svelte。Svelte に依らない
// （store の約束 subscribe だけを満たす）。

export interface ConfirmOptions {
  ok?: string;               // はいのボタンの文言（省略で「OK」）
  danger?: boolean;          // 取り返しのつかない操作（はいのボタンを赤く、最初の焦点は「キャンセル」に）
}

export interface ConfirmRequest extends ConfirmOptions {
  id: number;
  message: string;
}

export class Confirmer {
  private queue: { request: ConfirmRequest; resolve: (ok: boolean) => void }[] = [];
  private listeners = new Set<(r: ConfirmRequest | null) => void>();
  private ids = 0;

  subscribe(fn: (r: ConfirmRequest | null) => void): () => void {
    this.listeners.add(fn);
    fn(this.current);
    return () => this.listeners.delete(fn);
  }

  /** 聞く。はいなら true */
  ask(message: string, opts: ConfirmOptions = {}): Promise<boolean> {
    return new Promise((resolve) => {
      this.queue.push({ request: { id: ++this.ids, message, ...opts }, resolve });
      if (this.queue.length === 1) this.emit();
    });
  }

  /** いま出している問いに答える */
  answer(ok: boolean): void {
    const head = this.queue.shift();
    if (!head) return;
    head.resolve(ok);
    this.emit();
  }

  private get current(): ConfirmRequest | null {
    return this.queue[0]?.request ?? null;
  }

  private emit(): void {
    const r = this.current;
    this.listeners.forEach((fn) => fn(r));
  }
}

export const confirmer = new Confirmer();
