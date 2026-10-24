// 端末: 源の一覧・読み・送る操作・制御コード
import { describe, expect, it } from "vitest";
import { RpcError } from "../src/lib/api";
import {
  emptyScreen, feed, lineAtTime, lines, loadSources, plain, readSource, splitTerminals, stampAt, terminalTabs, writeCall,
  type TerminalSource,
} from "../src/lib/terminal";
import { FakeConn } from "./fake-conn";


describe("端末の源", () => {
  it("terminal.sources・terminal.read で読む", async () => {
    const c = new FakeConn({
      "terminal.sources": { sources: [{ id: "uart", kind: "uart", name: "UART", writable: true }], rtt: false },
      "terminal.read": ({ source, since }: any) => ({ source, data: "ok", start: since, next: since + 2, lost: 0 }),
    });
    const call = c.call.bind(c) as any;
    const list = await loadSources(call);
    expect(list).toEqual({ sources: [{ id: "uart", kind: "uart", name: "UART", writable: true }], rtt: false, swo: false });
    expect(await readSource(call, list.sources[0], 5)).toEqual({ data: "ok", next: 7, lost: 0 });
    c.responders["terminal.read"] = ({ source, since }: any) => ({ source, data: "ok", start: since, next: since + 2, lost: 0,
                                                                   times: [{ at: 0, wall: 1, t: 50 }], timebase_hz: 1e6 });
    expect(await readSource(call, list.sources[0], 5, true))
      .toEqual({ data: "ok", next: 7, lost: 0, times: [{ at: 0, wall: 1, t: 50 }], timebaseHz: 1e6 });
    expect(c.calls.at(-1)).toEqual(["terminal.read", { source: "uart", since: 5, times: true }]);
  });

  it("誤りの文の語（「は無い」・not found・terminals）で別の経路に落とさない: 誤りはそのまま返す", async () => {
    const c = new FakeConn({
      "terminal.sources": () => { throw new RpcError(-32601, "terminal.sources は無い"); },
      "terminal.read": () => { throw new RpcError(-32020, "terminals: not found"); },
      "rtt.status": { running: true },
    });
    const call = c.call.bind(c) as any;
    await expect(loadSources(call)).rejects.toThrow("terminal.sources は無い");
    const rtt: TerminalSource = { id: "rtt0", kind: "rtt", name: "Terminal", writable: true, channel: 0 };
    await expect(readSource(call, rtt, 0)).rejects.toThrow("terminals: not found");
    expect(c.calls.map(([m]) => m)).toEqual(["terminal.sources", "terminal.read"]);   // rtt.* に落とさない・頼み直さない
  });

  it("RTT は仮想端末の切り替えも頼む", async () => {
    const rtt: TerminalSource = { id: "rtt0", kind: "rtt", name: "Terminal", writable: true, channel: 0 };
    const c = new FakeConn({
      "terminal.read": ({ source, since }: any) => ({ source, data: "ab", start: since, next: since + 2, lost: 0,
                                                     terminals: [{ at: 1, id: 2 }] }),
    });
    const call = c.call.bind(c) as any;
    expect(await readSource(call, rtt, 0)).toEqual({ data: "ab", next: 2, lost: 0, terminals: [{ at: 1, id: 2 }] });
    expect(c.calls.at(-1)).toEqual(["terminal.read", { source: "rtt0", since: 0, terminals: true }]);
  });

  it("送るのは RTT の下り・セミホスティングの標準入力（rtt.write）、UART（uart.write）", () => {
    const src = (kind: TerminalSource["kind"], channel?: number): TerminalSource => ({ id: kind, kind, name: kind, writable: true, channel });
    expect(writeCall(src("rtt", 1), "a\n")).toEqual({ method: "rtt.write", params: { channel: 1, text: "a\n" } });
    expect(writeCall(src("semihosting"), "b")).toEqual({ method: "rtt.write", params: { channel: "semihosting", text: "b" } });
    expect(writeCall(src("uart"), "c\r")).toEqual({ method: "uart.write", params: { data: "c\r" } });
  });
});

describe("仮想端末（SEGGER_RTT_SetTerminal）", () => {
  it("切り替えの位置で端末ごとに分け、各部分に受け取りの時刻を付け直す。前の読みの端末から続ける", () => {
    const r = splitTerminals({ data: "aaBBc", next: 9, lost: 0, times: [{ at: 0, wall: 1, t: null }, { at: 3, wall: 2, t: 7 }],
                               terminals: [{ at: 2, id: 1 }, { at: 4, id: 0 }] }, 0);
    expect(r.current).toBe(0);
    expect(r.parts).toEqual([
      { terminal: 0, data: "aa", times: [{ at: 0, wall: 1, t: null }] },
      { terminal: 1, data: "BB", times: [{ at: 0, wall: 1, t: null }, { at: 1, wall: 2, t: 7 }] },
      { terminal: 0, data: "c", times: [{ at: 0, wall: 2, t: 7 }] },
    ]);
    const next = splitTerminals({ data: "x", next: 10, lost: 0, terminals: [] }, 3);
    expect(next).toEqual({ parts: [{ terminal: 3, data: "x", times: [] }], current: 3 });
    expect(splitTerminals({ data: "", next: 10, lost: 0, terminals: [{ at: 0, id: 5 }] }, 3))
      .toEqual({ parts: [], current: 5 });                                  // 文字の無い切り替えも覚える
  });

  it("タブ: 切り替えが来た源は「すべて」（null）と使われた端末の番号順", () => {
    expect(terminalTabs(new Set())).toEqual([]);
    expect(terminalTabs(new Set([2, 0]))).toEqual([null, 0, 2]);
  });
});

