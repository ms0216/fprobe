// 時刻からコードの位置へ（画面の作り直し §4）: 時間軸の選択カーソル（記録の時刻）が動いたら recording.code_at を呼び、
// その応答をソース・呼び出し履歴・ローカル変数・スレッドの窓に配る。画面は応答を描くだけ（行を推定しない・判定しない）。
// 時刻の連動は既定で有効、窓ごとに外せる（その人のブラウザに覚える）。Svelte に依らない。
// 仕様: docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §3.3・§4
import { errorText, type DaemonConnection } from "./api";
import { EN, type Key, type Translate } from "./i18n";
import { load, save } from "./prefs";
import type { TimelineSnapshot } from "./timeline/state";

/** PC のサンプル（DWT_PCSR の読み 1 つ）。引けない欄は null */
export interface CodeAtSample {
  t: number;
  dt_s: number;                         // t（問い合わせの時刻）との差（秒。直前は負）
  value: number;                        // 読んだ値
  pc: number | null;                    // 0xFFFFFFFF・読めなかった語は null（reason）
  reason?: "pcsr_invalid" | "read_failed";
  ack?: number;
  function: string | null;
  offset: number | null;
  file: string | null;
  line: number | null;
  where: string | null;
}

/** 記録に残るターゲットの状態の出来事（本文は target.status のまま） */
export interface CodeAtTarget {
  t: number;
  dt_s: number;
  uncertainty_s: number | null;
  state?: string;
  pc?: number;
  where?: string;
  function?: string;
  file?: string;
  line?: number;
  halt_reason?: string;
  [key: string]: unknown;
}

/** t を含む RTOS の実行の区間 */
export interface CodeAtTask {
  t0: number;
  t1: number;
  handle: number;
  task?: string;
  idle?: boolean;
  method?: string;
  resolution?: number;
  [key: string]: unknown;
}

export interface CodeAtMissing {
  reason: string;
  t0?: number;
  t1?: number;
  detail?: string | null;
}

/** recording.code_at の応答 */
export interface CodeAt {
  t: number;
  timebase_hz: number;
  elf: string | null;
  recorded_elf: string | null;
  pc_before: CodeAtSample | null;
  pc_after: CodeAtSample | null;
  pc_gap_s: number | null;
  state: CodeAtTarget | null;
  halt_before: CodeAtTarget | null;
  halt_after: CodeAtTarget | null;
  task: CodeAtTask | null;
  missing: Record<string, CodeAtMissing>;
}

// --- 応答 → 描く値（計算はしない。どの欄をどこに出すかだけ） ---------------------------------------------

export type LineMark = "before" | "after" | "halt";

/** 記録の上で t のとき止まっていた（t 以前に最後に記録された状態が HALTED）なら、その状態の出来事 */
export function haltedAt(r: CodeAt | null): CodeAtTarget | null {
  return r?.state?.state === "HALTED" ? r.state : null;
}

/** ソースの窓の行の印: そのファイルの行ごとに、直前・直後のサンプルと（t で止まっていれば）止まった場所 */
export function lineMarks(r: CodeAt | null, file: string | null): Map<number, LineMark[]> {
  const out = new Map<number, LineMark[]>();
  if (!r || !file) return out;
  const add = (f: string | null | undefined, line: number | null | undefined, mark: LineMark) => {
    if (f !== file || line == null) return;
    out.set(line, [...(out.get(line) ?? []), mark]);
  };
  add(r.pc_before?.file, r.pc_before?.line, "before");
  add(r.pc_after?.file, r.pc_after?.line, "after");
  const halt = haltedAt(r);
  add(halt?.file, halt?.line, "halt");
  return out;
}

/** 材料が無いものの理由（表示の順。i18n の codeAt.missing.<欄>.<理由> の鍵の材料） */
export function missingList(r: CodeAt | null): { key: string; reason: CodeAtMissing }[] {
  if (!r) return [];
  return ["pc", "elf", "state", "task"].filter((k) => r.missing[k]).map((k) => ({ key: k, reason: r.missing[k] }));
}

/** 欠けの文。表に無い理由（新しいデーモン）は符号のまま */
export function missingText(tr: Translate, key: string, m: CodeAtMissing): string {
  const k = `codeAt.missing.${key}.${m.reason}`;
  return k in EN ? tr(k as Key, { detail: m.detail ?? "" }) : `${key}: ${m.reason}`;
}

