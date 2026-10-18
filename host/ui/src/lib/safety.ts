// 止めると壊れるターゲットの安全: 止める操作（中断・ステップ・ブレークポイント・関数の呼び出し）の前の警告と、
// 走らせたままの致命的エラー（target.state の fatal）の表し方。Svelte に依らない（store の約束 subscribe だけを満たす）。
// 見分けはデーモン（debug.halt_risk = ELF の無線・USB・ウォッチドッグ・RTOS の手がかり、target.status の fatal =
// ロックアップ・致命的エラーの関数・故障の処理のループ・ティックの停止）。画面は機器の名前で判断しない。
import type { DaemonConnection } from "./api";
import { confirmer } from "./confirm";
import { t, type Key, type Translate } from "./i18n";
import type { AppState, Snapshot } from "./state";

export interface RiskReason {
  kind: "radio" | "usb" | "watchdog" | "rtos";
  count: number;
  symbols: string[];
  rtos?: string | null;
}

export interface HaltRisk {
  elf: string;
  risky: boolean;
  reasons: RiskReason[];
  monitor_in_elf: boolean;
}

export interface Clue {
  clue: "lockup" | "pc_in" | "pc_loop" | "tick_stopped";
  function?: string;
  bytes?: number;
  tick?: number;
}

export interface Fatal {
  kind: "lockup" | "fatal_halt" | "fault_loop" | "stuck";
  function?: string;
  since: number;                      // UNIX 秒
  evidence: Clue[];
}

export type HaltAction = "halt" | "step" | "breakpoint" | "call";

/** 止める前に警告するか（止めると壊れうる ELF で、モニタ方式でないとき） */
export function needsWarning(risk: HaltRisk | null, target: Record<string, any> | null): boolean {
  return !!risk?.risky && target?.debug_mode !== "monitor";
}

/** 理由の 1 行（「無線（RADIO_IRQHandler ほか 11）」） */
export function reasonText(r: RiskReason, tr: Translate): string {
  const what = tr(`safety.reason.${r.kind}` as Key, { rtos: r.rtos ?? "" });
  const first = r.symbols[0];
  if (!first) return what;
  return `${what} (${r.count > 1 ? tr("safety.andMore", { name: first, n: r.count - 1 }) : first})`;
}

/** 止める前の確かめの文 */
export function confirmText(risk: HaltRisk, action: HaltAction, tr: Translate): string {
  const reasons = risk.reasons.map((r) => `・${reasonText(r, tr)}`).join("\n");
  const way = tr(risk.monitor_in_elf ? "safety.useMonitor" : "safety.addMonitor");
  return [tr("safety.confirm", { action: tr(`safety.action.${action}` as Key) }), reasons, "", tr("safety.ways"), way,
          "", tr("safety.continue")].join("\n");
}

/** 手がかりの 1 行 */
export function clueText(c: Clue, tr: Translate): string {
  return tr(`safety.clue.${c.clue}` as Key, { function: c.function ?? "?", bytes: c.bytes ?? 0, tick: c.tick ?? 0 });
}

/** 致命的エラーの見出し（「致命的エラーの処理で回り続けています: arch_system_halt()」） */
export function fatalTitle(f: Fatal, tr: Translate): string {
  return tr(`safety.fatal.${f.kind}` as Key, { function: f.function ?? "?" });
}

/** プローブの USB が切れて開き直した（target.state の link_lost。B28） */
export interface LinkLost {
  at: number;                                   // UNIX 秒
  changed: Record<string, [unknown, unknown]>;  // 前と変わった機能（Flash の古い版で立ち上がった等）
  // ビットストリームの版の比較（U49。changed / latest_build は分からなければ null。古いデーモンには無い）
  version?: { before: string | null; after: string | null; changed: boolean | null; latest_build: boolean | null } | null;
}

/** つなぎ直しの知らせの文。版が分かれば版が変わったかを確実に言い、分からなければ変わった機能（「jtag: false → true, …」）
 *  から推測を添える */
export function linkText(l: LinkLost, tr: Translate): string {
  const v = l.version;
  if (v && v.changed !== null) {
    if (!v.changed) return tr("safety.linkLost");
    const key = v.latest_build ? "safety.linkVersionLatest" : "safety.linkVersion";
    return `${tr("safety.linkLost")} ${tr(key, { before: v.before ?? "?", after: v.after ?? "?" })}`;
  }
  const list = Object.entries(l.changed).map(([k, [a, b]]) => `${k}: ${a} → ${b}`).join(", ");
  return list ? `${tr("safety.linkLost")} ${tr("safety.linkChanged", { list })}` : tr("safety.linkLost");
}

export interface SafetySnapshot {
  risk: HaltRisk | null;              // 読み込んだ ELF の見分け（ELF が無ければ null）
  fatal: Fatal | null;                // 走らせたままの致命的エラー（target.state の fatal）
}

type Conn = Pick<DaemonConnection, "call" | "on" | "onLink">;
// ELF を変える操作。誰か（AI も）が行ったら見分け直す
const ELF_METHODS = new Set(["debug.elf.load", "debug.elf.load_demo", "debug.elf.upload", "target.program_ram",
                             "target.flash"]);

export class Safety {
  private snap: SafetySnapshot = { risk: null, fatal: null };
  private listeners = new Set<(s: SafetySnapshot) => void>();
  private acknowledged = new Set<string>();         // 止めてよいと答えた ELF（この窓の間は聞き直さない）
  private target: Record<string, any> | null = null;

  constructor(private conn: Conn, app: Pick<AppState, "subscribe">,
              private ask: (text: string) => boolean | Promise<boolean> =
                (text) => confirmer.ask(text, { ok: t("confirm.continue"), danger: true })) {
    conn.onLink((link) => {
      if (link === "open") void this.reload();
    });
    conn.on("activity", (e: { method: string; error?: string }) => {
      if (ELF_METHODS.has(e.method) && !e.error) void this.reload();
    });
    app.subscribe((s: Snapshot) => {
      this.target = s.target;
      const fatal = (s.target?.fatal as Fatal | undefined) ?? null;
      if (JSON.stringify(fatal) !== JSON.stringify(this.snap.fatal)) this.set({ fatal });
    });
  }

  subscribe(fn: (s: SafetySnapshot) => void): () => void {
    this.listeners.add(fn);
    fn(this.snap);
    return () => this.listeners.delete(fn);
  }

  get snapshot(): SafetySnapshot {
    return this.snap;
  }

  /** 止める操作の前: 止めると壊れうるターゲット（モニタ方式でない）なら確かめる。進めてよければ true。
   *  答えは ELF ごとに覚える（同じ ELF のステップ・ブレークポイントのたびには聞かない） */
  async confirmHalt(action: HaltAction, tr: Translate): Promise<boolean> {
    const risk = this.snap.risk;
    if (!risk || !needsWarning(risk, this.target) || this.acknowledged.has(risk.elf)) return true;
    if (!(await this.ask(confirmText(risk, action, tr)))) return false;
    this.acknowledged.add(risk.elf);
    return true;
  }

  async reload(): Promise<void> {
    try {
      const risk = await this.conn.call("debug.halt_risk", {}, { quiet: true });
      this.set({ risk: risk ?? null });
    } catch {
      this.set({ risk: null });                       // ELF が無い・古いデーモン
    }
  }

  private set(patch: Partial<SafetySnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    this.listeners.forEach((fn) => fn(this.snap));
  }
}
