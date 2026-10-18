// 画面のデバッガの状態: ソース・ブレークポイント・止まった場所・レジスタ・呼び出し履歴・変数。
// Svelte に依らない（store の約束 subscribe だけを満たす）。ターゲットの状態は AppState（target.state の通知）から受ける。
// ウォッチは止まったら読み直し、走っている間はウォッチのパネルが開いていれば周期的に走らせたまま読み（ライブ ウォッチ。
// debug.evaluate の live = 大域・静的とメモリ）、つないでいなければ ELF だけで式と型を調べる（debug.expression.check）。
import { errorText, type DaemonConnection } from "./api";
import type { BreakpointAction } from "./breakpoints";
import { isRunning } from "./commands";
import { execution, stepParams } from "./execution";
import { load, save } from "./prefs";
import type { AppState, Snapshot } from "./state";
import type { UiState } from "./ui";

export interface SourceFile {
  name: string;
  path: string;
  exists: boolean;
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
  function?: string;
  file?: string;
  line?: number;
}

/** "func+0x4 (file.c:12)" 等の場所の文字列から file と line を拾う */
export function placeOf(where: string): { file: string; line: number } | null {
  const m = /([\w.\-/\\]+\.[A-Za-z]+):(\d+)/.exec(where);
  return m ? { file: m[1].split(/[\\/]/).pop()!, line: Number(m[2]) } : null;
}

/** フレームのソースの場所（デーモンが file・line を返せばそれ、無ければ where から） */
export function frameLine(f: Frame | undefined): { file: string; line: number } | null {
  if (!f) return null;
  if (f.file && f.line !== undefined) return { file: f.file.split(/[\\/]/).pop()!, line: f.line };
  return placeOf(f.where);
}

/** 呼び出し履歴のフレームに移る: そのフレームの変数・逆アセンブルを読み、Source のその行を選ぶ（Call Stack の
 *  クリック、ほかの窓・AI が ui.state で選んだとき） */
export async function focusFrame(dbg: Debugger, ui: UiState, index: number): Promise<void> {
  await dbg.selectFrame(index);
  const at = frameLine(dbg.snapshot.backtrace[index]);
  if (at) ui.select(at.file, at.line);
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
}

// ELF・ブレークポイントを変える操作。誰か（AI も）が行ったら読み直す
const ELF_METHODS = new Set(["debug.elf.load", "debug.elf.load_demo", "debug.elf.upload", "target.program_ram",
                             "target.flash"]);
// 止まっている間に値を変え得る操作。誰か（AI も）が行ったら変数を読み直す
const WRITE_METHODS = new Set(["debug.write_expression", "debug.variable.write", "target.memory.write",
                               "target.registers.write", "target.peripheral.write", "debug.call"]);
// 接続し直す・読み込み直すとデバッガが作り直され、ブレークポイントは空になる
const BREAK_METHODS = new Set(["debug.breakpoint.set", "debug.breakpoint.clear", "debug.watchpoint.set",
                               "debug.watchpoint.clear", "target.connect", "target.disconnect", "target.program_ram",
                               "target.flash"]);

/** 開いているファイルの、ブレークポイントの印を付ける行。置いたときの「ファイル:行」を優先する（同じ番地に複数の行が
 *  あると、番地から引き直した行は押した行とずれる） */
export function markedLines(d: DebugSnapshot): Set<number> {
  const out = new Set<number>();
  for (const b of d.breakpoints) {
    const [file, line] = b.location.split(":");
    if (d.open && file === d.open && /^\d+$/.test(line ?? "")) out.add(Number(line));
    else if (b.file === d.open && b.line !== undefined) out.add(b.line);
  }
  return out;
}

/** ブレークポイントのある命令の番地（逆アセンブルの印） */
export function markedAddresses(d: Pick<DebugSnapshot, "breakpoints">): Set<number> {
  return new Set(d.breakpoints.map((b) => b.address));
}

type Conn = Pick<DaemonConnection, "call" | "on" | "onLink">;