/** サンプルの場所の文（引けた場所 where、無ければ PC、PC が無ければ理由） */
export function sampleText(tr: Translate, s: CodeAtSample): string {
  if (s.where) return s.where;
  if (s.pc !== null) return tr("codeAt.pc", { pc: `0x${(s.pc >>> 0).toString(16).padStart(8, "0")}` });
  const k = `codeAt.sample.${s.reason}`;
  return k in EN ? tr(k as Key, { ack: s.ack ?? "" }) : String(s.reason);
}

/** 符号つきの時間の差の文字（+1.5 µs / −3 µs） */
export function signedDuration(seconds: number, format: (s: number) => string): string {
  return seconds < 0 ? `−${format(-seconds)}` : `+${format(seconds)}`;
}

// --- 連動（窓ごとの on / off と、問い合わせ） ----------------------------------------------------------

export const LINKED_PANELS = ["source", "callStack", "locals", "threads"] as const;
export type LinkedPanel = (typeof LINKED_PANELS)[number];

const PREF = (panel: LinkedPanel) => `codeAt.link.${panel}`;

/** 窓が時刻に連動するか（既定は連動する） */
export function isLinked(panel: LinkedPanel): boolean {
  return load(PREF(panel), "1") === "1";
}

export function setLinked(panel: LinkedPanel, on: boolean): void {
  save(PREF(panel), on ? "1" : "0");
}

export interface CodeAtSnapshot {
  /** 問い合わせた時刻（記録の選択カーソル。記録でない・カーソルが無ければ null） */
  t: number | null;
  result: CodeAt | null;
  error: string | null;
  loading: boolean;
}

type Conn = Pick<DaemonConnection, "call">;
type Timeline = { subscribe(fn: (s: TimelineSnapshot) => void): () => void };

/** 問い合わせの鍵: 記録を見ていて選択カーソルがあるときだけ（捕捉の時刻はサンプル番号なので問い合わせない） */
export function cursorKey(s: Pick<TimelineSnapshot, "source" | "recording" | "selection">): string | null {
  return s.source === "recording" && s.selection !== null ? `${s.recording ?? ""}@${s.selection}` : null;
}

/** 時間軸の選択カーソルに合わせて recording.code_at を呼ぶ。使う窓（use）が 1 つも無ければ呼ばない。
 *  問い合わせは同時に 1 つ、動き続けたら終わってから最後の位置で 1 回だけ */
export class CodeAtLink {
  private snap: CodeAtSnapshot = { t: null, result: null, error: null, loading: false };
  private listeners = new Set<(s: CodeAtSnapshot) => void>();
  private users = 0;
  private key: string | null = null;
  private at: number | null = null;
  private asked: string | null = null;      // 最後に問い合わせた（問い合わせ中の）鍵
  private busy = false;
  private stop: () => void;

  constructor(private conn: Conn, timeline: Timeline) {
    this.stop = timeline.subscribe((s) => {
      const key = cursorKey(s);
      if (key === this.key) return;
      this.key = key;
      this.at = key === null ? null : s.selection;
      this.kick();
    });
  }

  subscribe(fn: (s: CodeAtSnapshot) => void): () => void {
    this.listeners.add(fn);
    fn(this.snap);
    return () => this.listeners.delete(fn);
  }

  /** 窓が連動を使い始める（やめる関数を返す）。使う窓が現れたら、今のカーソルで問い合わせる */
  use(): () => void {
    this.users++;
    this.kick();
    let done = false;
    return () => {
      if (done) return;
      done = true;
      this.users--;
    };
  }

  dispose(): void {
    this.stop();
    this.listeners.clear();
  }

  private set(patch: Partial<CodeAtSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    for (const fn of this.listeners) fn(this.snap);
  }

  private kick(): void {
    if (this.busy || this.users === 0 || this.key === this.asked) return;
    void this.run();
  }

  private async run(): Promise<void> {
    this.busy = true;
    try {
      while (this.users > 0 && this.key !== this.asked) {
        const key = this.key, at = this.at;
        this.asked = key;
        if (key === null || at === null) {
          this.set({ t: null, result: null, error: null, loading: false });
          continue;
        }
        this.set({ t: at, loading: true });
        let result: CodeAt | null = null, error: string | null = null;
        try {
          result = (await this.conn.call("recording.code_at", { t: at }, { quiet: true })) as CodeAt;
        } catch (e) {
          error = errorText(e);
        }
        if (this.key === key) this.set({ t: at, result, error, loading: false });
      }
    } finally {
      this.busy = false;
    }
  }
}
