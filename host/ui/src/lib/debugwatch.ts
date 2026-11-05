// デバッガのウォッチの読み方: 変数ごとの読み（止まったフレーム / 走らせたまま / ELF だけ）と、走行中の周期の読みの管理。
// Svelte にも Debugger にも依らない（Debugger が使う）。
import { errorText, type DaemonConnection } from "./api";
import type { Watch, WatchMode } from "./debugtypes";

type Call = DaemonConnection["call"];

/** ウォッチを読む: stopped = 選んだフレームで、live = 走らせたまま（大域・静的）、elf = ELF だけで式と型を調べる
 *  （値は無い）。変数ごとの誤りはその行に出す */
export async function readWatchList(call: Call, watches: Watch[], mode: WatchMode, frame: number): Promise<Watch[]> {
  const out: Watch[] = [];
  for (const w of watches) {
    try {
      if (mode === "elf") {
        const r = await call("debug.expression.check", { expression: w.name }, { quiet: true });
        out.push({ name: w.name, type: r.type ?? undefined });
        continue;
      }
      const where = mode === "live" ? { live: true } : { frame };
      const r = await call("debug.evaluate", { expression: w.name, ...where }, { quiet: true });
      out.push({ name: w.name, type: r.type, value: r.value });
    } catch (e) {
      out.push({ name: w.name, error: errorText(e) });
    }
  }
  return out;
}

/** ウォッチを走らせたまま読む周期（ウォッチのパネルが開いている間だけ、ターゲットが走っているときに回る） */
export class LiveReader {
  private users = 0;                          // 走らせたまま読む利用者（開いているパネル）の数
  private timer: ReturnType<typeof setInterval> | null = null;
  private ms: number;

  /** running: ターゲットが走っているか。halted: 止まった状態を持っているか（止まっていればすぐの 1 回は読まない）。
   *  read: ウォッチを 1 回読む */
  constructor(private deps: { running: () => boolean; halted: () => boolean; read: () => Promise<void> }, ms: number) {
    this.ms = ms;
  }

  get active(): boolean {
    return this.users > 0;
  }

  /** 利用者を足す。止めるときは返った関数を呼ぶ */
  start(ms: number): () => void {
    this.users++;
    this.ms = ms;
    this.sync();
    let stopped = false;
    return () => {
      if (stopped) return;
      stopped = true;
      this.users--;
      this.sync();
    };
  }

  /** 状態に合わせて周期の読みを始める・止める（走っていて利用者がいるときだけ）。止まっていない間はすぐ 1 回読む */
  sync(): void {
    const want = this.users > 0 && this.deps.running();
    if (want && !this.timer) {
      this.timer = setInterval(() => void this.deps.read(), this.ms);
    } else if (!want && this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (this.users > 0 && !this.deps.halted()) void this.deps.read();
  }
}
