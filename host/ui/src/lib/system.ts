// システム解析（パネル SystemPanel）の計算。Svelte に依らない。
// MCU と FPGA を対等に並べる: MCU は止めずに読む番地（ファームウェアのメールボックス等）、FPGA は観測の最新の値。
// どちらもデーモンの system.status 1 回で読む。表と条件はデーモンの画面の状態（ui.state の system）で別の窓・別の PC・
// AI と分け合う（SharedSystem）。
import type { DaemonConnection } from "./api";
import type { Confirmer } from "./confirm";
import { allowed } from "./guard";
import { unitHex } from "./memory";
import type { AppState } from "./state";

export interface Watch {
  name: string;
  address: number;
}

/** 「0x20040800」「536873984」→ 番地。32 ビットの語なので 4 の倍数・32 ビットの中だけ。崩れていれば null */
export function parseAddress(text: string): number | null {
  const s = text.trim().replace(/_/g, "");
  if (!/^(0x[0-9a-f]+|\d+)$/i.test(s)) return null;
  const n = Number(s);
  return Number.isSafeInteger(n) && n >= 0 && n <= 0xFFFF_FFFF && n % 4 === 0 ? n : null;
}

/** 保存してあった一覧（prefs）のうち形の正しいものだけ */
export function sanitizeWatches(raw: unknown): Watch[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((w): w is Watch => typeof w === "object" && w !== null && typeof w.name === "string"
    && w.name.trim() !== "" && Number.isInteger(w.address) && w.address >= 0 && w.address % 4 === 0);
}

export const addressKey = (a: number) => `0x${a.toString(16).toUpperCase().padStart(8, "0")}`;

/** FPGA の値: 信号の名前があれば「名前=値」を並べ、無ければ組の値を 16 進で */
export function fieldsText(fields: Record<string, number | string> | undefined, value: number): string {
  const names = Object.keys(fields ?? {});
  if (!fields || (names.length === 1 && names[0] === "value")) return `0x${value.toString(16).toUpperCase().padStart(4, "0")}`;
  return names.map((n) => `${n}=${fields[n]}`).join("  ");
}

/** FPGA の条件の 1 項: 信号が直前の値から変わった（changed）か、信号の値（列挙の名前・数）が value に等しい（=） */
export interface Term {
  field: string;
  op: "changed" | "rose" | "fell" | "=";
  value?: string;
}

/** 「restart changed and state=BUSY」「restart が変化 かつ state = BUSY」「flag rose」→ 項の並び。崩れていれば null。
 *  結果の直前に多い出来事（recording.correlate）と同じ書き方: 空白・= を含む名前・値は "…" で囲む。fpga. の前置きは外す */
export function parseCondition(text: string): Term[] | null {
  const parts = text.split(/\s+(?:and|かつ)\s+|\s*&&\s*/i).map((s) => s.trim()).filter(Boolean);
  if (!parts.length) return null;
  const out: Term[] = [];
  const field = (quoted: string | undefined, bare: string | undefined) => (quoted ?? bare ?? "").replace(/^fpga\./, "");
  for (const p of parts) {
    let m = /^(?:"([^"]+)"|(\S+?))\s*(changed|rose|fell|が変化)$/u.exec(p);
    if (m) {
      out.push({ field: field(m[1], m[2]), op: m[3] === "が変化" ? "changed" : (m[3] as Term["op"]) });
      continue;
    }
    m = /^(?:"([^"]+)"|([^\s="]+))\s*=\s*(?:"([^"]+)"|(\S+))$/u.exec(p);
    if (!m) return null;
    out.push({ field: field(m[1], m[2]), op: "=", value: m[3] ?? m[4] });
  }
  return out;
}

/** 条件が FPGA の信号（names）だけで書かれていて、変化の項を含むか（システム解析の FPGA の条件の表に足せるか） */
export function fpgaOnly(expr: string, names: string[]): boolean {
  const terms = parseCondition(expr);
  return terms !== null && terms.some((t) => t.op !== "=") && terms.every((t) => names.includes(t.field));
}

/** 条件の回数（デーモンの fpga_obs.count の条件ごとの結果。数えた範囲の事実を含む。読めない条件は error） */
export interface ConditionCount {
  condition: string;
  count?: number;
  values?: number;                 // 数えた値の数
  t0_us?: number | null;           // 最初と最後の値の時刻（fprobe の時刻 µs。値が無ければ null）
  t1_us?: number | null;
  span_s?: number | null;
  gaps?: number;                   // 切れ目の数
  lost?: number;                   // 失った変化の数
  error?: string;
}

