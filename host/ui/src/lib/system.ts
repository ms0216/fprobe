// システム解析（パネル SystemPanel）の計算。Svelte に依らない。
// MCU と FPGA を対等に並べる: MCU は止めずに読む番地（ファームウェアのメールボックス等）、FPGA は観測の最新の値。
// どちらもデーモンの system.status 1 回で読む。表と条件はデーモンの画面の状態（ui.state の system）で別の窓・別の PC・
// AI と分け合う（SharedSystem）。
import { readQuiet, type Result } from "./api";
import type { DaemonConnection } from "./api";
import type { Confirmer } from "./confirm";
import { allowed } from "./guard";
import { unitHex } from "./memory";
import type { AppState } from "./state";

/** 記録の見張り（recording.trigger_set / recording.trigger_status の応答） */
export interface RecordingTrigger {
  state: string; condition?: string; action?: string; hits?: number; last_t?: number | null; error?: string | null;
}

/** FPGA の条件で仕掛けた停止・捕捉の状態（fpga_obs.trigger_status の応答。orphan = デーモンの再起動前に仕掛けた条件が残っている） */
export interface HaltState { condition: string | null; count?: number; orphan?: boolean; halt?: boolean }

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
export function fieldsText(fields: Record<string, number | string> | undefined, value: number | null): string {
  const names = Object.keys(fields ?? {});
  if (!fields || (names.length === 1 && names[0] === "value")) {
    return value === null ? "—" : `0x${value.toString(16).toUpperCase().padStart(4, "0")}`;
  }
  return names.map((n) => `${n}=${fields[n]}`).join("  ");
}

/** 条件の文字をデーモンに確かめさせる（recording.condition.parse。recording.correlate・fpga_obs.count と同じ書き方。
 *  画面は構文解析しない）。読めれば null、読めなければデーモンの理由。FPGA の信号だけの条件かは recording.correlate の
 *  候補の fpga_only */
export async function conditionError(conn: Pick<DaemonConnection, "call">, text: string): Promise<string | null> {
  return (await readQuiet(conn, "recording.condition.parse", { condition: text.trim() })).error;
}

/** 条件の回数（デーモンの fpga_obs.count の条件ごとの結果。数えた範囲の事実を含む。読めない条件は error） */
export type ConditionCount = Result<"fpga_obs.count">["conditions"][number];

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

/** ピンの垂れ流し（recording.la_stream.start）の引数。形式・サンプルレート（MS/s）・チャンネル数は使う人が選ぶ（画面で既定を決めない）。
 * RLE は形式だけ（16 ビットの値の繰り返しなので、詰めない・サンプルレートは指定しない）。RAW はサンプルレートとチャンネル数の両方が要り、
 * どちらかが空・誤りなら null（始められない）。チャンネル数 16 は詰めない（pack_channels = null）、8・4・2 は詰める（ch 0〜n − 1 だけ） */
export function pinsStreamStart(format: "" | "rle" | "raw", rateMsText: string, channels: string, rleFormat: string, tickText = "", repeatOn = false,
                                maxChangesText = ""):
    { rle: boolean; rate_hz?: number; pack_channels?: 8 | 4 | 2 | null; rle_format?: "records" | "tokens"; tick_us?: number; rle_repeat?: true;
      max_changes?: number } | null {
  // RLE のレコード/トークンも使う人が選んだものを明示して送る（空なら始められない。API の既定に黙って任せない）
  if (format === "rle") {
    if (rleFormat === "records") return { rle: true, rle_format: rleFormat };
    // トークン形式は tick_us（マイクロ秒。0 = 無し）も使う人が明示する（空・誤りなら始められない）
    const tick = tickText.trim();
    if (rleFormat !== "tokens" || !/^\d+(\.\d+)?$/.test(tick)) return null;
    // REPEAT（周期的な信号の繰り返しを 1 トークンにまとめる）は使う人が選ぶ。選んだときだけ rle_repeat: true を送る（API の既定は使わない）
    // 記録に入れる変化点の総数の上限（REPEAT は小さな受け取りが大量の変化点になり、記録の大きさがそれに比例する）。空 = 上限なし（送らない）。
    // 入れたのに正の整数でなければ始められない（黙って上限なしにしない）
    const cap = maxChangesText.trim();
    if (repeatOn && cap !== "" && !/^[1-9]\d*$/.test(cap)) return null;
    return { rle: true, rle_format: rleFormat, tick_us: Number(tick), ...(repeatOn ? { rle_repeat: true as const } : {}),
             ...(repeatOn && cap !== "" ? { max_changes: Number(cap) } : {}) };
  }
  if (format !== "raw") return null;
  const rate = parseRate(rateMsText);
  if (rate === null || !["16", "8", "4", "2"].includes(channels)) return null;
  return { rle: false, rate_hz: rate * 1e6, pack_channels: channels === "16" ? null : (Number(channels) as 8 | 4 | 2) };
}

/** ピンの垂れ流しの形式の事実の見せ方（recording.status の la_stream の欄をそのまま。計算・判定はしない: 桁と無いときの "—" だけ）。
 *  AI が MCP で始めた流れも、人が画面で始めた流れも、同じ欄がデーモンから届くので同じに出る */
export function pinsStreamFacts(s: NonNullable<Result<"recording.status">["la_stream"]>):
    { format: string; wire: string; pad: string; changes: string; perChange: string } | null {
  if (s.rle_format === undefined || s.wire_bytes === undefined) return null;      // 形式の事実を持たない古いデーモン
  const dash = "—";
  return { format: s.rle_format, wire: s.wire_bytes.toLocaleString(), pad: (s.pad_bytes ?? 0).toLocaleString(),
           changes: s.changes == null ? dash : s.changes.toLocaleString(),
           perChange: s.bytes_per_change == null ? dash : s.bytes_per_change.toFixed(3) };
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
