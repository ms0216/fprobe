// 端末（Terminal のパネル）の源と文字の扱い。Svelte に依らない。
// 源: RTT の上りのチャンネル（rtt0〜）・SWO の ITM のポート（swo0〜）・UART・セミホスティング。デーモンの
// terminal.sources / terminal.read で読む（位置 since から。ほかの読み手の位置は動かさない）。画面はデーモンと
// 一緒に配られるので、古いデーモン向けの経路（誤りの文の語で rtt.* に落とす・チャンネル 0 だけ書けるとみなす）は持たない。
// 行の時刻: terminal.read の times（受け取りごとの壁時計の秒と、記録中ならタイムベースの時刻 t）を画面の文字の位置の
// 印（Stamp）にし、行の始まりの時刻を出す。t のある行は時間軸のその時刻へ飛べる。
import type { DaemonConnection } from "./api";

type Call = DaemonConnection["call"];

export interface TerminalSource {
  id: string;                 // rtt0 / swo0 / uart / semihosting
  kind: "rtt" | "swo" | "uart" | "semihosting";
  name: string;
  writable: boolean;
  channel?: number;
  port?: number;
  baud?: number | null;       // uart のいまのボーレート
}

export interface SourceList {
  sources: TerminalSource[];
  rtt: boolean;               // RTT を始めている
  swo: boolean;               // SWO を受けている
}

/** 受け取りの時刻（at = 読んだ文字列の中の文字の位置） */
export interface ChunkTime {
  at: number;
  wall: number;               // 壁時計の秒（Unix 時刻）
  t: number | null;           // 記録中に受けたものはタイムベースの時刻
}

/** SEGGER の仮想端末の切り替え（at = data の中の文字の位置。そこから後は端末 id） */
export interface TerminalSwitch {
  at: number;
  id: number;
}

export interface Chunk {
  data: string;
  next: number;
  lost: number;
  times?: ChunkTime[];
  timebaseHz?: number | null;
  terminals?: TerminalSwitch[];   // RTT の源だけ（仮想端末の切り替えはデーモンが文字から除いて返す）
}

export async function loadSources(call: Call): Promise<SourceList> {
  const r = await call("terminal.sources", {}, { quiet: true });
  return { sources: r.sources, rtt: Boolean(r.rtt), swo: Boolean(r.swo) };
}

/** 源の位置 since からを読む（times なら受け取りの時刻も）。RTT は仮想端末の切り替え（terminals）も頼む */
export async function readSource(call: Call, source: TerminalSource, since: number, times = false): Promise<Chunk> {
  const terminals = source.kind === "rtt";
  const r = await call("terminal.read", { source: source.id, since, ...(times ? { times: true } : {}),
                                          ...(terminals ? { terminals: true } : {}) } as any, { quiet: true });
  return { data: r.data, next: r.next, lost: r.lost ?? 0,
           ...(times ? { times: r.times ?? [], timebaseHz: r.timebase_hz ?? null } : {}),
           ...(Array.isArray(r.terminals) ? { terminals: r.terminals } : {}) };
}

/** 送る操作（メソッドと引数）: RTT の下り・セミホスティングの標準入力は rtt.write、UART は uart.write */
export function writeCall(source: TerminalSource, text: string): { method: "rtt.write" | "uart.write"; params: any } {
  if (source.kind === "uart") return { method: "uart.write", params: { data: text } };
  return { method: "rtt.write", params: { channel: source.kind === "semihosting" ? "semihosting" : source.channel ?? 0, text } };
}

// --- 仮想端末（SEGGER RTT Viewer の Terminal 0〜F。ファームが SEGGER_RTT_SetTerminal・TerminalOut で切り替える） --------

/** 端末ごとの部分（times の at は部分の中の位置。部分の始めの文字を受けた時刻を at 0 に付け直す） */
export interface TerminalPart {
  terminal: number;
  data: string;
  times: ChunkTime[];
}

/** 読んだ文字を仮想端末の切り替えの位置で分ける。current は前の読みの終わりの端末（始めは 0）。終わりの端末を返す */
export function splitTerminals(chunk: Chunk, current: number): { parts: TerminalPart[]; current: number } {
  const times = [...(chunk.times ?? [])].sort((a, b) => a.at - b.at);
  const cuts = [...(chunk.terminals ?? [])].sort((a, b) => a.at - b.at);
  const parts: TerminalPart[] = [];
  let start = 0;
  let terminal = current;
  const close = (end: number) => {
    if (end <= start) return;
    // 部分の始めより前（か同じ位置）の最後の受け取り → at 0、部分の中の受け取りはずらす
    const before = times.filter((m) => m.at <= start).at(-1);
    const inside = times.filter((m) => m.at > start && m.at < end).map((m) => ({ ...m, at: m.at - start }));
    parts.push({ terminal, data: chunk.data.slice(start, end),
                 times: [...(before ? [{ ...before, at: 0 }] : []), ...inside] });
  };
  for (const cut of cuts) {
    const at = Math.max(start, Math.min(chunk.data.length, cut.at));
    close(at);
    start = at;
    terminal = cut.id;
  }
  close(chunk.data.length);
  return { parts, current: terminal };
}