/** 条件の回数をデーモンに数えさせる（最後の last 件。使う人が欄で入れた値。画面では数えない）。条件・件数が無ければ
 *  問い合わせず null */
export async function readConditionCounts(conn: Pick<DaemonConnection, "call">, conditions: string[], last: number | null)
    : Promise<Record<string, ConditionCount> | null> {
  if (last === null || !conditions.length) return null;
  const r = await conn.call("fpga_obs.count", { conditions, last }, { quiet: true });
  return Object.fromEntries((r?.conditions ?? []).map((c: ConditionCount) => [c.condition, c]));
}

/** 使う人が入れる正の数（記録のメモリの読みの速さ 回/秒）。空・誤りは null（既定の値を持たない） */
export function parseRate(text: string): number | null {
  const s = text.trim();
  if (!/^\d+(\.\d+)?$/.test(s)) return null;
  const n = Number(s);
  return n > 0 ? n : null;
}

/** 使う人が入れる正の整数（条件を数える件数 = 最後の N 件）。空・誤りは null（数えない） */
export function parseCount(text: string): number | null {
  const s = text.trim();
  if (!/^\d+$/.test(s)) return null;
  const n = Number(s);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

/** 結果の直前に多い出来事の例（recording.correlate の examples の 1 つ）を見る画面の状態: 時間軸の範囲は候補から結果までに、前後へ
 *  間の半分（最小 50 µs）の余白。カーソルは基準 = 候補・選択 = 結果（差 = 遅れが読める） */
export function exampleView(ex: { cause_t: number; effect_t: number }, timebaseHz: number) {
  const margin = Math.max((ex.effect_t - ex.cause_t) / 2, 50e-6 * timebaseHz);
  return { view: { source: "recording" as const, t0: Math.round(ex.cause_t - margin), t1: Math.round(ex.effect_t + margin) },
           cursors: { reference: ex.cause_t, selection: ex.effect_t } };
}

/** recording.correlate の引数。結果の条件・窓（ms）は使う人が入れたものだけ（空なら null = 送らない。表の並び等から
 *  暗黙に決めない）。auto なら候補を書かずデーモンが記録の変数から作る（causes = null） */
export function correlateParams(effect: string, causes: string[], auto: boolean, windowMs: number | null, limit: number | null)
  : { effect: string; causes: string[] | null; window_s: number; limit?: number } | null {
  const e = effect.trim();
  if (!e || windowMs === null || !Number.isFinite(windowMs) || windowMs <= 0) return null;
  if (!auto) return causes.length ? { effect: e, causes, window_s: windowMs / 1000 } : null;
  // 自動の候補: 返す数（上位 limit）は使う人が入れたものだけ（API の既定を暗黙に使わない）
  return limit !== null && Number.isSafeInteger(limit) && limit > 0 ? { effect: e, causes: null, window_s: windowMs / 1000, limit } : null;
}

/** 条件（recording.correlate）の中の変数の名前: 空白・= ・" を含めば "…" で囲む（"Error count" changed） */
export function termName(name: string): string {
  return /[\s="]/.test(name) ? `"${name.replace(/"/g, "")}"` : name;
}

export interface Condition {
  name: string;
  expr: string;
}

type Conn = Pick<DaemonConnection, "call" | "on">;

/** MCU のメモリの表と FPGA の条件をデーモンの画面の状態（ui.state の system）で分け合う。ほかの窓・AI の変更は
 *  onChange で届く（自分の変更の通知は読み飛ばす）。ui.state の無いデーモンでは何もしない（手元の表のまま） */
export class SharedSystem {
  readonly origin = `y${Math.random().toString(36).slice(2, 10)}`;
  private available = false;
  private off: () => void;

  constructor(private conn: Conn, private onChange: (watches: Watch[], conditions: Condition[]) => void) {
    this.off = conn.on("ui.state", (n: { origin: string | null; changed?: string[]; state?: any }) => {
      if (n.origin === this.origin || !n.changed?.includes("system")) return;
      const sys = n.state?.system;
      if (sys) this.onChange(sanitizeWatches(sys.watches), sanitizeConditions(sys.conditions));
    });
  }

  /** つながったとき: デーモンにあればそれ（onChange）、無ければ手元のものを上げる */
  async load(watches: Watch[], conditions: Condition[]): Promise<void> {
    let state: any;
    try {
      state = await this.conn.call("ui.state.get", {}, { quiet: true });
    } catch {
      this.available = false;
      return;
    }
    this.available = true;
    const sys = state?.system;
    if (sys) this.onChange(sanitizeWatches(sys.watches), sanitizeConditions(sys.conditions));
    else await this.save(watches, conditions);
  }

  /** 手元の変更を上げる（load の後だけ。記録に残さない） */
  async save(watches: Watch[], conditions: Condition[]): Promise<void> {
    if (!this.available) return;
    try {
      await this.conn.call("ui.state.set", { system: { watches, conditions }, origin: this.origin } as any, { quiet: true });
    } catch {
      /* 切れた等: つながり直したら load で合わせる */
    }
  }

  dispose(): void {
    this.off();
  }
}

/** 保存してあった条件（prefs・ui.state）のうち形の正しいもの */
export function sanitizeConditions(raw: unknown): Condition[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((c): c is Condition => typeof c === "object" && c !== null && typeof c.expr === "string" && c.expr.trim() !== "")
    .map((c) => ({ name: typeof c.name === "string" && c.name.trim() ? c.name.trim() : c.expr.trim(), expr: c.expr.trim() }));
}

// --- 記録の欠けと分解能（デーモンの事実をそのまま描く） --------------------------------------------------

/** 記録中に MCU のメモリの読みが止まったか: recording.status の watch_running が false（別の道具・AI が止めた等。
 *  黙って欠けさせない）。startedWatch = この画面が記録のためにメモリの読みを始めた（始めていなければ知らせない）。
 *  watch_running を申告しないデーモンでは止まったと言わない */
export function memoryStopped(rec: { recording: boolean; watch_running?: boolean } | null, startedWatch: boolean): boolean {
  return Boolean(rec?.recording) && startedWatch && rec?.watch_running === false;
}

/** recording.correlate の sampling（変数ごとの読みの周期 秒。周期の分からない変数は null）を変数ごとに ms で。
 *  並びはデーモンの順のまま。最大を取る等でまとめない */
export function samplingRows(sampling: Record<string, number | null> | undefined): { name: string; ms: number | null }[] {
  return Object.entries(sampling ?? {}).map(([name, s]) => ({ name, ms: s === null ? null : s * 1000 }));
}

// --- MCU の番地の語を書く --------------------------------------------------------------------------

/** 書く値の欄: 10 進・0x の 16 進（_ で区切ってよい）の 32 ビットの符号なしの値。崩れていれば null */
export function parseWord(text: string): number | null {
  const s = text.trim().replace(/_/g, "");
  if (!/^(0x[0-9a-f]+|\d+)$/i.test(s)) return null;
  const n = Number(s);
  return Number.isSafeInteger(n) && n <= 0xFFFF_FFFF ? n : null;
}

/** 番地の語（32 ビット）に値を書く（target.memory.write。止めずに書ける）。人の操作なので、AI がつながっていて
 *  一時停止していなければ先に確かめる（断れば書かない）。値が崩れていれば書かずに文言のキーを返す */
export async function writeWord(app: Pick<AppState, "aiConnectedUnpaused" | "act">, address: number, text: string,
                                ask?: Confirmer): Promise<"system.badWordValue" | null> {
  const value = parseWord(text);
  if (value === null) return "system.badWordValue";
  if (!(await allowed(app, ask))) return null;
  await app.act("target.memory.write", { address, data_hex: unitHex(value, 32) });
  return null;
}

/** 番地でなく変数名（C の識別子・メンバーの . つき）と見える文字 */
export function isSymbolName(text: string): boolean {
  return /^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*$/.test(text.trim());
}

/** 変数名を ELF（debug.symbol.info の結果）で番地に。名前が空なら変数名。関数・語の境でない番地（32 ビットの語で
 *  読むので 4 の倍数だけ）は断る（文言のキーを返す） */
export function symbolWatch(text: string, name: string, info: { address: number; kind?: string; size?: number }):
    Watch | "system.notVariable" | "system.unaligned" {
  if (info.kind && info.kind !== "object") return "system.notVariable";
  if (info.address % 4 !== 0) return "system.unaligned";
  return { name: name.trim() || text.trim(), address: info.address };
}
