// デバッガの状態から引く・畳む純関数（ソースの場所・ブレークポイントの印・ウォッチの式の保存）。Debugger（debugger.ts）が使う。
import { load, save } from "./prefs";
import type { Breakpoint, DebugSnapshot, Frame, Watch } from "./debugtypes";

/** デーモンの欄 file・line → ソースの場所（file は行表のファイルの名前のまま）。欄が無ければ null（where の文字は解釈しない） */
export function placeOf(p: { file?: string | null; line?: number | null } | null | undefined)
  : { file: string; line: number } | null {
  return p?.file && typeof p.line === "number" ? { file: p.file, line: p.line } : null;
}

/** フレームのソースの場所（デーモンが返す file・line） */
export function frameLine(f: Frame | undefined): { file: string; line: number } | null {
  return placeOf(f);
}

/** 開いているファイルの、ブレークポイントの印を付ける行。置いた「ファイル:行」（デーモンが解いた at）を優先する（同じ番地に
 *  複数の行があると、番地から引き直した行は押した行とずれる）。場所の文字は画面で解釈しない */
export function markedLines(d: DebugSnapshot, only?: (b: Breakpoint) => boolean): Set<number> {
  const out = new Set<number>();
  for (const b of only ? d.breakpoints.filter(only) : d.breakpoints) {
    if (b.at) {
      if (b.at.file === d.open) out.add(b.at.line);
    } else if (b.file === d.open && b.line !== undefined) out.add(b.line);
  }
  return out;
}

/** 行に置いたブレークポイント（置いた場所の at が先、無ければ番地から引いた行） */
export function breakpointOnLine(bps: Breakpoint[], file: string, line: number): Breakpoint | undefined {
  return bps.find((b) => b.at?.file === file && b.at.line === line)
    ?? bps.find((b) => !b.at && b.file === file && b.line === line);
}

/** ブレークポイントのある命令の番地（逆アセンブルの印） */
export function markedAddresses(d: Pick<DebugSnapshot, "breakpoints">): Set<number> {
  return new Set(d.breakpoints.map((b) => b.address));
}

/** ファイルの中身 → base64（debug.elf.upload・target.flash.upload） */
export function base64(data: ArrayBuffer): string {
  const bytes = new Uint8Array(data);
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000) out += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(out);
}

/** ウォッチの式は画面に覚える（VS Code と同じく、開き直しても残る） */
export function savedWatches(): Watch[] {
  try {
    const names = JSON.parse(load("watches", "[]"));
    return Array.isArray(names) ? names.filter((n) => typeof n === "string").map((name) => ({ name })) : [];
  } catch {
    return [];
  }
}

export function saveWatches(watches: Watch[]): void {
  save("watches", JSON.stringify(watches.map((w) => w.name)));
}