/** 端末のタブ: 切り替えが来ていなければ無し。来ていれば「すべて」（null）と使われた端末の番号順 */
export function terminalTabs(used: ReadonlySet<number>): (number | null)[] {
  return used.size ? [null, ...[...used].sort((a, b) => a - b)] : [];
}

/** 端末の画面のキー（源の id と端末の番号。null = すべて） */
export const screenKey = (source: string, terminal: number | null): string =>
  terminal === null ? source : `${source}/t${terminal}`;

export const LINE_ENDINGS ={ lf: "\n", cr: "\r", crlf: "\r\n", none: "" } as const;
export type LineEnding = keyof typeof LINE_ENDINGS;

/** UART のボーレートの選択肢（端末ソフトの標準） */
export const BAUDS = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1000000, 2000000];
/** SWO の速さの選択肢（TPIU の分周で作れる代表値） */
export const SWO_BAUDS = [500000, 1000000, 2000000, 4000000, 6000000, 8000000];

/** swo.configure の引数: ITM のポート（0〜31 の整数を , か空白で区切る）と SWO の速さは使う人が入れたものだけ（暗黙の
 *  ポート 0〜3・速さを使わない）。コア クロックは空なら null（デーモンが要れば誤りで返す）。足りない・誤りは null */
export function swoConfigureParams(portsText: string, baudText: string, coreClockText: string):
  { ports: number[]; swo_baud: number; core_clock_hz: number | null } | null {
  const parts = portsText.split(/[\s,]+/).filter(Boolean);
  if (!parts.length || !parts.every((p) => /^\d+$/.test(p) && Number(p) <= 31)) return null;
  const baud = Number(baudText);
  if (!baudText.trim() || !(baud > 0)) return null;
  const clock = Number(coreClockText);
  return { ports: parts.map(Number), swo_baud: baud, core_clock_hz: coreClockText.trim() && clock > 0 ? clock : null };
}

// --- 制御コード（VT100 の一部: 色・太字・画面の消去） ----------------------------------------------------

export interface Span {
  text: string;
  fg: number | null;          // 0〜15（30〜37 = 0〜7、90〜97 = 8〜15）
  bold: boolean;
}

/** 画面の文字の位置 at から後の文字を受けた時刻 */
export interface Stamp {
  at: number;
  wall: number;
  t: number | null;
}

export interface Screen {
  spans: Span[];
  fg: number | null;
  bold: boolean;
  pending: string;            // 途中で切れたエスケープの並び（次の文字と合わせる）
  stamps: Stamp[];            // at の昇順
  length: number;             // 画面の文字の数
  discarded: number;          // 上限（limit）を超えて画面から捨てた文字の数（黙って捨てない）
}

/** 画面に持つ文字の上限（古い文字から捨てる。捨てた数は Screen.discarded） */
export const TERMINAL_KEEP_CHARS = 200_000;

export const emptyScreen = (): Screen => ({ spans: [], fg: null, bold: false, pending: "", stamps: [], length: 0, discarded: 0 });

