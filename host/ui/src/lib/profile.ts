// Code Profile のビューの計算と、ソースの余白に出す当たりの割合・カバレッジ（Source のパネルが読む）。
// Svelte・DOM に依らない（store の約束 subscribe だけを満たす）。
import type { ProfileFunctionRow, ProfileLineRow, ProfileRow } from "../../../api/generated/fprobe_api";
import type { DaemonConnection, Result } from "./api";
import type { TimelineSnapshot } from "./timeline/state";
import { addListener } from "./store";

type Conn = Pick<DaemonConnection, "call">;

// 割合の表の行（profile.result / profile.aggregate の rows。仕様から生成した型）: by = function の行・by = line の行
export type { ProfileFunctionRow, ProfileLineRow };

/** by = 関数・行・命令・例外の表（profile.aggregate の組の表でない形。行を頼んだ応答は必ずこちら） */
export function rowTable(r: Result<"profile.result"> | Result<"profile.aggregate">) {
  if (!("rows_total" in r)) throw new Error(`profile: by=${r.by} の組の表が返った（行の表を頼んだ）`);
  return r;
}

/** by = function の表の行（頼んだ by の行だけが返る。型を絞るだけ） */
export function functionRows(rows: ProfileRow[]): ProfileFunctionRow[] {
  return rows.filter((r): r is ProfileFunctionRow => "size" in r);
}

/** by = line の表の行（命令の行は text を持つ） */
export function lineRows(rows: ProfileRow[]): ProfileLineRow[] {
  return rows.filter((r): r is ProfileLineRow => "line" in r && !("text" in r));
}

/** 関数のカバレッジ（profile.coverage の functions の要素） */
export type FunctionCoverage = Result<"profile.coverage">["functions"][number];

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
export function heatOf(lines: ProfileLineRow[], coverage: FunctionCoverage[]): HeatSnapshot {
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
    return addListener(this.listeners, fn, this.snap);
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