export class Debugger {
  private snap: DebugSnapshot = {
    elf: null, files: [], open: null, source: null, stop: null, stale: false, breakpoints: [], watchpoints: [],
    registers: null, backtrace: [], watches: savedWatches(), watchMode: "elf", locals: null, disasm: [], gen: 0, stops: 0,
    frame: 0, focus: null,
  };
  private liveUsers = 0;                     // ウォッチを走らせたまま読む利用者（開いているパネル）の数
  private liveTimer: ReturnType<typeof setInterval> | null = null;
  private liveMs = LIVE_MS;
  private liveBusy = false;
  private target: Record<string, any> | null = null;
  private members = new Map<string, Promise<Record<string, string>>>();   // 型の名前 → メンバーの型
  private listeners = new Set<(s: DebugSnapshot) => void>();
  private lastStop: string | undefined;     // 止まった pc と実行の番号（変わったら読み直す）
  private openSeq = 0;                       // 最後に頼んだファイル・止まった場所の読み（遅れて返った古い応答は捨てる）
  private stopSeq = 0;

  constructor(private conn: Conn, private app: AppState) {
    conn.onLink((link) => {
      if (link === "open") void this.reloadAll();
    });
    conn.on("activity", (e: { method: string }) => {
      if (ELF_METHODS.has(e.method)) void this.reloadFiles();
      if (BREAK_METHODS.has(e.method)) void this.reloadBreakpoints();
      if (WRITE_METHODS.has(e.method) && this.snap.stop) void this.refreshVariables();
      if (e.method === "target.registers.write" && this.snap.stop) void this.reloadRegisters();
    });
    app.subscribe((s: Snapshot) => this.onTarget(s.target));
  }

  /** ウォッチを走らせたまま読む（ウォッチのパネルが開いている間）。止めるときは返った関数を呼ぶ */
  startLive(ms = LIVE_MS): () => void {
    this.liveUsers++;
    this.liveMs = ms;
    this.syncLive();
    let stopped = false;
    return () => {
      if (stopped) return;
      stopped = true;
      this.liveUsers--;
      this.syncLive();
    };
  }

  /** 状態に合わせて周期の読みを始める・止める（走っていて利用者がいるときだけ）。止まっていない間はすぐ 1 回読む */
  private syncLive(): void {
    const want = this.liveUsers > 0 && isRunning(this.target?.state);
    if (want && !this.liveTimer) {
      this.liveTimer = setInterval(() => void this.refreshWatches(), this.liveMs);
    } else if (!want && this.liveTimer) {
      clearInterval(this.liveTimer);
      this.liveTimer = null;
    }
    if (this.liveUsers > 0 && !this.snap.stop) void this.refreshWatches();
  }

  /** 止まっていないときのウォッチ: 走っていれば走らせたまま読み、つないでいなければ ELF だけで調べる */
  private async refreshWatches(): Promise<void> {
    if (this.snap.stop || this.liveBusy || !this.snap.watches.length) return;
    this.liveBusy = true;
    try {
      const mode: WatchMode = isRunning(this.target?.state) ? "live" : "elf";
      const watches = await this.readWatches(this.snap.watches, mode);
      if (!this.snap.stop) this.set({ watches, watchMode: mode });
    } finally {
      this.liveBusy = false;
    }
  }

  subscribe(fn: (s: DebugSnapshot) => void): () => void {
    this.listeners.add(fn);
    fn(this.snap);
    return () => this.listeners.delete(fn);
  }

  get snapshot(): DebugSnapshot {
    return this.snap;
  }

  /** ELF を開く（デーモンの PC のパス）。開けたら true */
  async loadElf(path: string): Promise<boolean> {
    const done = await this.app.act("debug.elf.load", { path });
    if (done) await this.reloadFiles();
    return Boolean(done);
  }

  /** 同梱のデモの ELF を開く。開けたらそのパス */
  async loadDemo(): Promise<string | null> {
    const done = await this.app.act("debug.elf.load_demo");
    if (done) await this.reloadFiles();
    return done?.path ?? null;
  }

  /** 画面で選んだファイルの中身をデーモンへ送って開く。開けたらデーモンの PC のパス */
  async upload(name: string, data: ArrayBuffer): Promise<string | null> {
    const done = await this.app.act("debug.elf.upload", { name, data_base64: base64(data) });
    if (done) await this.reloadFiles();
    return done?.path ?? null;
  }

  /** 行単位のステップ（over / into / out。実行の設定の割り込みの扱いで）。止まった場所は target.state の通知で届く */
  async step(mode: "over" | "into" | "out"): Promise<void> {
    await this.app.act("debug.step", stepParams(mode, execution.get()));
  }

  /** 1 命令のステップ（逆アセンブルの命令ステップ） */
  async stepInstruction(): Promise<void> {
    await this.app.act("target.step", { count: 1 });
  }

