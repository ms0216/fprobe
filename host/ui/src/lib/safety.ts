// 止めると壊れるターゲットの安全: 止める操作（中断・ステップ・ブレークポイント・関数の呼び出し）の前の警告と、
// 走らせたままの致命的エラー（target.state の fatal）の表し方。Svelte に依らない（store の約束 subscribe だけを満たす）。
// 事実はデーモンが返す（debug.halt_risk = 使う側が debug.halt_risk.set で渡した名前・正規表現に合う ELF のシンボル、
// target.status の fatal = ロックアップ・target.liveness.set で渡した関数の中・ティックの停止）。画面は名前で種類を
// 決めず（無線・RTOS 等と言わない）、返った事実を描くだけ。
import type { Params, Result } from "./api";
import type { LinkLost } from "../../../api/generated/fprobe_api";
import type { DaemonConnection } from "./api";
import { confirmer } from "./confirm";
import { t, type Key, type Translate } from "./i18n";
import type { AppState, Snapshot } from "./state";
import { addListener } from "./store";

export interface SymbolHit {
  name: string;
  kind: string;                       // function / object 等（ELF のシンボルの種類）
  address: number;
}

export interface RiskMatch {
  query: string;                      // 使う側が渡した名前・正規表現
  by: "name" | "pattern";
  count: number;
  symbols: SymbolHit[];
}

export interface HaltRisk {
  elf: string;
  names: string[];                    // debug.halt_risk.set で渡された条件
  patterns: string[];
  matched: boolean;                   // どれかの条件に合うシンボルが ELF にある
  matches: RiskMatch[];
  monitor_in_elf: boolean;
  debug_mode: "halt" | "monitor";     // いまのデバッグの方式
  confirm_before_halt: boolean;       // 止める前に確かめる（matched かつ debug_mode が halt。デーモンが決める）
}

export interface Clue {
  clue: "lockup" | "pc_in" | "pc_range" | "tick_stopped";
  function?: string;
  bytes?: number;
  tick?: number;
  valid?: number;                     // 読めた PC の見本の数
  pc_samples?: number;                // 読んだ数（target.liveness.set の pc_samples）
}

export interface Fatal {
  kind: "lockup" | "pc_in" | "tick_stopped";
  alert: boolean;                     // 知らせる種類（lockup・pc_in。tick_stopped は事実だけ）。デーモンが決める
  function?: string;
  since: number;                      // UNIX 秒
  evidence: Clue[];
}

export type HaltAction = "halt" | "step" | "breakpoint" | "call";

/** 止める前に知らせるか（API の confirm_before_halt をそのまま） */
export function needsWarning(risk: HaltRisk | null): boolean {
  return !!risk?.confirm_before_halt;
}

/** 合った条件の 1 行（「^ll_ (ll_adv_enable ほか 11)」。名前そのものが合っただけなら名前だけ） */
export function reasonText(r: RiskMatch, tr: Translate): string {
  const first = r.symbols[0]?.name;
  if (!first || (r.count === 1 && first === r.query)) return r.query;
  return `${r.query} (${r.count > 1 ? tr("safety.andMore", { name: first, n: r.count - 1 }) : first})`;
}

/** 止める前の確かめの文 */
export function confirmText(risk: HaltRisk, action: HaltAction, tr: Translate): string {
  const reasons = risk.matches.map((r) => `・${reasonText(r, tr)}`).join("\n");
  const way = tr(risk.monitor_in_elf ? "safety.useMonitor" : "safety.addMonitor");
  return [tr("safety.confirm", { action: tr(`safety.action.${action}` as Key) }), reasons, "", tr("safety.ways"), way,
          "", tr("safety.continue")].join("\n");
}

/** 手がかりの 1 行 */
export function clueText(c: Clue, tr: Translate): string {
  return tr(`safety.clue.${c.clue}` as Key, { function: c.function ?? "?", bytes: c.bytes ?? 0, tick: c.tick ?? 0,
                                             valid: c.valid ?? "?", pc_samples: c.pc_samples ?? "?" });
}

