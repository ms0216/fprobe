// 画面の状態の同期: 時間軸の視点・カーソル 3 種・開いている段・選んだ出来事・呼び出し履歴のフレームを、デーモンの
// 「画面の状態」（ui.state.get / ui.state.set と通知 ui.state）と URL（#v=…）にそろえる。人・AI（MCP）・別の窓・
// 別の PC が同じ状態を見る。Svelte に依らない。
//
// - 手元が変わったら（TimelineState・Debugger の通知）少し待ってまとめて ui.state.set（マウスのカーソルだけの変化は
//   操作の記録に残さない = quiet）。自分の変更の通知（origin が自分）は読み飛ばす。
// - ほかから変わったら（通知）手元に当てる。視点は、その源のデータの範囲が分かってから当てる。
// - URL のハッシュに視点・カーソル・段・出来事・フレームを載せ、リンクで同じところを開ける（開いたときは URL を優先し、
//   デーモンへも送る）。ui.state の無いデーモンでは URL だけ。
// 仕様: docs/superpowers/specs/2026-10-18-ui-framework-evaluation.md §4・§5 段取り 4
import type { DaemonConnection } from "./api";
import type { Debugger, DebugSnapshot } from "./debugger";
import type { PickedItem } from "./timeline/query";
import type { RowConfig } from "./timeline/rows";
import type { Source, TimelineSnapshot, TimelineState } from "./timeline/state";

export interface ViewRef { source: Source; t0: number; t1: number }
export interface Cursors { selection: number | null; mouse: number | null; reference: number | null }
export interface EventRef { stream: string; t: number; t_end?: number; kind?: string; text?: string; row?: string }
/** ほかの窓・AI のマウスのカーソル（ui.state の pointers。デーモンが name・role を付ける） */
export interface Pointer { key: string; t: number; source: Source | null; name: string | null; role: string | null; time: number }

export interface ScreenData {
  view: ViewRef | null;
  cursors: Cursors;
  rows: string[] | null;
  event: EventRef | null;
  frame: number;
}

export const KEYS = ["view", "cursors", "rows", "event", "frame"] as const;
type Key = (typeof KEYS)[number];

export const emptyScreen = (): ScreenData => ({
  view: null, cursors: { selection: null, mouse: null, reference: null }, rows: null, event: null, frame: 0,
});

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

// --- URL のハッシュ -------------------------------------------------------------------------------------

const URL_KEYS = ["v", "sel", "ref", "rows", "ev", "f"];

