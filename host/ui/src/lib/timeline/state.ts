// 時間軸の状態: 視点・源（最後の捕捉 / 記録）・タイルの取得の計画・段の構成・カーソル 3 種・選んだ出来事・
// 間引いた最新のフレームと縮図。Svelte に依らない（store の約束 subscribe だけを満たす）。
//
// - 視点が動くたびに、持っているタイルから間引き（Worker）で 1 フレームを作る。デーモンは待たない。
// - 持っているタイルで足りない（範囲の外・要約が粗い）ときだけ、視点を前後 1 画面ずつ広げた範囲を
//   bin = 1 画素の半分で頼む。届くまでは持っているタイルで描く（足りない部分は空白）。
// - 全体のタイル（最初に読む全範囲）は縮図（ミニマップ）用に持ち続ける。
// - 誰か（AI も）が捕捉したら（activity の通知）捨てて読み直す。新しい捕捉なら全体を表示する。記録に印を置いたら
//   （recording.mark）記録を読み直す（印の段）。
// - 誰か（AI・別の窓も）が記録を開いた・始めた・止めたら（通知 recording）、記録の 0 の位置を読み直し、記録を見ていれば
//   読み直す。読み直しはどれも quiet / 記録に残らない読みなので、操作の記録を増やさない（通知が続いて止まらなくならない）。
// - 記録中（live）は記録の範囲を周期的に読み、伸びた端を追いかける（follow）: 全体を見ていれば全体のまま、端を拡大して
//   いれば幅を保って端へ。利用者が戻って見ている（視点の終わりが端より前）ときは追わない。端へ戻すとまた追う。
// - 時刻の 0 は「記録（または捕捉）の始め」（origins。lib/time.ts）。ビューの時刻の欄も同じ 0 を使う。
// - 段の構成（並び・表示・畳み）は源ごとにブラウザに覚える。
// 仕様: docs/superpowers/specs/2026-10-18-timeline-tiles-design.md §5
import type { DaemonConnection } from "../api";
import { load, save } from "../prefs";
import type { AppState } from "../state";
import { NO_ORIGINS, type Origins } from "../time";
import { panView, zoomView, type CaptureInfo, type CaptureRequest, type View } from "../waveform";
import type { RowSpec, TimelineFrame } from "./compose";
import { cursorRows, type CursorValues } from "./cursor";
import { createDecimator, type Anomaly, type Decimator } from "./decimator";
import { announceSelect } from "./events";
import type { PickedItem } from "./query";
import {
  EMPTY_CONFIG, RowRegistry, moveGroup, moveRow, parseConfig, setShown, toggleFold, type GroupDef, type RowConfig,
  type VisibleRow, systemOrder } from "./rows";
import { overlapOf, tileServes, type TileHeader } from "./tile";

export type Source = "capture" | "recording";

/** ビューが時間軸に置く印（検索の結果・解読の注釈・長い順の上位）。目盛りの帯と縮図に描く。時刻は源の単位 */
export interface Mark {
  t: number;
  tEnd?: number;
  label?: string;
  tone?: 0 | 1 | 2 | 3;                // 0 = 普通 / 1 = bad / 2 = good / 3 = warn（段の tone と同じ）
}

/** 印の組（ビューごと。owner で置き換える）。source が今の源と違う組は描かない */
export interface MarkSet {
  owner: string;
  source: Source;
  marks: Mark[];                       // t の昇順
}

/** ほかの窓・AI のマウスのカーソル（画面の状態 ui.state の pointers）。role = human / ai / script */
export interface RemotePointer {
  t: number;
  source: Source;
  label: string;
  role: string;
}

/** ほかのビューから時間軸のその時刻へ飛ぶ（reveal）。時刻は源の単位（捕捉はサンプル番号、記録はタイムベース）か、
 *  タイムベースの µs（us。watch.series 等。記録の 1 単位の秒で換算する）。tEnd（usEnd）があれば区間が見えるように
 *  視点を合わせ、無ければ幅を保って真ん中に。item があればその出来事を選ぶ */
export interface Reveal {
  source: Source;
  t?: number;
  tEnd?: number;
  us?: number;
  usEnd?: number;
  item?: PickedItem | null;
}