const ESC = "\x1b";
// CSI（ESC [ 引数 文字）・OSC（ESC ] … BEL / ESC \）・2 文字のエスケープ
const SEQUENCE = /\x1b\[([0-9;?]*)([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]/g;

type Work = Omit<Screen, "pending"> & { pending: string };

/** 受けた文字を画面に足す（色を spans に、CR LF を LF に、ESC[2J / ESC c で消す、ほかの制御は捨てる）。
 *  times（受け取りの時刻。at は input の中の位置）があれば、その位置の画面の文字に時刻の印を付ける。
 *  limit を超えたら古い文字から捨てる */
export function feed(screen: Screen, input: string, limit = TERMINAL_KEEP_CHARS, times: ChunkTime[] = []): Screen {
  const w: Work = { ...screen, spans: screen.spans.slice(), stamps: screen.stamps.slice() };
  let pos = 0;
  for (const m of [...times].sort((a, b) => a.at - b.at)) {
    const at = Math.max(pos, Math.min(input.length, m.at));
    feedText(w, input.slice(pos, at));
    pos = at;
    const last = w.stamps[w.stamps.length - 1];
    const stamp = { at: w.length, wall: m.wall, t: m.t };
    if (last && last.at === w.length) w.stamps[w.stamps.length - 1] = stamp;   // 文字の増えない受け取りは置き換え
    else w.stamps.push(stamp);
  }
  feedText(w, input.slice(pos));
  return trim(w, limit);
}

function feedText(w: Work, input: string): void {
  let text = w.pending + input;
  w.pending = "";
  const cut = text.lastIndexOf(ESC);
  if (cut >= 0 && !/^\x1b(\[[0-9;?]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[@-Z\\-_])/.test(text.slice(cut))) {
    w.pending = text.slice(cut);                      // 続きが来ていないエスケープ
    text = text.slice(0, cut);
  }
  const push = (s: string) => {
    s = s.replace(/\r\n/g, "\n").replace(/\r/g, "").replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, "");
    if (!s) return;
    const last = w.spans[w.spans.length - 1];
    if (last && last.fg === w.fg && last.bold === w.bold) w.spans[w.spans.length - 1] = { ...last, text: last.text + s };
    else w.spans.push({ text: s, fg: w.fg, bold: w.bold });
    w.length += s.length;
  };
  let at = 0;
  for (const m of text.matchAll(SEQUENCE)) {
    push(text.slice(at, m.index));
    at = m.index! + m[0].length;
    if (m[2] === "m") {
      for (const p of (m[1] || "0").split(";").map(Number)) {
        if (p === 0) [w.fg, w.bold] = [null, false];
        else if (p === 1) w.bold = true;
        else if (p === 22) w.bold = false;
        else if (p >= 30 && p <= 37) w.fg = p - 30;
        else if (p >= 90 && p <= 97) w.fg = p - 90 + 8;
        else if (p === 39) w.fg = null;
      }
    } else if ((m[2] === "J" && m[1] === "2") || m[0] === `${ESC}c`) {
      // 画面を消す: 文字も印も消す（いま受けている分の時刻は、消した後の始めに残す）
      const last = w.stamps[w.stamps.length - 1];
      w.spans = [];
      w.length = 0;
      w.stamps = last ? [{ ...last, at: 0 }] : [];
    }
  }
  push(text.slice(at));
}

function trim(w: Work, limit: number): Screen {
  let drop = w.length - limit;
  if (drop <= 0) return w;
  const dropped = drop;
  let i = 0;
  let spans = w.spans;
  while (drop > 0 && i < spans.length) {
    const n = spans[i].text.length;
    if (n <= drop) {
      drop -= n;
      i++;
    } else {
      spans = [...spans.slice(0, i), { ...spans[i], text: spans[i].text.slice(drop) }, ...spans.slice(i + 1)];
      drop = 0;
    }
  }
  // 印をずらす（残った最初の文字を受けた時刻の印は 0 に詰めて残す）
  const moved = w.stamps.map((s) => ({ ...s, at: s.at - dropped }));
  let k = -1;                                         // 新しい始めより前（か同じ）に始まった最後の受け取り
  while (k + 1 < moved.length && moved[k + 1].at <= 0) k++;
  const stamps = k >= 0 ? [{ ...moved[k], at: 0 }, ...moved.slice(k + 1)] : moved;
  return { ...w, spans: i ? spans.slice(i) : spans, length: w.length - dropped, stamps, discarded: w.discarded + dropped };
}

/** 画面の文字だけ（ログの保存） */
export function plain(screen: Screen): string {
  return screen.spans.map((s) => s.text).join("");
}

/** 画面の 1 行（改行で分けた。改行の文字は含めない） */
export interface Line {
  start: number;              // 行の始まりの文字の位置
  spans: Span[];
}

/** 画面を行に分ける（終わりの改行の後の空の行は、まだ何も来ていなければ含めない） */
export function lines(screen: Screen): Line[] {
  const out: Line[] = [];
  let current: Line = { start: 0, spans: [] };
  let pos = 0;
  for (const s of screen.spans) {
    const parts = s.text.split("\n");
    parts.forEach((part, k) => {
      if (k > 0) {
        out.push(current);
        current = { start: pos, spans: [] };
      }
      if (part) current.spans.push({ ...s, text: part });
      pos += part.length + (k < parts.length - 1 ? 1 : 0);
    });
  }
  if (current.spans.length || out.length === 0) out.push(current);
  return out;
}

/** 文字の位置 at の文字を受けた時刻（at 以前の最後の印。無ければ null） */
export function stampAt(screen: Screen, at: number): Stamp | null {
  let lo = 0, hi = screen.stamps.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (screen.stamps[mid].at <= at) lo = mid + 1;
    else hi = mid;
  }
  return lo > 0 ? screen.stamps[lo - 1] : null;
}

/** 時間軸の時刻 t（タイムベース）に当たる行: t 以前に受けたいちばん後の行（無ければ -1） */
export function lineAtTime(screen: Screen, rows: Line[], t: number): number {
  let best = -1;
  for (let i = 0; i < rows.length; i++) {
    const s = stampAt(screen, rows[i].start);
    if (s?.t === null || s?.t === undefined) continue;
    if (s.t <= t) best = i;
    else break;
  }
  return best;
}

/** 壁時計の秒 → 時刻の表示（hh:mm:ss.mmm、手元の時刻帯） */
export function clockTime(wall: number): string {
  const d = new Date(wall * 1000);
  const two = (n: number) => String(n).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}