/** ハッシュの中の画面の状態（無いものは undefined）。例 #v=capture:0-8192&sel=100&ref=50&rows=wave:0,wave:1&ev=uart@120&f=1 */
export function fromHash(hash: string): Partial<ScreenData> {
  const p = new URLSearchParams(hash.replace(/^#/, ""));
  const out: Partial<ScreenData> = {};
  const v = /^(capture|recording):(-?\d+)-(-?\d+)$/.exec(p.get("v") ?? "");
  if (v && Number(v[3]) > Number(v[2])) out.view = { source: v[1] as Source, t0: Number(v[2]), t1: Number(v[3]) };
  const num = (k: string) => (/^-?\d+$/.test(p.get(k) ?? "") ? Number(p.get(k)) : null);
  if (p.has("sel") || p.has("ref")) out.cursors = { selection: num("sel"), mouse: null, reference: num("ref") };
  if (p.has("rows")) out.rows = (p.get("rows") ?? "").split(",").filter(Boolean);
  const ev = /^(.+)@(-?\d+)$/.exec(p.get("ev") ?? "");
  if (ev) out.event = { stream: ev[1], t: Number(ev[2]) };
  if (num("f") !== null && num("f")! >= 0) out.frame = num("f")!;
  return out;
}

/** 画面の状態をハッシュにする（ほかの項目 = token 等は保つ） */
export function toHash(hash: string, d: ScreenData): string {
  const p = new URLSearchParams(hash.replace(/^#/, ""));
  for (const k of URL_KEYS) p.delete(k);
  if (d.view) p.set("v", `${d.view.source}:${d.view.t0}-${d.view.t1}`);
  if (d.cursors.selection !== null) p.set("sel", String(d.cursors.selection));
  if (d.cursors.reference !== null) p.set("ref", String(d.cursors.reference));
  if (d.rows) p.set("rows", d.rows.join(","));
  if (d.event) p.set("ev", `${d.event.stream}@${d.event.t}`);
  if (d.frame) p.set("f", String(d.frame));
  const s = p.toString().replace(/%3A/gi, ":").replace(/%2C/gi, ",").replace(/%40/g, "@");
  return s ? `#${s}` : "";
}

// --- 手元の状態 ------------------------------------------------------------------------------------------

/** 時間軸の開いている段（まとまりの見出しを除く、表示の順）。既定の表示のまま（隠した・見せた段が無い）か、段を
 *  まだ知らなければ null（画面に任せる） */
export function openRows(t: TimelineSnapshot): string[] | null {
  if (!t.groups.length || (!t.config.hidden.length && !t.config.shown.length)) return null;
  return t.rows.filter((r) => !r.header).map((r) => r.def.id);
}

/** rows だけを開く段の構成（ほかは隠す。並びと畳みは保つ） */
export function configShowing(t: TimelineSnapshot, rows: string[]): RowConfig {
  const want = new Set(rows);
  const defs = t.groups.flatMap((g) => g.rows);
  return {
    ...t.config,
    hidden: defs.filter((d) => !want.has(d.id)).map((d) => d.id),
    shown: defs.filter((d) => want.has(d.id) && d.hiddenByDefault).map((d) => d.id),
  };
}

function eventOf(item: PickedItem | null): EventRef | null {
  if (!item) return null;
  return { stream: item.stream, t: item.t, ...(item.tEnd !== item.t ? { t_end: item.tEnd } : {}), kind: item.kind,
           ...(item.label ? { text: item.label } : {}), row: item.row };
}

function itemOf(e: EventRef): PickedItem {
  return { row: e.row ?? e.stream, type: "intervals", stream: e.stream, kind: e.kind ?? "", t: e.t, tEnd: e.t_end ?? e.t,
           label: e.text ?? null, value: null, unit: null, tone: 0, depth: 0, address: null, jump: null };
}

// --- 同期 -----------------------------------------------------------------------------------------------

type Conn = Pick<DaemonConnection, "call" | "on" | "onLink">;

export interface Place {
  hash(): string;
  replace(hash: string): void;
}

const browserPlace: Place = {
  hash: () => (typeof location === "undefined" ? "" : location.hash),
  replace: (hash) => {
    if (typeof history !== "undefined") history.replaceState(null, "", `${location.pathname}${location.search}${hash}`);
  },
};

export class ScreenSync {
  readonly origin = `w${Math.random().toString(36).slice(2, 10)}`;
  private shared = emptyScreen();          // デーモンと最後にそろえた状態
  private local = emptyScreen();           // 手元の状態
  private mouse: number | null = null;     // 手元のマウスのカーソル（ほかの窓・AI のものは共有の状態にだけ置く）
  private available = false;               // デーモンに ui.state がある
  private ready = false;                   // 最初の読み（または URL の送り）を終えた
  private pending: ScreenData | null = null;          // 時間軸のデータが届いたら当てる（視点・カーソル・出来事）
  private pendingRows: string[] | null = null;        // 段を知ったら当てる
  private applying = false;
  private dataKey = "";                    // 時間軸のデータ（源・捕捉・記録・範囲）。変わったら視点の変化は自動
  private autoView = false;
  private pendingSince = 0;                 // pending を置いた時刻（先の範囲の視点を待つ上限に）                // 次に送る視点の変化はデータの読み直しによる（人の操作でない）
  private pushTimer: ReturnType<typeof setTimeout> | null = null;
  private urlTimer: ReturnType<typeof setTimeout> | null = null;
  private fromUrl: Partial<ScreenData>;

  constructor(private conn: Conn, private tl: TimelineState, private dbg: Debugger,
              private onFrame: (index: number) => void = () => {}, private place: Place = browserPlace,
              private delayMs = 250) {
    this.fromUrl = fromHash(place.hash());
    conn.onLink((link) => {
      if (link === "open") void this.connect();
      else {
        this.ready = false;
        this.tl.setPointers([]);              // 切れた: ほかの窓・AI のカーソルは分からなくなる（つながったら読み直す）
      }
    });
    conn.on("ui.state", (n: { origin: string | null; changed?: string[]; state: ScreenData; pointers?: Pointer[] }) => {
      if (n.pointers) this.pointers(n.pointers);
      // ほかの窓・AI の変化を当てる（マウスのカーソルだけの変化は pointers で描くだけ）
      if (n.origin !== this.origin && !this.onlyPointer(n.changed, n.state)) this.apply(pick(n.state));
    });
    tl.subscribe((s) => this.fromTimeline(s));
    dbg.subscribe((s) => this.fromDebugger(s));
  }

  /** 手元のマウスのカーソル（時間軸の上。外れたら null） */
  setMouse(t: number | null): void {
    if (t === this.mouse) return;
    this.mouse = t;
    this.changed({ cursors: { ...this.local.cursors, mouse: t } });
  }

  /** 窓を閉じる・離れる: 自分のマウスのカーソルを共有の状態から消す（送るだけ。応答は待たない。届かなくても、
   *  デーモンは接続が切れたときにこの窓のカーソルを消す） */
  leave(): void {
    if (!this.available || this.shared.cursors.mouse === null) return;
    this.mouse = null;
    this.local = { ...this.local, cursors: { ...this.local.cursors, mouse: null } };
    this.shared = { ...this.shared, cursors: { ...this.shared.cursors, mouse: null } };
    void this.conn.call("ui.state.set", { cursors: { mouse: null }, origin: this.origin } as any, { quiet: true })
      .catch(() => {});
  }

  dispose(): void {
    if (this.pushTimer) clearTimeout(this.pushTimer);
    if (this.urlTimer) clearTimeout(this.urlTimer);
  }

  /** つながったとき: URL に状態があればそれを当ててデーモンへ送り、無ければデーモンの状態を読んで当てる */
  private async connect(): Promise<void> {
    try {
      const url = this.fromUrl;
      this.fromUrl = {};
      if (Object.keys(url).length) {
        this.apply({ ...this.local, ...url, cursors: { ...emptyScreen().cursors, ...(url.cursors ?? {}) } });
        const state = await this.conn.call("ui.state.set", { ...url, origin: this.origin } as any);
        this.shared = pick(state);
        if (state?.pointers) this.pointers(state.pointers);
      } else {
        const state = await this.conn.call("ui.state.get", {}, { quiet: true });
        this.apply(pick(state));
        if (state?.pointers) this.pointers(state.pointers);
      }
      this.available = true;
    } catch {
      this.available = false;                                // ui.state の無いデーモン: URL だけ
    } finally {
      this.ready = true;
    }
    this.schedulePush();                                     // 待っている間の手元の変化
  }

  // --- 手元 → 共有 ---------------------------------------------------------------------------------------

  private fromTimeline(s: TimelineSnapshot): void {
    if (this.pending?.view && s.extent && s.source === this.pending.view.source
        && (!ahead(this.pending.view, s) || Date.now() - this.pendingSince > AHEAD_WAIT_MS)) {
      const state = this.pending;
      this.pending = null;
      this.applyTimeline(state);
      return;
    }
    if (this.pendingRows && s.groups.length) {
      const rows = this.pendingRows;
      this.pendingRows = null;
      this.guard(() => this.tl.setConfig(configShowing(s, rows)));
      return;
    }
    if (this.pending) return;                                // 当てる前の手元（読み込み中）は送らない
    const key = `${s.source}:${s.recording ?? ""}:${s.capture?.id ?? ""}:${s.extent?.join("-") ?? ""}`;
    if (key !== this.dataKey) {
      this.dataKey = key;
      this.autoView = true;
    }
    const view = s.extent ? { source: s.source, t0: s.view.start, t1: s.view.end } : null;
    this.changed({
      view: view ?? this.local.view,
      cursors: { selection: s.selection, reference: s.reference, mouse: this.mouse },
      rows: openRows(s),
      event: eventOf(s.selected),
    });
  }

  private fromDebugger(s: DebugSnapshot): void {
    this.changed({ frame: s.frame });
  }

  private changed(patch: Partial<ScreenData>): void {
    const next = { ...this.local, ...patch };
    if (same(next, this.local)) return;
    this.local = next;
    if (this.applying) return;
    this.scheduleUrl();
    this.schedulePush();
  }

  private schedulePush(): void {
    if (!this.available || !this.ready) return;
    if (this.pushTimer) clearTimeout(this.pushTimer);
    this.pushTimer = setTimeout(() => void this.push(), this.delayMs);
  }

  /** 手元と共有の差だけを送る */
  private async push(): Promise<void> {
    this.pushTimer = null;
    const params: Record<string, unknown> = {};
    const clear: Key[] = [];
    const { local, shared } = this;
    if (local.view && !same(local.view, shared.view)) params.view = local.view;
    const cursors = Object.fromEntries((["selection", "mouse", "reference"] as const)
      .filter((k) => local.cursors[k] !== shared.cursors[k]).map((k) => [k, local.cursors[k]]));
    if (Object.keys(cursors).length) params.cursors = cursors;
    if (!same(local.rows, shared.rows)) {
      if (local.rows) params.rows = local.rows;
      else clear.push("rows");
    }
    if (!same(local.event, shared.event)) {
      if (local.event) params.event = local.event;
      else clear.push("event");
    }
    if (local.frame !== shared.frame) params.frame = local.frame;
    if (clear.length) params.clear = clear;
    if (!Object.keys(params).length) return;
    // マウスのカーソルだけの変化と、データの読み直し（ほかの利用者が記録を変えた・捕捉した等）による視点の変化は
    // 人の操作ではないので操作の記録に残さない
    const onlyMouse = !params.cursors || Object.keys(cursors).join() === "mouse";
    const quiet = onlyMouse && Object.keys(params).every((k) => k === "cursors" || (k === "view" && this.autoView));
    this.autoView = false;
    this.shared = { ...local, cursors: { ...local.cursors } };
    try {
      await this.conn.call("ui.state.set", { ...params, origin: this.origin } as any, quiet ? { quiet: true } : undefined);
    } catch {
      /* 切れた等: つながり直したら読み直す */
    }
  }

  private scheduleUrl(): void {
    if (this.urlTimer) return;
    this.urlTimer = setTimeout(() => {
      this.urlTimer = null;
      const hash = toHash(this.place.hash(), this.local);
      if (hash !== this.place.hash()) this.place.replace(hash);
    }, 300);
  }

  // --- 共有 → 手元 ---------------------------------------------------------------------------------------

  /** マウスのカーソルだけの変化（pointers で描くだけで、視点・選択は当て直さない） */
  private onlyPointer(changed: string[] | undefined, state: ScreenData): boolean {
    if (!changed) return false;
    const c = state?.cursors;
    // sampling（データ サンプリングの一覧）は Data Sampling が受け持つ（lib/sampling.ts の SharedSampling）
    return changed.every((k) => k === "pointers" || k === "sampling" || (k === "cursors" && c?.selection === this.local.cursors.selection
                                                      && c?.reference === this.local.cursors.reference));
  }

  /** ほかの窓・AI のマウスのカーソルを時間軸へ（自分のものは除く） */
  private pointers(list: Pointer[]): void {
    this.tl.setPointers(list.filter((p) => p.key !== this.origin && p.source !== null)
      .map((p) => ({ t: p.t, source: p.source!, label: p.name ?? p.key, role: p.role ?? "" })));
  }

  private apply(state: ScreenData): void {
    this.shared = { ...state, cursors: { ...state.cursors } };
    this.autoView = true;                  // 当てた結果の視点（データの範囲に収めた等）は人の操作ではない
    const t = this.tl.snapshot;
    const view = state.view;
    if (view && (view.source !== t.source || !t.extent || ahead(view, t))) {
      // 時間軸のデータがまだ無い（源を変える・読み込み中）・伸びている記録のまだ読んでいない範囲: 届いてから当てる
      // （新しい捕捉はカーソルを消すため。範囲の外を捨てると、追従の視点を送り返して上書きしてしまう）
      this.pending = state;
      this.pendingSince = Date.now();
      if (view.source !== t.source) this.guard(() => this.tl.setSource(view.source));
    } else {
      this.pending = null;
      this.applyTimeline(state);
    }
    const rows = state.rows;
    if (rows && !same(rows, openRows(this.tl.snapshot))) {
      if (this.tl.snapshot.groups.length) this.guard(() => this.tl.setConfig(configShowing(this.tl.snapshot, rows)));
      else this.pendingRows = rows;
    }
    if (state.frame !== this.dbg.snapshot.frame && this.dbg.snapshot.stop) this.guard(() => this.onFrame(state.frame));
    // フレームは移れたとき（止まっているとき）だけ手元になる。移れなければ手元の値のまま（次の変化で共有へ戻る）
    this.local = { ...this.local, ...state, cursors: { ...state.cursors, mouse: this.mouse },
                   rows: rows ?? this.local.rows, frame: this.dbg.snapshot.frame };
    this.scheduleUrl();
  }

  /** 視点・カーソル・出来事を時間軸に当てる（データの範囲に収める） */
  private applyTimeline(state: ScreenData): void {
    this.guard(() => {
      const t = this.tl.snapshot;
      if (state.view && t.extent) {
        const lo = Math.max(t.extent[0], state.view.t0), hi = Math.min(t.extent[1], state.view.t1);
        if (hi > lo) this.tl.setView({ start: lo, end: hi });
      }
      const s = this.tl.snapshot;
      if (state.cursors.selection !== s.selection || !same(eventOf(s.selected), state.event)) {
        this.tl.select(state.cursors.selection ?? state.event?.t ?? null, state.event ? itemOf(state.event) : null);
      }
      if (state.cursors.reference !== s.reference) this.tl.setReference(state.cursors.reference);
    });
  }

  /** 共有の状態を当てる間の手元の変化は送り返さない */
  private guard(fn: () => void): void {
    const was = this.applying;
    this.applying = true;
    try {
      fn();
    } finally {
      this.applying = was;
    }
  }
}

/** 記録中で、視点が時間軸の読んだ範囲より先にある（記録は伸びているので、読み直せば届く） */
/** 先の範囲の視点を待つ上限（記録が伸びない・時刻が先すぎる: 今の範囲に収めて当て、手元の変化を送れるように戻す） */
const AHEAD_WAIT_MS = 5000;

function ahead(view: { t1: number }, t: TimelineSnapshot): boolean {
  return t.live && !!t.extent && view.t1 > t.extent[1];
}

function pick(r: any): ScreenData {
  const d = emptyScreen();
  return { view: r?.view ?? d.view, cursors: { ...d.cursors, ...(r?.cursors ?? {}) }, rows: r?.rows ?? d.rows,
           event: r?.event ?? d.event, frame: r?.frame ?? d.frame };
}