export interface TimelineSnapshot {
  source: Source;
  capture: CaptureInfo | null;          // source = capture の捕捉
  recording: string | null;             // source = recording の記録の名前
  extent: [number, number] | null;      // データの範囲 [始め, 終わり)（捕捉はサンプル番号、記録はタイムベース）
  tickS: number;                        // 1 単位の秒
  view: View;                           // 見ている範囲（整数、[start, end)）
  width: number;                        // 間引きの幅（デバイスの画素）
  frame: TimelineFrame | null;          // 最新の間引きの結果
  header: TileHeader | null;            // その元のタイルの見出し
  rows: VisibleRow[];                   // 描く段（上から）
  groups: GroupDef[];                   // すべての段（隠したものも。段の一覧のメニュー）
  config: RowConfig;
  selection: number | null;             // 選択カーソル
  reference: number | null;             // 基準のカーソル
  selected: PickedItem | null;          // 選んだ出来事
  overview: TimelineFrame | null;       // 縮図（全体のタイルを縮図の幅で）
  anomalies: Anomaly[];                 // 縮図の異常の印
  capturedBy: string | null;            // 捕捉した利用者（記録の通知で分かったとき）
  marks: MarkSet[];                     // ビューが置いた印（setMarks）
  pointers: RemotePointer[];            // ほかの窓・AI のマウスのカーソル
  origins: Origins;                     // 源ごとの時刻 0（記録・捕捉の始め）と 1 単位の秒。今の源でなくても持つ
  live: boolean;                        // 記録中の記録を見ている（範囲が伸びる）
  follow: boolean;                      // 伸びた端を追いかける（視点の終わりが端にある）
  loading: boolean;                     // タイルを読んでいる
  error: string | null;                 // 捕捉が無い以外の読みの誤り
  decimator: "worker" | "inline";
}

const CAPTURE_METHODS = new Set(["la.capture", "la.collect", "la.deep.capture", "la.deep.collect"]);
// 記録の段が変わる操作: 印を足す・FPGA の信号の定義を変える（記録の情報の名前で段を作り直す）
const MARK_METHODS = new Set(["recording.mark", "fpga_obs.signals_set"]);
const MAX_PX_PER_SAMPLE = 32;         // 拡大の限界（1 単位を最大 32 画素に）
const MAX_TILES = 4;
const LIVE_POLL_MS = 1000;            // 記録中の範囲の読みの間（前の読みが終わってから）

type Conn = Pick<DaemonConnection, "call" | "on" | "onLink">;
interface HeldTile { id: number; header: TileHeader }

/** 今の源の時刻 0 の位置（捕捉・記録の始め。lib/time.ts） */
export function originOf(s: Pick<TimelineSnapshot, "source" | "origins">): number {
  return s.origins[s.source]?.t0 ?? 0;
}

/** 伸びた範囲で端を追いかける視点: 全体を見ていれば新しい全体、そうでなければ幅を保って終わりを端に */
export function followView(view: View, before: [number, number], extent: [number, number]): View {
  if (view.start <= before[0] && view.end >= before[1]) return { start: extent[0], end: extent[1] };
  const width = view.end - view.start;
  return { start: Math.max(extent[0], extent[1] - width), end: extent[1] };
}

/** 描く段 → Worker への段の指定 */
export function specsOf(rows: VisibleRow[]): RowSpec[] {
  return rows.map((r) => ({ id: r.def.id, type: r.def.type, color: r.def.color }));
}

/** 縮図の段: 記録の stream のまとまり（無ければロジアナのチャンネル、最大 8） */
export function overviewSpecs(groups: GroupDef[]): RowSpec[] {
  const streams = groups.filter((g) => g.id !== "logic").map((g) => ({ id: `group:${g.id}`, type: "group" as const,
                                                                         color: 0 }));
  if (streams.length) return streams;
  return (groups.find((g) => g.id === "logic")?.rows ?? []).slice(0, 8)
    .map((r) => ({ id: r.id, type: r.type, color: r.color }));
}