  /** その行まで実行する（Run to Cursor） */
  async runTo(file: string, line: number): Promise<void> {
    await this.app.act("debug.step", stepParams("until", execution.get(), `${file}:${line}`));
  }

  /** 命令の番地のブレークポイントを切り替える（逆アセンブルの左の余白。行で置いたものも同じ番地なら外す） */
  async toggleAddress(address: number): Promise<void> {
    const existing = this.snap.breakpoints.find((b) => b.address === address);
    if (existing) await this.app.act("debug.breakpoint.clear", { location: existing.location });
    else await this.app.act("debug.breakpoint.set", { location: `0x${address.toString(16).toUpperCase().padStart(8, "0")}` });
    await this.reloadBreakpoints();
  }

  /** ファイルの行のブレークポイントを切り替える（開いていないファイルでも、置いた場所の「ファイル:行」で） */
  async toggleAt(file: string, line: number): Promise<void> {
    if (this.snap.source?.name === file) return this.toggleBreakpoint(line);
    const existing = this.snap.breakpoints.find((b) => b.location === `${file}:${line}`);
    if (existing) await this.app.act("debug.breakpoint.clear", { location: existing.location });
    else await this.app.act("debug.breakpoint.set", { location: `${file}:${line}` });
    await this.reloadBreakpoints();
  }

  async removeAllBreakpoints(): Promise<void> {
    await this.app.act("debug.breakpoint.clear", {});
    await this.reloadBreakpoints();
  }

  /** ELF を RAM に読み込み、ベクタ表から起動できる状態で止める（試験用のプログラム。Flash は書かない） */
  async programRam(path: string): Promise<void> {
    if (await this.app.act("target.program_ram", { path })) await this.reloadFiles();
  }

  markedLines(): Set<number> {
    return markedLines(this.snap);
  }

  async openFile(name: string): Promise<void> {
    const seq = ++this.openSeq;
    const file = this.snap.files.find((f) => f.name === name);
    if (!file?.exists) {
      this.set({ open: name, source: null });
      return;
    }
    if (this.snap.source?.name === name) {
      this.set({ open: name });
      return;
    }
    const source = await this.app.act("debug.source.read", { name });
    if (seq === this.openSeq) this.set({ open: name, source: source ?? null });
  }

  /** 開いているファイルの行のブレークポイントを切り替える（命令の無い行は何もしない） */
  async toggleBreakpoint(line: number): Promise<void> {
    const source = this.snap.source;
    if (!source || !source.lines.includes(line)) return;
    const existing = this.snap.breakpoints.find((b) => b.location === `${source.name}:${line}`)
      ?? this.snap.breakpoints.find((b) => b.file === source.name && b.line === line);
    if (existing) await this.app.act("debug.breakpoint.clear", { location: existing.location });
    else await this.app.act("debug.breakpoint.set", { location: `${source.name}:${line}` });
    await this.reloadBreakpoints();
  }

  async removeBreakpoint(location: string): Promise<void> {
    await this.app.act("debug.breakpoint.clear", { location });
    await this.reloadBreakpoints();
  }

  async setWatchpoint(name: string, kind = "write"): Promise<void> {
    await this.app.act("debug.watchpoint.set", { location: name, kind });
    await this.reloadBreakpoints();
  }

  async clearWatchpoint(address: number): Promise<void> {
    await this.app.act("debug.watchpoint.clear", { location: `0x${address.toString(16)}` });
    await this.reloadBreakpoints();
  }

  async addWatch(name: string): Promise<void> {
    if (!name || this.snap.watches.some((w) => w.name === name)) return;
    this.set({ watches: [...this.snap.watches, { name }] });
    saveWatches(this.snap.watches);
    if (this.snap.stop) this.set({ watches: await this.readWatches(this.snap.watches) });
    else if (this.liveUsers > 0) await this.refreshWatches();
  }

  removeWatch(name: string): void {
    this.set({ watches: this.snap.watches.filter((w) => w.name !== name) });
    saveWatches(this.snap.watches);
  }

  /** C の式を評価する（変数の木のポインタの先・多い配列の続き）。誤りは投げる */
  async evaluate(expression: string, maxElements?: number): Promise<{ type?: string; value: unknown }> {
    const where = this.snap.stop ? { frame: this.snap.frame } : { live: true };      // 走っていれば走らせたまま
    const params = { expression, ...where, ...(maxElements ? { max_elements: maxElements } : {}) };
    return this.conn.call("debug.evaluate", params, { quiet: true });
  }

