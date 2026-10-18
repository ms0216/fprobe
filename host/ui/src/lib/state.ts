// 画面の状態: デーモンの応答と通知から組み立てる。Svelte に依らない（store の約束 subscribe だけを満たす）。
import { errorText, type DaemonConnection, type LinkState, type Methods } from "./api";
import { setChannelLayout } from "./waveform";

export interface ActivityEntry {
  seq: number;
  time: string;
  client: string;
  role: string;
  method: string;
  params: unknown;
  result?: unknown;
  error?: string;
  seconds?: number;
}

export interface ClientInfo {
  id: number;
  name: string;
  role: string;
}

export interface Control {
  paused: boolean;
  paused_by: string | null;
  held: number;
}

export interface Snapshot {
  link: LinkState;
  device: { link: string; address: string | null } | null;
  control: Control;
  clients: ClientInfo[];
  target: Record<string, any> | null;
  probe: Record<string, any> | null;
  voltage: Record<string, any> | null;
  activity: ActivityEntry[];
  hasOlder: boolean;
  error: string | null;
}

const PAGE = 100;
const MAX_ROWS = 5000;          // 手元に持つ記録の上限（古い方は上へのスクロールで読み直せる）

type Conn = Pick<DaemonConnection, "call" | "on" | "onLink">;

export class AppState {
  private snap: Snapshot = {
    link: "connecting", device: null, control: { paused: false, paused_by: null, held: 0 }, clients: [],
    target: null, probe: null, voltage: null, activity: [], hasOlder: false, error: null,
  };
  private listeners = new Set<(s: Snapshot) => void>();
  private boot: string | undefined;          // デーモンの起動の識別。変われば記録の番号は振り直されている

  constructor(private conn: Conn) {
    conn.onLink((link, info) => {
      this.set({ link });
      if (link === "closed" && info instanceof Error) this.fail(info);    // 名乗りを断られた（トークン・版）
      if (link === "open") void this.reload();
    });
    conn.on("activity", (e: ActivityEntry) => this.setActivity(merge(this.snap.activity, [e])));
    conn.on("control", (control: Control) => this.set({ control }));
    conn.on("clients", (d: { clients: ClientInfo[] }) => this.set({ clients: d.clients }));
    conn.on("target.state", (target: Record<string, any>) => this.set({ target }));
  }

  subscribe(fn: (s: Snapshot) => void): () => void {
    this.listeners.add(fn);
    fn(this.snap);
    return () => this.listeners.delete(fn);
  }

  /** 一時停止していなくて、AI の利用者がつながっている */
  get aiWorking(): boolean {
    return !this.snap.control.paused && this.snap.clients.some((c) => c.role === "ai");
  }

  /** 操作する。誤りは error に出し、投げない（画面のボタンから呼ぶ）。誤りの表示は利用者が閉じるか、次の誤りで替わる
   *  （裏の読みの成功で、まだ読んでいない誤りを消さない） */
  async act<M extends keyof Methods>(method: M, params?: Methods[M]["params"], opts?: { quiet?: boolean }): Promise<any> {
    try {
      return await this.conn.call(method, params, opts);
    } catch (e) {
      this.fail(e);
      return undefined;
    }
  }

  async readVoltage(quiet = false): Promise<void> {
    const voltage = await this.act("probe.voltage", undefined, { quiet });
    if (voltage !== undefined) this.set({ voltage });
  }

  async loadOlder(): Promise<void> {
    const oldest = this.snap.activity[0]?.seq;
    if (oldest === undefined || !this.snap.hasOlder) return;
    const page = await this.act("session.activity.list", { before: oldest, limit: PAGE });
    if (!page) return;
    const activity = merge(this.snap.activity, page.entries);
    this.set({ activity, hasOlder: page.entries.length > 0 && activity[0].seq > 1 });     // 古い方へは上限なく読める
  }

  clearError(): void {
    this.set({ error: null });
  }

  private async reload(): Promise<void> {
    const status = await this.act("session.status");
    const otherBoot = status !== undefined && this.boot !== undefined && status.boot !== this.boot;
    if (status) {
      this.boot = status.boot;
      // 見張りがまだ状態を知らない（null）ときは、先に届いた target.state の通知を消さない
      this.set({ device: status.device, control: status.control, clients: status.clients,
                 target: status.target ?? this.snap.target });
    }
    const page = await this.act("session.activity.list", { limit: PAGE });
    if (page) {
      let fresh: ActivityEntry[] = page.entries;
      const known = this.snap.activity;
      // 別の起動（または番号が手元より戻った）= デーモンが再起動した。古い行と混ぜない
      const restarted = otherBoot ||
        (known.length > 0 && (fresh.length === 0 || fresh[fresh.length - 1].seq < known[known.length - 1].seq));
      if (!restarted && known.length > 0) {
        // 切れている間に PAGE 件より多く流れたら、手元の続きまでさかのぼって間を埋める
        const last = known[known.length - 1].seq;
        while (fresh.length > 0 && fresh[0].seq > last + 1) {
          const older = await this.act("session.activity.list", { before: fresh[0].seq, limit: PAGE });
          if (!older || older.entries.length === 0) break;
          fresh = [...older.entries, ...fresh];
        }
      }
      this.setActivity(restarted ? fresh : merge(known, fresh));
    }
    const probe = await this.act("probe.info", undefined, { quiet: true });
    if (probe !== undefined) {
      setChannelLayout(probe?.capabilities?.la_layout);                // ロジアナの ch の名前（in0 等）を申告から
      this.set({ probe });
    }
    if (probe?.capabilities?.aux) await this.readVoltage(true);   // 電圧を測れる構成なら最初に一度読む（記録しない）
  }

  /** 新しい記録を足した後: 上限を超えたら古い方を捨てる（捨てた分は上へのスクロールで読み直せる） */
  private setActivity(activity: ActivityEntry[]): void {
    const kept = activity.length > MAX_ROWS ? activity.slice(-MAX_ROWS) : activity;
    this.set({ activity: kept, hasOlder: kept.length > 0 && kept[0].seq > 1 });
  }

  private fail(e: unknown): void {
    this.set({ error: errorText(e) });
  }

  private set(patch: Partial<Snapshot>): void {
    this.snap = { ...this.snap, ...patch };
    this.listeners.forEach((fn) => fn(this.snap));
  }
}

/** 番号で重複を除き、番号の小さい順に並べる */
function merge(a: ActivityEntry[], b: ActivityEntry[]): ActivityEntry[] {
  if (b.length === 1 && (a.length === 0 || b[0].seq > a[a.length - 1].seq)) return [...a, b[0]];   // 通知: 末尾に足すだけ
  const bySeq = new Map<number, ActivityEntry>();
  for (const e of [...a, ...b]) bySeq.set(e.seq, e);
  return [...bySeq.values()].sort((x, y) => x.seq - y.seq);
}