export class TimelineState {
  private snap: TimelineSnapshot;
  private listeners = new Set<(s: TimelineSnapshot) => void>();
  private tiles: HeldTile[] = [];
  private whole: HeldTile | null = null;   // 全体のタイル（縮図・範囲の外の問い合わせ）
  private nextTile = 1;
  private capturer: string | null = null;
  private fetching = false;
  private refetch = false;
  private reset = true;                 // 次の読みで全体を読み直す（捕捉が変わったかもしれない）
  private rendering = false;
  private rerender = false;
  private linked = false;                // デーモンとつながっている（つながるまで読まない）
  private registry = new RowRegistry();
  private overviewWidth = 0;
  private overviewKey = "";
  private revealing: Reveal | null = null;
  private lastFullMs = 0;                     // 前の全体の読み込みにかかった時間（記録中の読み直しの間に使う）   // 源を変える・読み込みの後に当てる reveal
  private liveTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private conn: Conn, private app: AppState, private decimator: Decimator = createDecimator(),
              private livePollMs = LIVE_POLL_MS) {
    const config = loadConfig("capture");
    this.snap = {
      source: "capture", capture: null, recording: null, extent: null, tickS: 0, view: { start: 0, end: 0 }, width: 0,
      frame: null, header: null, rows: [], groups: [], config, selection: null, reference: null, selected: null,
      overview: null, anomalies: [], capturedBy: null, loading: false, error: null, decimator: decimator.kind,
      marks: [], pointers: [], origins: NO_ORIGINS, live: false, follow: true,
    };
    conn.onLink((link) => {
      this.linked = link === "open";
      if (this.linked) {
        this.reload();
        void this.readRecording();
      }
    });
    // 読み直しはどれも quiet（操作の記録に残らない）ので、通知 → 読み直し → 通知 … と続かない
    conn.on("activity", (e: { method: string; client?: string; error?: string }) => {
      if (e.error !== undefined) return;
      if (CAPTURE_METHODS.has(e.method)) {
        this.capturer = e.client ?? null;
        if (this.snap.source === "capture") this.reload();
      } else if (MARK_METHODS.has(e.method) && this.snap.source === "recording") {
        this.reload();
      }
    });
    conn.on("recording", () => {
      void this.readRecording();
      if (this.snap.source === "recording") this.reload();
    });
  }

  /** 時刻 0 の位置だけの store（変わったときだけ配る。表の時刻の欄が多くても、視点の動きのたびには走らない） */
  readonly origins = {
    subscribe: (fn: (o: Origins) => void): (() => void) => {
      this.originListeners.add(fn);
      fn(this.snap.origins);
      return () => this.originListeners.delete(fn);
    },
  };
  private originListeners = new Set<(o: Origins) => void>();

  subscribe(fn: (s: TimelineSnapshot) => void): () => void {
    this.listeners.add(fn);
    fn(this.snap);
    return () => this.listeners.delete(fn);
  }

  get snapshot(): TimelineSnapshot {
    return this.snap;
  }

  dispose(): void {
    if (this.liveTimer) clearTimeout(this.liveTimer);
    this.liveTimer = null;
    this.decimator.dispose();
  }

  /** 間引きの幅（デバイスの画素） */
  setWidth(px: number): void {
    const width = Math.max(1, Math.min(8192, Math.round(px)));
    if (width === this.snap.width) return;
    this.set({ width });
    this.update();
  }

  /** 縮図の幅（デバイスの画素） */
  setOverviewWidth(px: number): void {
    const width = Math.max(0, Math.min(8192, Math.round(px)));
    if (width === this.overviewWidth) return;
    this.overviewWidth = width;
    void this.renderOverview();
  }

  setSource(source: Source): void {
    if (source === this.snap.source) return;
    this.dropTiles(true);
    this.registry.clear();
    this.set({ source, capture: null, recording: null, extent: null, frame: null, header: null, error: null,
               view: { start: 0, end: 0 }, rows: [], groups: [], config: loadConfig(source), selection: null,
               reference: null, selected: null, overview: null, anomalies: [], live: false, follow: true });
    this.reload();
  }

  // --- 視点 -------------------------------------------------------------------------------------

  /** factor < 1 で拡大。anchor は画面の横位置（0..1） */
  zoomAt(anchor: number, factor: number): void {
    const e = this.snap.extent;
    if (!e) return;
    this.setView(zoomView(this.snap.view, factor, anchor, e, Math.max(1, this.snap.width / MAX_PX_PER_SAMPLE)));
  }

  /** 画面の幅の fraction 倍だけ右へ（負なら左へ） */
  panBy(fraction: number): void {
    const e = this.snap.extent;
    if (!e) return;
    this.setView(panView(this.snap.view, fraction * (this.snap.view.end - this.snap.view.start), e));
  }

  /** 端へ（-1 = 始め、1 = 終わり）。幅は保つ */
  toEdge(side: -1 | 1): void {
    const e = this.snap.extent;
    if (!e) return;
    this.setView(panView(this.snap.view, side * (e[1] - e[0]), e));
  }

  /** 時刻 t を画面の真ん中に（幅は保つ） */
  centerOn(t: number): void {
    const e = this.snap.extent;
    if (!e) return;
    const { view } = this.snap;
    this.setView(panView(view, t - (view.start + view.end) / 2, e));
  }

  fit(): void {
    const e = this.snap.extent;
    if (e) this.setView({ start: e[0], end: e[1] });
  }

  /** 視点を変える（利用者の操作）。視点の終わりが端にあれば、記録中は伸びた端を追いかける */
  setView(view: View): void {
    const follow = !!this.snap.extent && view.end >= this.snap.extent[1];
    if (view.start === this.snap.view.start && view.end === this.snap.view.end && follow === this.snap.follow) return;
    this.set({ view, follow });
    this.update();
  }

  // --- 段の構成 -----------------------------------------------------------------------------------

  setConfig(config: RowConfig): void {
    save(`timeline.rows.${this.snap.source}`, JSON.stringify(config));
    this.set({ config });
    this.relayout();
  }

  toggleFold(group: string): void {
    this.setConfig(toggleFold(this.snap.config, group));
  }

  setRowShown(id: string, show: boolean): void {
    const def = this.snap.groups.flatMap((g) => g.rows).find((r) => r.id === id);
    if (def) this.setConfig(setShown(this.snap.config, def, show));
  }

  moveRow(id: string, before: string | null): void {
    this.setConfig(moveRow(this.snap.config, this.snap.groups, id, before));
  }

  moveGroup(group: string, before: string | null): void {
    this.setConfig(moveGroup(this.snap.config, this.snap.groups, group, before));
  }

  /** システム解析の並び（MCU と FPGA が隣り合う。rows.ts の systemOrder） */
  arrangeForSystem(flat: number[] = []): void {
    this.setConfig(systemOrder(this.snap.config, this.snap.groups, flat));
  }

  resetRows(): void {
    this.setConfig(EMPTY_CONFIG);
  }

  setAllFolded(folded: boolean): void {
    this.setConfig({ ...this.snap.config, folded: folded ? this.snap.groups.map((g) => g.id) : [] });
  }

  private relayout(): void {
    const groups = this.registry.ordered(this.snap.config);
    const rows = this.registry.visible(this.snap.config);
    this.set({ groups, rows });
    void this.render();
    void this.renderOverview();
  }

  // --- カーソル・選択・出来事の移動 ------------------------------------------------------------------

  /** 選択カーソル（と選んだ出来事）。item が無ければ出来事の選択を外す。他のパネルへ知らせる（events.ts） */
  select(t: number | null, item: PickedItem | null = null): void {
    this.set({ selection: t, selected: item });
    if (t !== null) announceSelect({ t, seconds: (t - this.origin()) * this.snap.tickS, source: this.snap.source, item });
  }

  /** 今の源の時刻 0 の位置（捕捉・記録の始め） */
  origin(): number {
    return originOf(this.snap);
  }

  /** 記録の 0 の位置（記録の範囲の始め）を読む。時間軸が捕捉を見ていても、ビューの記録の時刻のために持つ */
  private async readRecording(): Promise<void> {
    try {
      const status = await this.conn.call("recording.status", {}, { quiet: true });
      const recording = status?.name && status.span && status.timebase_hz > 0
        ? { t0: status.span[0], tickS: 1 / status.timebase_hz } : null;
      this.setOrigin("recording", recording);
    } catch {
      /* 切れた等: つながり直したら読み直す */
    }
  }

  private setOrigin(source: Source, origin: Origins[Source]): void {
    const now = this.snap.origins[source];
    if (now?.t0 === origin?.t0 && now?.tickS === origin?.tickS) return;
    this.set({ origins: { ...this.snap.origins, [source]: origin } });
    this.originListeners.forEach((fn) => fn(this.snap.origins));
  }

  setReference(t: number | null): void {
    this.set({ reference: t });
  }

  /** 段 row の時刻 at の出来事（ホバー・クリック）。tolerancePx は印を拾う幅（画素） */
  pick(row: string, at: number, tolerancePx = 4): Promise<PickedItem | null> {
    const { view, width } = this.snap;
    const tolerance = ((view.end - view.start) / Math.max(1, width)) * tolerancePx;
    return this.decimator.pick(this.tileOrder(), row, at, tolerance);
  }

  /** マウスの縦線の値: 見えている値・状態の段の、時刻 at に保たれている値（段の名前の列。cursor.ts） */
  async valuesAt(at: number): Promise<CursorValues> {
    const ids = cursorRows(this.snap.rows);
    const tiles = this.tileOrder();
    const found = await Promise.all(ids.map((id) => this.decimator.pick(tiles, id, at, 0)));
    const out: CursorValues = {};
    found.forEach((item, i) => {
      if (item) out[ids[i]] = item;
    });
    return out;
  }

  /** 時刻へ（Go To Time）: 始めからの秒 seconds の時刻を選び、画面の外なら真ん中へ（範囲の外は端に収める）。
   *  データが無ければ false */
  goTo(seconds: number): boolean {
    const { extent, tickS } = this.snap;
    if (!extent || !(tickS > 0) || !Number.isFinite(seconds)) return false;
    this.applyReveal({ source: this.snap.source, t: Math.round(this.origin() + seconds / tickS) });
    return true;
  }

  /** 次（dir = 1）/ 前（−1）の出来事へ: 出来事を選んでいればその段、無ければ見えている出来事の段の、選択カーソル
   *  （無ければ画面の真ん中）からいちばん近い出来事を選び、画面の外なら真ん中へ */
  async seek(dir: 1 | -1): Promise<boolean> {
    const { selection, selected, view, rows } = this.snap;
    const from = selection ?? Math.floor((view.start + view.end) / 2);
    const candidates = selected ? [selected.row]
      : rows.filter((r) => !r.header && r.def.type !== "logic").map((r) => r.def.id);
    const found = await this.decimator.seek(this.tileOrder(), candidates, from, dir);
    if (!found) return false;
    const item = await this.decimator.pick(this.tileOrder(), found.row, found.t, 0);
    this.select(found.t, item);
    if (found.t < view.start || found.t >= view.end) this.centerOn(found.t);
    return true;
  }

  /** 問い合わせに使うタイルの順（視点を描いているもの、ほかの持っているもの、全体） */
  private tileOrder(): number[] {
    const best = this.best();
    const ids = [best?.id, ...this.tiles.map((t) => t.id), this.whole?.id].filter((x): x is number => x !== undefined);
    return [...new Set(ids)];
  }

  /** 捕捉して、最新のキャプチャを読み直す。結果（trigger_time = タイムベースのトリガの時刻 等）を返す（失敗は undefined） */
  async capture(r: CaptureRequest): Promise<{ trigger_time?: number; trigger_index?: number } | undefined> {
    const trigger = r.crossOnly ? { mask: 1, value: 1, fall: 1, rise: 0 } : { rise: r.rise ?? 0, fall: r.fall ?? 0 };
    const done = r.deep
      ? await this.app.act("la.deep.capture", { rate_hz: r.rate_hz, ...trigger, before_beats: r.beforeBeats ?? 4096,
                                                  after_beats: r.afterBeats ?? 4096, ...(r.rle !== undefined ? { rle: r.rle } : {}) })
      : await this.app.act("la.capture", { rate_hz: r.rate_hz, pretrigger: r.pretrigger, ...trigger });
    if (done !== undefined) this.reload();
    return done;
  }

  /** ほかの窓・AI のマウスのカーソル（ScreenSync が ui.state から） */
  setPointers(pointers: RemotePointer[]): void {
    if (JSON.stringify(pointers) !== JSON.stringify(this.snap.pointers)) this.set({ pointers });
  }

  /** ビューの印を置く（同じ owner の前の印は置き換える。空の並びで消す）。時刻は source の単位。前と同じ印なら
   *  何もしない（知らせない）: 時間軸の状態を読む $effect が印を置いても、置き直しが続かない */
  setMarks(owner: string, source: Source, marks: Mark[]): void {
    const before = this.snap.marks.find((m) => m.owner === owner);
    const sorted = [...marks].sort((a, b) => a.t - b.t);
    if (before ? before.source === source && JSON.stringify(before.marks) === JSON.stringify(sorted) : !sorted.length) return;
    const rest = this.snap.marks.filter((m) => m.owner !== owner);
    this.set({ marks: sorted.length ? [...rest, { owner, source, marks: sorted }] : rest });
  }

  /** ほかのビューの行から、時間軸のその時刻へ飛ぶ。源が違えば変えて、データが届いてから当てる */
  reveal(r: Reveal): void {
    if (r.source !== this.snap.source) {
      this.revealing = r;
      this.setSource(r.source);
    } else if (!this.snap.extent || this.revealTicks(r) >= this.snap.extent[1]) {
      this.revealing = r;          // 範囲がまだ無い・伸びている記録のまだ読んでいない時刻: 読み直してから当てる
      this.reload();
    } else {
      this.revealing = null;
      this.applyReveal(r);
    }
  }

  /** 移動先の時刻（源の単位。µs で指定されていれば換算。無ければ -∞） */
  private revealTicks(r: Reveal): number {
    const { tickS } = this.snap;
    if (r.t !== undefined) return r.t;
    return r.us !== undefined && tickS > 0 ? Math.round(r.us * 1e-6 / tickS) : -Infinity;
  }

  private applyReveal(r: Reveal): void {
    const { extent, tickS, view } = this.snap;
    if (!extent) return;
    const ticksOf = (us: number) => (tickS > 0 ? Math.round(us * 1e-6 / tickS) : NaN);
    const raw = r.t ?? (r.us !== undefined ? ticksOf(r.us) : NaN);
    if (!Number.isFinite(raw)) return;
    const rawEnd = r.tEnd ?? (r.usEnd !== undefined ? ticksOf(r.usEnd) : raw);
    const clamp = (x: number) => Math.min(extent[1] - 1, Math.max(extent[0], Math.round(x)));
    const t = clamp(raw), tEnd = Math.max(t, clamp(rawEnd));
    if (tEnd > t) {
      // 区間: 前後に区間の半分ずつの余白（幅は 1 単位 × 画素の数を下限に）
      const pad = Math.max((tEnd - t) / 2, 1);
      const start = Math.max(extent[0], Math.floor(t - pad)), end = Math.min(extent[1], Math.ceil(tEnd + pad));
      this.setView({ start, end: Math.max(end, start + 1) });
    } else if (t < view.start || t >= view.end || view.end <= view.start) {
      this.centerOn(t);
    }
    this.select(t, r.item ?? null);
  }

  /** 全体を読み直す（捕捉・記録が変わったかもしれない） */
  reload(): void {
    this.reset = true;
    void this.fetch();
  }

  // --- タイル -----------------------------------------------------------------------------------

  private update(): void {
    void this.render();
    if (!this.serving()) void this.fetch();
  }

  private serving(): HeldTile | undefined {
    const { view, width } = this.snap;
    const pixel = (view.end - view.start) / Math.max(1, width);
    return [...this.tiles, ...(this.whole ? [this.whole] : [])]
      .find((t) => tileServes(t.header, view.start, view.end, pixel));
  }

  /** 描くのに使うタイル: 足りるもの、無ければ視点といちばん重なるもの */
  private best(): HeldTile | undefined {
    const { view } = this.snap;
    const all = [...this.tiles, ...(this.whole ? [this.whole] : [])];
    return this.serving() ?? all.reduce<HeldTile | undefined>(
      (a, t) => (!a || overlapOf(t.header, view.start, view.end) > overlapOf(a.header, view.start, view.end) ? t : a),
      undefined);
  }

  /** 読みは同時に 1 つ。読みの途中の操作は、終わってから最後の視点で 1 回だけ読み直す */
  private async fetch(): Promise<void> {
    if (!this.snap.width || !this.linked) return;
    if (this.fetching) {
      this.refetch = true;
      return;
    }
    this.fetching = true;
    this.set({ loading: true });
    try {
      do {
        this.refetch = false;
        const full = this.reset || !this.snap.extent;
        this.reset = false;
        if (!full && this.serving()) continue;
        try {
          const began = Date.now();
          await (full ? this.fetchAll() : this.fetchAround());
          if (full) this.lastFullMs = Date.now() - began;
          this.set({ error: null });
          const pending = this.revealing;
          if (full && pending && pending.source === this.snap.source && this.snap.extent) {
            this.revealing = null;
            this.applyReveal(pending);
          }
        } catch (e) {
          // 捕捉・記録がまだ無いのは誤りにしない（空のまま）。読みの間に操作があれば、その視点で読み直す
          const message = e instanceof Error ? e.message : String(e);
          this.revealing = null;                // 当てられなかった移動を残さない（後の関係ない読み直しで当たる）
          if (!/捕捉が無い|読む記録が無い|記録が空/.test(message)) this.set({ error: message });
          else if (full) this.set({ capture: null, extent: null, frame: null, header: null, overview: null });
        }
      } while (this.refetch || this.reset);
    } finally {
      this.fetching = false;
      this.set({ loading: false });
    }
  }

  /** 全体: 捕捉なら捕捉の全体、記録なら記録の範囲。新しい捕捉・記録なら全体を表示する */
  private async fetchAll(): Promise<void> {
    const { source, width } = this.snap;
    if (source === "capture") {
      const result = await this.conn.call("timeline.tiles", { source, t0: 0, t1: null, bin: null }, { quiet: true });
      if (this.snap.source !== source) return;              // 読む間に源が替わった: 古い源の結果は当てない
      const info = result.capture as CaptureInfo;
      const fresh = info.id !== this.snap.capture?.id;
      const extent: [number, number] = [0, info.length];
      if (fresh) {
        this.dropTiles(true);
        this.registry.clear();
      }
      this.setOrigin("capture", { t0: 0, tickS: result.tick_s });
      this.set({ capture: info, extent, tickS: result.tick_s,
                 ...(fresh ? { view: { start: 0, end: info.length }, capturedBy: this.capturer, selection: null,
                               reference: null, selected: null } : {}) });
      this.keep(result, true);
      if (!fresh && !this.serving()) this.refetch = true;     // 同じ捕捉（つなぎ直し）: 拡大していた視点を保つ
      return;
    }
    const status = await this.conn.call("recording.status", {}, { quiet: true });
    if (this.snap.source !== source) return;
    if (!status.name || !status.span) throw new Error("読む記録が無い");
    const extent: [number, number] = [status.span[0], status.span[1] + 1];
    const named = status.name !== this.snap.recording;
    const bin = Math.max(1, Math.floor((extent[1] - extent[0]) / Math.max(1, width) / 2));
    const result = await this.conn.call("timeline.tiles", { source, t0: extent[0], t1: extent[1], bin }, { quiet: true });
    if (this.snap.source !== source) return;
    this.dropTiles(true);                       // 新しい全体を受け取ってから捨てる（読みが失敗しても前のタイルは残る）
    if (named) this.registry.clear();
    this.setOrigin("recording", { t0: extent[0], tickS: result.tick_s });
    const before = this.snap.extent;
    // 新しい記録は全体を表示。範囲が変わったら、端を追いかけていれば追う（戻って見ていれば視点を保つ）
    const view = named || !this.snap.view.end || !before ? { start: extent[0], end: extent[1] }
      : this.snap.follow ? followView(this.snap.view, before, extent) : this.snap.view;
    this.set({ recording: status.name, extent, tickS: result.tick_s, view, live: !!status.recording,
               ...(named ? { selection: null, reference: null, selected: null, follow: true } : {}) });
    this.keep(result, true);
    this.watchLive();
  }

  /** 記録中は範囲を周期的に読み、伸びたら読み直す（前の読みが終わってから次を出す） */
  private watchLive(): void {
    if (this.liveTimer || !this.snap.live || this.snap.source !== "recording") return;
    this.liveTimer = setTimeout(async () => {
      try {
        const status = await this.conn.call("recording.status", {}, { quiet: true });
        const end = status?.span ? status.span[1] + 1 : null;
        const changed = status?.name !== this.snap.recording || end !== this.snap.extent?.[1];
        if (!status?.recording) this.set({ live: false });
        if (changed && this.snap.source === "recording") {
          this.liveTimer = null;
          this.reload();                 // 読み終わりに watchLive がまた始める
          return;
        }
      } catch {
        /* 切れた等: つながり直したら読み直す */
      }
      this.liveTimer = null;
      this.watchLive();
    }, Math.max(this.livePollMs, 2 * this.lastFullMs));    // 重い記録は前の読みの 2 倍空ける（読み込みを続けない）
  }

  /** 視点の前後 1 画面ずつを、1 画素の半分の細かさで */
  private async fetchAround(): Promise<void> {
    const { source, view, width, extent, capture } = this.snap;
    if (!extent) return;
    const span = view.end - view.start;
    const t0 = Math.max(extent[0], view.start - span), t1 = Math.min(extent[1], view.end + span);
    const bin = Math.max(1, Math.floor(span / Math.max(1, width) / 2));
    const result = await this.conn.call("timeline.tiles", { source, t0, t1, bin }, { quiet: true });
    if (source === "capture" && result.capture?.id !== capture?.id) {
      this.reset = true;                                        // 読みの間に捕捉が変わった: 全体を読み直す
      return;
    }
    this.keep(result, false);
  }

  private keep(result: any, whole: boolean): void {
    const { data, ...header } = result;
    const buffer = (Object.values(data ?? {})[0] as ArrayBufferView | undefined)?.buffer as ArrayBuffer | undefined;
    const id = this.nextTile++;
    this.decimator.addTile(id, header as TileHeader, buffer ?? new ArrayBuffer(0));
    const held = { id, header: header as TileHeader };
    if (whole) {
      if (this.whole) this.decimator.dropTile(this.whole.id);
      this.whole = held;
    } else {
      this.tiles.unshift(held);
      for (const old of this.tiles.splice(MAX_TILES - 1)) this.decimator.dropTile(old.id);
    }
    if (this.registry.merge(header as TileHeader) || !this.snap.rows.length) this.relayout();
    else void this.render();
    if (whole) {
      void this.renderOverview();
      void this.decimator.anomalies(id).then((anomalies) => {
        if (this.whole?.id === id) this.set({ anomalies });
      });
    }
  }

  private dropTiles(all: boolean): void {
    for (const t of this.tiles) this.decimator.dropTile(t.id);
    this.tiles = [];
    if (all) {
      this.whole = null;
      this.overviewKey = "";
      this.decimator.reset();
    }
  }

  // --- 間引き -----------------------------------------------------------------------------------

  /** 間引きは同時に 1 つ（Worker に溜めない）。途中の操作は、終わってから最後の視点で 1 回 */
  private async render(): Promise<void> {
    if (this.rendering) {
      this.rerender = true;
      return;
    }
    this.rendering = true;
    try {
      do {
        this.rerender = false;
        const tile = this.best();
        const { view, width, rows } = this.snap;
        if (!tile || !width || view.end <= view.start) break;
        const frame = await this.decimator.frame({ tile: tile.id, v0: view.start, v1: view.end, width,
                                                   rows: specsOf(rows) });
        if (frame) this.set({ frame, header: tile.header });
      } while (this.rerender);
    } finally {
      this.rendering = false;
    }
  }

  private async renderOverview(): Promise<void> {
    const whole = this.whole, width = this.overviewWidth, e = this.snap.extent;
    if (!whole || !width || !e) return;
    const rows = overviewSpecs(this.snap.groups);
    const key = `${whole.id}:${width}:${rows.map((r) => r.id).join(",")}`;
    if (key === this.overviewKey) return;
    this.overviewKey = key;
    const frame = await this.decimator.frame({ tile: whole.id, v0: e[0], v1: e[1], width, rows });
    if (frame && this.overviewKey === key) this.set({ overview: frame });
  }

  private set(patch: Partial<TimelineSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    this.listeners.forEach((fn) => fn(this.snap));
  }
}

function loadConfig(source: Source): RowConfig {
  return parseConfig(load(`timeline.rows.${source}`, ""));
}
