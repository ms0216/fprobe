// システム解析（パネル SystemPanel）の計算。Svelte に依らない。
// MCU と FPGA を対等に並べる: MCU は止めずに読む番地（ファームウェアのメールボックス等）、FPGA は観測の最新の値。
// どちらもデーモンの system.status 1 回で読む。表と条件はデーモンの画面の状態（ui.state の system）で別の窓・別の PC・
// AI と分け合う（SharedSystem）。
import type { DaemonConnection } from "./api";

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

export interface MemoryRead {
  t_us: number;
  memory: Record<string, number>;
}

/** 前の読みからの毎秒の増え方（カウンタの速さ。32 ビットの一周は前へ進んだとみなす） */
export function rates(prev: MemoryRead | null, cur: MemoryRead): Record<string, number> {
  const out: Record<string, number> = {};
  if (!prev) return out;
  const dt = (cur.t_us - prev.t_us) / 1e6;
  if (!(dt > 0)) return out;
  for (const [k, v] of Object.entries(cur.memory)) {
    if (!(k in prev.memory)) continue;
    const d = (v - prev.memory[k] + 2 ** 32) % 2 ** 32;
    out[k] = Math.round(d / dt);
  }
  return out;
}

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

export interface ObsItem {
  kind: string;
  time_us: number | null;
  fields?: Record<string, number | string>;
}

/** 1 つの項が、直前の値 prev から今の値 cur で成り立つか（rose / fell は 0 と 0 以外） */
function holds(t: Term, cur: number | string | undefined, prev: number | string | undefined): boolean {
  const on = (x: number | string | undefined) => x !== undefined && x !== 0 && x !== "0";
  switch (t.op) {
    case "changed": return cur !== prev;
    case "rose": return !on(prev) && on(cur) && prev !== undefined;
    case "fell": return on(prev) && !on(cur) && cur !== undefined;
    default: return String(cur) === t.value;
  }
}

/** 値の並び（fpga_obs.read の items）で条件の成立回数と 1 秒あたりの回数。nowUs（fprobe の今の時刻）を渡すと直近
 *  RATE_WINDOW_US で数える（スナップショット型は変化しか残さないので、値が止まったら 0 になる）。省けば最初から最後の値まで。
 *  「変化」は直前の値の記録と比べる（欠落・グループ切り替えは飛ばす） */
export const RATE_WINDOW_US = 2_000_000;

export function countCondition(items: ObsItem[], terms: Term[], nowUs?: number | null): { count: number; perSecond: number | null } {
  const values = items.filter((it) => it.kind === "value" && it.fields && it.time_us !== null);
  if (values.length < 2) return { count: 0, perSecond: nowUs != null ? 0 : null };   // 観測中で値が無い = 変化なし
  const first = values[0].time_us!;
  const last = values[values.length - 1].time_us!;
  const end = Math.max(nowUs ?? last, last);                 // 値は今の時刻より後に読むので、今より新しいこともある
  const from = nowUs != null ? Math.max(first, end - RATE_WINDOW_US) : first;
  let count = 0;
  const prevOf = new Map<number, Record<string, number | string>>();   // グループ → 直前の値（別のグループと比べない）
  for (const it of values) {
    const group = (it as { group?: number }).group ?? 0, cur = it.fields!, prev = prevOf.get(group);
    if (prev && it.time_us! >= from && terms.every((t) => holds(t, cur[t.field], prev[t.field]))) count++;
    prevOf.set(group, cur);
  }
  const span = (end - from) / 1e6;
  return { count, perSecond: span > 0 ? count / span : null };
}

/** 記録の縮図の波形（recording.overview の waveform: ch ごとに画素の 0 / 1 / x（画素の中で変化）/ -（波形が無い））で、
 *  波形のある所が全体で同じ値の ch（波形の無い ch は入れない） */
export function flatChannels(waveform: string[]): number[] {
  return waveform.flatMap((w, ch) => {
    const levels = new Set(w.replace(/-/g, ""));
    return levels.size === 1 && !levels.has("x") ? [ch] : [];
  });
}

/** 結果の直前に多い出来事の例（recording.correlate の examples の 1 つ）を見る画面の状態: 時間軸の範囲は候補から結果までに、前後へ
 *  間の半分（最小 50 µs）の余白。カーソルは基準 = 候補・選択 = 結果（差 = 遅れが読める） */
export function exampleView(ex: { cause_t: number; effect_t: number }, timebaseHz: number) {
  const margin = Math.max((ex.effect_t - ex.cause_t) / 2, 50e-6 * timebaseHz);
  return { view: { source: "recording" as const, t0: Math.round(ex.cause_t - margin), t1: Math.round(ex.effect_t + margin) },
           cursors: { reference: ex.cause_t, selection: ex.effect_t } };
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

/** サンプラを使っている watch（watch.status の watches）が、システム解析の表と同じ名前・番地の組か（前に記録のために
 *  始めて、記録が条件・AI・別の窓で止まって残ったもの。自分のものとして引き継ぎ、記録を止めるときに止める） */
export function ownWatch(running: { name: string; address: string | null }[], watches: Watch[]): boolean {
  const key = (name: string, address: string) => `${name}@${address.toUpperCase()}`;
  const mine = new Set(watches.map((w) => key(w.name, addressKey(w.address))));
  return mine.size > 0 && running.length === mine.size && running.every((w) => w.address !== null && mine.has(key(w.name, w.address)));
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

/** 記録の MCU のメモリの読みの速さ（DAP サンプラ、回/秒）。ピンも流す（ロジアナの深いバッファのストリーム）とき、
 *  Ethernet のリンクでは下げる: サンプラの SWD の転送で内部の ch（SWDIO 駆動・DAP 処理中）が細かく変わり、
 *  1000 回/秒ではストリームが 2.3 MB/s になって PYNQ の中継が詰まり、制御の応答が 1 往復 0.4〜1.4 秒になった。
 *  200 回/秒なら 0.7 MB/s・2 ms（2026-10-21 実機）。時刻の分解能は 5 ms になる */
export function memoryRateHz(withPins: boolean, link: string | undefined): number {
  return withPins && link === "eth" ? 200 : 1000;
}
