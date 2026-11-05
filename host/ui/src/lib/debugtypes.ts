// デバッガの画面の状態の型（デーモンの応答の形のまま）。Debugger（debugger.ts）が持つ。
import type { BreakpointAction } from "./breakpoints";

export interface SourceFile {
  name: string;
  path: string;
  exists: boolean;
  // Linux のプロセス: この PC に無く、attach のときに対象から取り寄せた写し（debug.source.read が読む）
  fetched?: { target: string; path: string; local_path: string; bytes: number; sha256: string };
  fetch_error?: { target: string; path: string; error: string };
}

export interface Source {
  name: string;
  path: string;
  text: string;
  lines: number[];          // 命令のある行（ブレークポイントを置ける行）
}

export interface Place {
  pc?: number;
  where: string;
  function?: string;
  file?: string;
  line?: number;
  run?: number;             // デーモンがコアを動かし得る操作を数えた番号（同じ場所で止まり直しても変わる）
}

export interface Breakpoint extends Place {
  address: number;
  location: string;
  kind?: "hw" | "flash" | "sw";        // 接続中: 比較器 / Flash の BKPT / RAM の BKPT
  action?: BreakpointAction;           // 条件・ヒット カウント・ログ（lib/breakpoints.ts）
  tool?: string;                       // 道具（heap.trace 等）が置いたもの
  enabled?: boolean;                   // false = 無効にしたもの（debug.breakpoint.enable。機器には置いていない）
  at?: { file: string; path: string; line: number };   // "ファイル:行" で置いた場所（デーモンが解いた事実）
}

export interface Watchpoint {
  address: number;
  size: number;
  kind: string;
}

export interface Watch {
  name: string;               // C の式
  type?: string;
  value?: unknown;
  error?: string;
}

/** ウォッチの値の出どころ: 止まったときの値 / 走らせたまま読んだ値 / ELF だけで調べた（値なし） */
export type WatchMode = "stopped" | "live" | "elf";
export const LIVE_MS = 1000;            // ライブ ウォッチの周期

export interface Variable {
  name: string;
  type?: string;
  value: unknown;
  address?: number;
}

export interface FrameVariables {
  function?: string;
  arguments: Variable[];
  locals: Variable[];
}

export interface Frame {
  index?: number;
  pc: number;
  where: string;
  note: string;
  function?: string | null;
  file?: string | null;
  line?: number | null;
}

export interface Instruction {
  address: number;
  mnemonic: string;
  operands: string;
  function?: string | null;
  offset?: number;
  file?: string | null;
  line?: number | null;
  new_line?: boolean;
  bytes?: string;
  in_flash?: boolean | null;     // その番地が Flash の領域か（デーモンの事実。null = 分からない。書く道は lib/valuewrite）
}

export interface DebugSnapshot {
  elf: string | null;        // 読み込んだ ELF（デーモンの PC のパス）
  files: SourceFile[];
  open: string | null;
  source: Source | null;
  stop: Place | null;        // 止まっているときの場所（実行中は null）
  stale: boolean;            // 実行中: レジスタ・変数・呼び出し履歴は最後に止まったときの値
  breakpoints: Breakpoint[];
  watchpoints: Watchpoint[];
  registers: Record<string, number> | null;
  backtrace: Frame[];
  frame: number;             // 選んだフレーム（呼び出し履歴の index。0 = 止まった関数）。Locals・Watch・Source が従う
  watches: Watch[];
  watchMode: WatchMode;
  locals: FrameVariables | null;
  disasm: Instruction[];     // 止まった PC の前後（showAddress の後はその番地の前後）
  focus: number | null;      // showAddress で見せた番地（Disassembly が強調する。止まり直すと null）
  gen: number;               // 変数を読み直した回数（木の展開したポインタの先を読み直す合図）
  stops: number;             // 止まって読み直した回数（変わった値の印は、前に止まったときの値と比べる）
  lastStep: StepInfo | null; // 最後のステップ操作の結果（デーモンが返した値。走り出したら null）
  filesRead: boolean;        // つないでから、ソースの一覧を一度は読み終えた（読み終わる前に「プログラムが無い」と出さない。backlog 69 番）
}

/** ステップ操作の結果（debug.step の応答のまま）。steps・runs はデーモンが測った数（古いデーモンは無い = null）。
 *  踏み越えた呼び出しから同じ行に戻っても、これで進んだと分かる */
export interface StepInfo {
  mode: "over" | "into" | "out";
  steps: number | null;      // ホストが発行した 1 命令ステップの数
  runs: number | null;       // 踏み台・ブレークポイントまで走らせた回数
  elapsedS: number;
  where: string;
}