  /** 構造体の型のメンバーの型（ELF だけを読む。型ごとに 1 回）。分からなければ空 */
  typeMembers(type: string): Promise<Record<string, string>> {
    let found = this.members.get(type);
    if (!found) {
      found = this.conn.call("debug.type_info", { name: type }, { quiet: true })
        .then((info: { members?: { name: string | null; type: string }[] }) =>
          Object.fromEntries((info.members ?? []).filter((m) => m.name).map((m) => [m.name!, m.type])))
        .catch(() => ({}));
      this.members.set(type, found);
    }
    return found;
  }

  /** 式の指す値に書き（Watch・Locals の値の編集）、変数を読み直す。書けたら true */
  async writeExpression(expression: string, value: number | string): Promise<boolean> {
    const done = await this.app.act("debug.write_expression", { expression, value, frame: this.snap.frame });
    if (done !== undefined) await this.refreshVariables();
    return done !== undefined;
  }

  /** コアレジスタに書く（Registers の値の編集。止まっているとき）。読み直しは書き込みの記録（activity）で。書けたら true */
  async writeRegister(name: string, value: number): Promise<boolean> {
    return (await this.app.act("target.registers.write", { name, value: value >>> 0 })) !== undefined;
  }

  private async reloadRegisters(): Promise<void> {
    const registers = await this.quiet("target.registers.read");
    if (registers) this.set({ registers });
  }

  /** ウォッチとローカル変数を読み直す（止まっているとき） */
  async refreshVariables(): Promise<void> {
    if (!this.snap.stop) return;
    const watches = await this.readWatches(this.snap.watches);
    const locals = await this.quiet("debug.frame_variables", { frame: this.snap.frame });
    this.set({ watches, locals: locals ?? null, gen: this.snap.gen + 1 });
  }

  /** 呼び出し履歴のフレームを選ぶ: Locals・Watch をそのフレームで読み直し、逆アセンブルをその番地の前後に、
   *  Source をその行へ（VS Code・Ozone と同じ）。止まった関数は 0 */
  async selectFrame(index: number): Promise<void> {
    const f = this.snap.backtrace[index];
    if (!f || !this.snap.stop) return;
    const seq = ++this.stopSeq;
    this.set({ frame: index });
    const at = frameLine(f);
    if (at && this.snap.files.some((x) => x.name === at.file)) await this.openFile(at.file);
    const watches = await this.readWatches(this.snap.watches);
    const locals = await this.quiet("debug.frame_variables", { frame: index });
    const around = await this.quiet("debug.disasm.around", { address: f.pc, before: 12, after: 24 });
    if (seq === this.stopSeq) {
      this.set({ watches, locals: locals ?? null, disasm: around?.instructions ?? [], focus: null, gen: this.snap.gen + 1 });
    }
  }

  /** 番地の場所を見せる（時間軸のダブルクリック・ビューの関数名から）: その番地の行を開き（ファイル:行を返す。
   *  分からなければ null）、Disassembly をその番地の前後に。読みは記録に残さない */
  async showAddress(address: number): Promise<{ file: string; line: number } | null> {
    const location = `0x${(address >>> 0).toString(16)}`;
    const info = await this.quiet("debug.symbol.info", { location });
    const at = info?.where ? placeOf(String(info.where)) : null;
    if (at && this.snap.files.some((f) => f.name === at.file)) await this.openFile(at.file);
    const around = await this.quiet("debug.disasm.around", { address, before: 12, after: 24 });
    if (around?.instructions) this.set({ disasm: around.instructions, focus: address });
    return at;
  }

  private onTarget(target: Record<string, any> | null): void {
    const was = this.target?.state;
    this.target = target;
    if (target?.state === "HALTED") {
      const place = target as Place;
      this.set({ stop: place, stale: false });
      const key = `${place.pc}/${place.run}`;
      if (key !== this.lastStop) {
        this.lastStop = key;
        void this.refreshStopped(place);
      }
    } else if (target) {
      this.lastStop = undefined;
      if (this.snap.stop !== null || !this.snap.stale) this.set({ stop: null, stale: this.snap.registers !== null });
    }
    if (target?.state !== was) this.syncLive();            // 走り出した・止まった・切れた: ウォッチの読み方を変える
  }