/** 致命的エラーの色で出すか（API の alert をそのまま: ロックアップ・使う側が渡した関数の中は true、ティックの停止は
 *  事実だけで false） */
export function isFatal(f: Fatal): boolean {
  return f.alert;
}

/** 見出し（「PC が arch_system_halt() の中にある（target.liveness.set で渡した関数）」） */
export function fatalTitle(f: Fatal, tr: Translate): string {
  return tr(`safety.fatal.${f.kind}` as Key, { function: f.function ?? "?" });
}

/** プローブの USB が切れて開き直した（target.state の link_lost。B28） */
export type { LinkLost };

/** つなぎ直しの知らせの文。版が分かれば版が変わったかを言い、分からなければ変わった機能（「jtag: false → true, …」）
 *  だけを書く（原因は推測しない） */
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

// --- 条件の欄（人が AI と同じ debug.halt_risk.set・target.liveness.set を行う場所）---------------------------------
// 欄は今の条件（API が返したもの）で描く。画面は既定の名前・既定の PC の数を持たない

/** target.liveness.get の結果: 走らせたままの見張りの条件（pc_samples は条件が無ければ null = PC を読まない） */
export type LivenessSettings = Result<"target.liveness.get">;

export interface HaltRiskForm {
  names: string;                      // 1 行に 1 つ（完全一致の名前）
  patterns: string;                   // 1 行に 1 つ（正規表現。, を含みうるので行で区切る）
}

export interface LivenessForm {
  functions: string;
  patterns: string;
  pcSamples: string;
}

/** 欄の 1 行が 1 つの条件（前後の空白と空の行は外す） */
export function conditionLines(text: string): string[] {
  return text.split(/\r?\n/).map((s) => s.trim()).filter((s) => s !== "");
}

/** 今の条件（debug.halt_risk の names・patterns）の欄。読めなければ null（ELF が無い等） */
export function haltRiskForm(risk: Pick<HaltRisk, "names" | "patterns"> | null): HaltRiskForm | null {
  return risk ? { names: risk.names.join("\n"), patterns: risk.patterns.join("\n") } : null;
}

/** 欄から debug.halt_risk.set の引数（置き換え。空なら何も探さない） */
export function haltRiskParams(form: HaltRiskForm): { names: string[]; patterns: string[] } {
  return { names: conditionLines(form.names), patterns: conditionLines(form.patterns) };
}

/** 今の見張りの条件（target.liveness.get）の欄。読めなければ null */
export function livenessForm(s: LivenessSettings | null): LivenessForm | null {
  if (!s) return null;
  return { functions: s.functions.join("\n"), patterns: s.patterns.join("\n"),
           pcSamples: s.pc_samples === null ? "" : String(s.pc_samples) };
}

/** 欄から target.liveness.set の引数。条件があれば PC の数（1 以上の整数）は必須で、無ければ誤り（欄の名前）。
 *  条件が無ければ PC の数は null（PC を読まない） */
export function livenessParams(form: LivenessForm): { params: Params<"target.liveness.set"> } | { error: "pc_samples" } {
  const functions = conditionLines(form.functions);
  const patterns = conditionLines(form.patterns);
  if (!functions.length && !patterns.length) return { params: { functions, patterns, pc_samples: null } };
  const text = form.pcSamples.trim();
  const n = /^\d+$/.test(text) ? Number(text) : 0;
  return n >= 1 ? { params: { functions, patterns, pc_samples: n } } : { error: "pc_samples" };
}

export interface SafetySnapshot {
  risk: HaltRisk | null;              // 読み込んだ ELF の見分け（ELF が無ければ null）
  fatal: Fatal | null;                // 走らせたままの致命的エラー（target.state の fatal）
  liveness: LivenessSettings | null;  // 走らせたままの見張りの条件（target.liveness.get。読めなければ null）
  haltConditions: { names: string[]; patterns: string[] } | null;   // 止める前の警告の条件（debug.halt_risk.get。ELF に依らない）
}

