// Code Profile のビューの計算と、ソースの余白に出す当たりの割合・カバレッジ（Source のパネルが読む）。
// Svelte・DOM に依らない（store の約束 subscribe だけを満たす）。
import type { DaemonConnection } from "./api";
import type { TimelineSnapshot } from "./timeline/state";

type Conn = Pick<DaemonConnection, "call">;

/** 関数（行）の割合の行（profile.result / profile.aggregate の rows） */
export interface ProfileRow {
  function: string;
  address?: number;
  file?: string | null;
  line?: number | null;
  samples: number;
  fraction: number;
}

/** 関数のカバレッジ（profile.coverage の functions の要素） */
export interface FunctionCoverage {
  function: string;
  lines: number;
  lines_hit: number;
  line_fraction: number;
  hit_lines: [string, number][];
  missed_lines: [string, number][];
}

/** 区間で集計する範囲: 時間軸の基準と選択のカーソルの間（記録の時間軸）。方法 pcsr の時刻はタイムベースなので
 *  そのまま t0 / t1 に渡せる。itm はコアのクロック数で合わないので null（理由は reason） */
export function selectedRange(s: Pick<TimelineSnapshot, "source" | "selection" | "reference">,
                              method: string | null): { t0: number; t1: number } | { reason: "itm" | "cursors" } {
  if (method === "itm") return { reason: "itm" };
  if (s.source !== "recording" || s.selection === null || s.reference === null || s.selection === s.reference) {
    return { reason: "cursors" };
  }
  return { t0: Math.min(s.selection, s.reference), t1: Math.max(s.selection, s.reference) };
}

export const baseName = (path: string | null | undefined): string => (path ?? "").split(/[\\/]/).pop() ?? "";

/** ソースの行の当たり（Source の余白）。fraction は全サンプルに対する割合、max は同じファイルでいちばん多い行 */
export interface LineHeat {
  fraction: number;
  samples: number;
  max: number;
}

export interface HeatSnapshot {
  lines: Map<string, Map<number, LineHeat>>;      // ファイル名（パスの最後）→ 行 → 当たり
  missed: Map<string, Set<number>>;               // サンプルの当たらなかった行（近似のカバレッジ）
}

/** 行の当たりとカバレッジ → ファイルごとの表 */
export function heatOf(lines: ProfileRow[], coverage: FunctionCoverage[]): HeatSnapshot {
  const out: HeatSnapshot = { lines: new Map(), missed: new Map() };
  for (const r of lines) {
    if (!r.file || !r.line) continue;
    const f = baseName(r.file);
    const m = out.lines.get(f) ?? new Map<number, LineHeat>();
    m.set(r.line, { fraction: r.fraction, samples: r.samples, max: 0 });
    out.lines.set(f, m);
  }
  for (const m of out.lines.values()) {
    const max = Math.max(...[...m.values()].map((h) => h.samples));
    for (const h of m.values()) h.max = max;
  }
  for (const c of coverage) {
    for (const [file, line] of c.missed_lines) {
      const f = baseName(file);
      if (out.lines.get(f)?.has(line)) continue;
      const s = out.missed.get(f) ?? new Set<number>();
      s.add(line);
      out.missed.set(f, s);
    }
  }
  return out;
}

/** ソースの余白の当たり（Code Profile が置き、Source が読む。空なら何も出さない） */
export class SourceHeat {
  private snap: HeatSnapshot = { lines: new Map(), missed: new Map() };
  private listeners = new Set<(s: HeatSnapshot) => void>();

  subscribe(fn: (s: HeatSnapshot) => void): () => void {
    this.listeners.add(fn);
    fn(this.snap);
    return () => this.listeners.delete(fn);
  }

  set(snap: HeatSnapshot): void {
    this.snap = snap;
    this.listeners.forEach((fn) => fn(snap));
  }

  clear(): void {
    this.set({ lines: new Map(), missed: new Map() });
  }
}

export const sourceHeat = new SourceHeat();

/** 関数名（"name+off" も）→ 番地（debug.symbol.info。覚えておく） */
export class SymbolAddresses {
  private known = new Map<string, number | null>();
  constructor(private conn: Conn) {}

  async of(name: string): Promise<number | null> {
    const key = name.replace(/\+\d+$/, "");
    if (!this.known.has(key)) {
      try {
        const r = await this.conn.call("debug.symbol.info", { location: key }, { quiet: true });
        this.known.set(key, typeof r.address === "number" ? r.address : null);
      } catch {
        this.known.set(key, null);
      }
    }
    return this.known.get(key) ?? null;
  }
}