  /** 止まったとき: 止まったファイルを開き、レジスタ・呼び出し履歴・変数を読み直す（確認なしの自動の読み） */
  private async refreshStopped(place: Place): Promise<void> {
    const seq = ++this.stopSeq;
    if (place.file && this.snap.files.some((f) => f.name === place.file)) await this.openFile(place.file);
    const registers = await this.quiet("target.registers.read");
    const backtrace = await this.quiet("debug.backtrace");
    this.set({ frame: 0 });                                   // 止まり直したら止まった関数に戻す
    const watches = await this.readWatches(this.snap.watches);
    const locals = await this.quiet("debug.frame_variables", { frame: 0 });
    const around = await this.quiet("debug.disasm.around", { before: 12, after: 24 });
    if (seq === this.stopSeq) {
      this.set({ registers: registers ?? null, backtrace: backtrace ?? [], watches, watchMode: "stopped",
                 locals: locals ?? null, disasm: around?.instructions ?? [], focus: null, gen: this.snap.gen + 1,
                 stops: this.snap.stops + 1 });
    }
  }

  /** ウォッチを読む: stopped = 選んだフレームで、live = 走らせたまま（大域・静的）、elf = ELF だけで式と型を調べる
   *  （値は無い）。変数ごとの誤りはその行に出す */
  private async readWatches(watches: Watch[], mode: WatchMode = "stopped"): Promise<Watch[]> {
    const out: Watch[] = [];
    for (const w of watches) {
      try {
        if (mode === "elf") {
          const r = await this.conn.call("debug.expression.check", { expression: w.name }, { quiet: true });
          out.push({ name: w.name, type: r.type ?? undefined });
          continue;
        }
        const where = mode === "live" ? { live: true } : { frame: this.snap.frame };
        const r = await this.conn.call("debug.evaluate", { expression: w.name, ...where }, { quiet: true });
        out.push({ name: w.name, type: r.type, value: r.value });
      } catch (e) {
        out.push({ name: w.name, error: errorText(e) });
      }
    }
    return out;
  }

  private async reloadAll(): Promise<void> {
    await this.reloadFiles();
    await this.reloadBreakpoints();
  }

  private async reloadFiles(): Promise<void> {
    const list = await this.quiet("debug.source.list");
    if (!list) return;
    const status = await this.quiet("debug.elf.status");
    const files: SourceFile[] = list.files;
    const keep = files.some((f) => f.name === this.snap.open);
    // ELF が変わったら本文は読み直す
    this.members.clear();                                     // 型は ELF ごと
    this.set({ files, open: keep ? this.snap.open : null, source: null, elf: status?.path ?? (files.length ? this.snap.elf : null) });
    // 止まった知らせが一覧より先に来ていたら、止まったファイルを開く
    const stopped = this.snap.stop?.file;
    const name = keep ? this.snap.open : stopped && files.some((f) => f.name === stopped) ? stopped : null;
    if (name) await this.openFile(name);
  }

  private async reloadBreakpoints(): Promise<void> {
    const list = await this.quiet("debug.breakpoint.list");
    // ターゲット未接続ではブレークポイントは無い（デバッガが無い）
    this.set({ breakpoints: list?.breakpoints ?? [], watchpoints: list?.watchpoints ?? [] });
  }

  /** 自動の読み: 誤り（ターゲット未接続・実行中など）は画面の誤りに出さず、無かったことにする */
  private async quiet(method: Parameters<Conn["call"]>[0], params?: object): Promise<any> {
    try {
      return await this.conn.call(method, params as any, { quiet: true });
    } catch {
      return undefined;
    }
  }

  private set(patch: Partial<DebugSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    this.listeners.forEach((fn) => fn(this.snap));
  }
}

/** ファイルの中身 → base64（debug.elf.upload・target.flash.upload） */
export function base64(data: ArrayBuffer): string {
  const bytes = new Uint8Array(data);
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000) out += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(out);
}

/** ウォッチの式は画面に覚える（VS Code と同じく、開き直しても残る） */
function savedWatches(): Watch[] {
  try {
    const names = JSON.parse(load("watches", "[]"));
    return Array.isArray(names) ? names.filter((n) => typeof n === "string").map((name) => ({ name })) : [];
  } catch {
    return [];
  }
}

function saveWatches(watches: Watch[]): void {
  save("watches", JSON.stringify(watches.map((w) => w.name)));
}