type Conn = Pick<DaemonConnection, "call" | "on" | "onLink">;
// ELF か探す条件を変える操作。誰か（AI も）が行ったら読み直す
const RELOAD_METHODS = new Set(["debug.elf.load", "debug.elf.load_demo", "debug.elf.upload", "target.program_ram",
                                "target.flash", "debug.halt_risk.set", "target.debug_mode.set"]);

export class Safety {
  private snap: SafetySnapshot = { risk: null, fatal: null, liveness: null, haltConditions: null };
  private listeners = new Set<(s: SafetySnapshot) => void>();
  private acknowledged = new Set<string>();         // 止めてよいと答えた ELF（この窓の間は聞き直さない）
  private debugMode: unknown = undefined;            // 状態の debug_mode（変わったら debug.halt_risk を読み直す）

  constructor(private conn: Conn, app: Pick<AppState, "subscribe">,
              private ask: (text: string) => boolean | Promise<boolean> =
                (text) => confirmer.ask(text, { ok: t("confirm.continue"), danger: true })) {
    conn.onLink((link) => {
      if (link === "open") {
        void this.reload();
        void this.reloadLiveness();
      }
    });
    conn.on("activity", (e: { method: string; error?: string }) => {
      if (RELOAD_METHODS.has(e.method) && !e.error) void this.reload();
      if (e.method === "target.liveness.set" && !e.error) void this.reloadLiveness();
    });
    app.subscribe((s: Snapshot) => {
      const mode = s.target?.debug_mode;
      if (mode !== this.debugMode) {                  // 方式が変わった（AI・別の窓も）: 確かめるかはデーモンが返す
        this.debugMode = mode;
        void this.reload();
      }
      const fatal = (s.target?.fatal as Fatal | undefined) ?? null;
      if (JSON.stringify(fatal) !== JSON.stringify(this.snap.fatal)) this.set({ fatal });
    });
  }

  subscribe(fn: (s: SafetySnapshot) => void): () => void {
    return addListener(this.listeners, fn, this.snap);
  }

  get snapshot(): SafetySnapshot {
    return this.snap;
  }

  /** 止める操作の前: デーモンが確かめると返した（confirm_before_halt）なら確かめる。進めてよければ true。
   *  答えは ELF ごとに覚える（同じ ELF のステップ・ブレークポイントのたびには聞かない） */
  async confirmHalt(action: HaltAction, tr: Translate): Promise<boolean> {
    const risk = this.snap.risk;
    if (!risk || !needsWarning(risk) || this.acknowledged.has(risk.elf)) return true;
    if (!(await this.ask(confirmText(risk, action, tr)))) return false;
    this.acknowledged.add(risk.elf);
    return true;
  }

  async reload(): Promise<void> {
    try {
      const conditions = await this.conn.call("debug.halt_risk.get", {}, { quiet: true });
      this.set({ haltConditions: conditions ?? null });
    } catch {
      this.set({ haltConditions: null });             // 古いデーモン
    }
    try {
      const risk = await this.conn.call("debug.halt_risk", {}, { quiet: true });
      this.set({ risk: risk ?? null });
    } catch {
      this.set({ risk: null });                       // ELF が無い・古いデーモン
    }
  }

  /** 走らせたままの見張りの条件を読み直す（デーモンが持つ。画面は既定を持たない） */
  async reloadLiveness(): Promise<void> {
    try {
      const s = await this.conn.call("target.liveness.get", {}, { quiet: true });
      this.set({ liveness: s ?? null });
    } catch {
      this.set({ liveness: null });                   // 古いデーモン
    }
  }

  private set(patch: Partial<SafetySnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    this.listeners.forEach((fn) => fn(this.snap));
  }
}