describe("制御コード", () => {
  it("色（SGR）を spans に、CR LF を LF に、ほかの制御は捨てる", () => {
    const s = feed(emptyScreen(), "\x1b[32mI\x1b[0m ok\r\n\x1b[1;91mERR\x1b[0m\x07\n");
    expect(s.spans).toEqual([{ text: "I", fg: 2, bold: false }, { text: " ok\n", fg: null, bold: false },
                             { text: "ERR", fg: 9, bold: true }, { text: "\n", fg: null, bold: false }]);
  });

  it("途中で切れたエスケープは次の文字と合わせる。ESC[2J で消す。上限を超えたら古い方を捨てる", () => {
    let s = feed(emptyScreen(), "a\x1b[3");
    expect(plain(s)).toBe("a");
    s = feed(s, "1mb");
    expect(s.spans.at(-1)).toEqual({ text: "b", fg: 1, bold: false });
    expect(plain(feed(s, "\x1b[2Jnew"))).toBe("new");
    expect(plain(feed(emptyScreen(), "0123456789", 4))).toBe("6789");
    expect(feed(feed(emptyScreen(), "0123456789", 4), "ab", 4).discarded).toBe(8);   // 捨てた文字の数（黙って捨てない）
    expect(emptyScreen().discarded).toBe(0);
  });
});

describe("行の時刻", () => {
  it("受け取りの時刻を画面の文字の位置の印にし、行の始まりの時刻を引く（制御コードは数えない）", () => {
    let s = feed(emptyScreen(), "\x1b[32mboot\x1b[0m\nline", 1000, [{ at: 0, wall: 10, t: 100 }]);
    s = feed(s, " 2\nthird\n", 1000, [{ at: 0, wall: 11, t: 200 }, { at: 3, wall: 12, t: null }]);
    expect(plain(s)).toBe("boot\nline 2\nthird\n");
    expect(s.stamps).toEqual([{ at: 0, wall: 10, t: 100 }, { at: 9, wall: 11, t: 200 }, { at: 12, wall: 12, t: null }]);
    const rows = lines(s);
    expect(rows.map((r) => r.start)).toEqual([0, 5, 12]);
    expect(rows.map((r) => stampAt(s, r.start)?.wall)).toEqual([10, 10, 12]);   // 行の始まりを受けた時刻
    expect(lineAtTime(s, rows, 150)).toBe(1);
    expect(lineAtTime(s, rows, 50)).toBe(-1);
  });

  it("古い文字を捨てても、残った最初の文字の時刻は残る。画面を消したら印も消す", () => {
    let s = feed(emptyScreen(), "aaaa", 1000, [{ at: 0, wall: 1, t: null }]);
    s = feed(s, "bbbb", 6, [{ at: 0, wall: 2, t: null }]);
    expect(plain(s)).toBe("aabbbb");
    expect(s.stamps).toEqual([{ at: 0, wall: 1, t: null }, { at: 2, wall: 2, t: null }]);
    s = feed(s, "\x1b[2Jnew", 1000, [{ at: 0, wall: 3, t: null }]);
    expect(plain(s)).toBe("new");
    expect(s.stamps).toEqual([{ at: 0, wall: 3, t: null }]);
  });

  it("行に分ける: 空の行は残し、終わりの改行の後は含めない", () => {
    const s = feed(emptyScreen(), "a\n\nb\x1b[31mc\x1b[0m\n");
    expect(lines(s).map((r) => r.spans.map((x) => x.text).join(""))).toEqual(["a", "", "bc"]);
    expect(lines(emptyScreen())).toEqual([{ start: 0, spans: [] }]);
  });
});


describe("SWO の設定の引数（swo.configure）", () => {
  it("ITM のポート・速さは使う人が入れたものだけ（0〜3・2 MHz を暗黙に使わない）。足りなければ null", async () => {
    const { swoConfigureParams } = await import("../src/lib/terminal");
    expect(swoConfigureParams("", "2000000", "")).toBeNull();
    expect(swoConfigureParams("0, 2", "", "")).toBeNull();
    expect(swoConfigureParams("0, 32", "2000000", "")).toBeNull();          // ITM のポートは 0〜31
    expect(swoConfigureParams("0 x", "2000000", "")).toBeNull();
    expect(swoConfigureParams("0, 2 5", "2000000", "")).toEqual({ ports: [0, 2, 5], swo_baud: 2000000, core_clock_hz: null });
    expect(swoConfigureParams("1", "6000000", "72000000")).toEqual({ ports: [1], swo_baud: 6000000, core_clock_hz: 72000000 });
  });
});
