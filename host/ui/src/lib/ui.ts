// 画面の共有の状態（パネルどうし・メニュー・ツールバーが使う）: ソースで選んだ行、最近開いた ELF、開いている
// ダイアログ、ほかのビューからの「その場所を見せて」（ソースの行・メモリの番地）。Svelte に依らない（store の約束 subscribe だけを満たす）。
import { load, save } from "./prefs";

export type Dialog = "openElf" | "connect" | "settings" | "shortcuts" | "about" | "loadSvd" | null;

export interface UiSnapshot {
  selection: { file: string; line: number } | null;    // Run to Cursor・Toggle Breakpoint の対象
  recent: string[];                                     // 最近開いた ELF（デーモンの PC のパス。新しい順）
  dialog: Dialog;
  /** ほかのビュー・時間軸から「この行を見せて」と頼まれた回数（Source はそのたびに選んだ行へ移す） */
  reveals: number;
  /** ほかのビュー・時間軸から頼まれた Memory の番地（seq はそのたびに増える） */
  memory: { address: number; seq: number } | null;
  /** ほかのビュー（Activity の行）から頼まれた API パネルの呼び出し（seq はそのたびに増える） */
  api: { method: string; params: Record<string, unknown>; seq: number; favorite?: string } | null;
}

const RECENT = 8;

function loadRecent(): string[] {
  try {
    const v = JSON.parse(load("recentElf", "[]"));
    return Array.isArray(v) ? v.filter((x) => typeof x === "string").slice(0, RECENT) : [];
  } catch {
    return [];
  }
}

export class UiState {
  private snap: UiSnapshot = { selection: null, recent: loadRecent(), dialog: null, reveals: 0,
                                     memory: null, api: null };
  private listeners = new Set<(s: UiSnapshot) => void>();

  subscribe(fn: (s: UiSnapshot) => void): () => void {
    this.listeners.add(fn);
    fn(this.snap);
    return () => this.listeners.delete(fn);
  }

  get snapshot(): UiSnapshot {
    return this.snap;
  }

  select(file: string, line: number): void {
    this.set({ selection: { file, line } });
  }

  /** 行を選んで Source をそこへ移す（ほかのビュー・時間軸から） */
  revealLine(file: string, line: number): void {
    this.set({ selection: { file, line }, reveals: this.snap.reveals + 1 });
  }

  /** API パネルをそのメソッドと引数で開く（Activity の行から。同じ呼び出しを人がやり直す） */
  openApi(method: string, params: Record<string, unknown>, favorite?: string): void {
    this.set({ api: { method, params, seq: (this.snap.api?.seq ?? 0) + 1, ...(favorite ? { favorite } : {}) } });
  }

  /** API パネルが依頼を受け取る（受け取ったら消す: パネルを付け直しても前の依頼がもう一度当たらない） */
  takeApi(): UiSnapshot["api"] {
    const api = this.snap.api;
    if (api) this.set({ api: null });
    return api;
  }

  /** Memory をその番地で開く（ほかのビュー・時間軸から） */
  revealMemory(address: number): void {
    this.set({ memory: { address: address >>> 0, seq: (this.snap.memory?.seq ?? 0) + 1 } });
  }

  remember(path: string): void {
    const recent = [path, ...this.snap.recent.filter((p) => p !== path)].slice(0, RECENT);
    save("recentElf", JSON.stringify(recent));
    this.set({ recent });
  }

  open(dialog: Dialog): void {
    this.set({ dialog });
  }

  close(): void {
    this.set({ dialog: null });
  }

  private set(patch: Partial<UiSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    this.listeners.forEach((fn) => fn(this.snap));
  }
}
