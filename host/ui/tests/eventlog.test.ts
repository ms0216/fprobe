// Event Log: 検索の条件 → recording.find の query、要約・CSV・印、区間の統計の棒と外れ値、印とメモの置いた者
import { describe, expect, it } from "vitest";
import {
  emptyCondition, eventsCsv, histogramBars, intervalsInBin, kindRows, longestMarks, notesOf,
  fieldText, measureParams, parseNumber, searchMarks, summary, toQuery, type RecEvent,
} from "../src/lib/eventlog";
import { venum, vint } from "./value-kinds";

const ev = (p: Partial<RecEvent>): RecEvent => ({ t: 0, stream: "rtos", kind: "isr", a: 0, b: 0, body: null, t_end: null,
                                                  uncertainty: null, ...p });

describe("検索の条件", () => {
  it("空の欄は条件にしない。範囲は t0 / t1", () => {
    expect(toQuery(emptyCondition())).toEqual({});
    expect(toQuery(emptyCondition(), [10, 20])).toEqual({ t0: 10, t1: 20 });
  });

  it("stream・kind（「,」で複数）・a / b の比較・body の正規表現と値", () => {
    const c = { ...emptyCondition(), stream: "rtos", kind: "isr, run", a: "0x0F", bOp: "ge" as const, b: "100",
                field: "name", value: "Sys.*" };
    expect(toQuery(c)).toEqual({ stream: "rtos", kind: ["isr", "run"], a: 15, b: { ge: 100 },
                                 body: { name: { regex: "Sys.*" } } });
    expect(toQuery({ ...emptyCondition(), field: "seq", regex: false, value: "7" })).toEqual({ body: { seq: 7 } });
    expect(toQuery({ ...emptyCondition(), field: "client", regex: false, value: "ui" })).toEqual({ body: { client: "ui" } });
  });

  it("数は 10 進・0x・負。読めなければ投げる", () => {
    expect([parseNumber("12"), parseNumber("0x1_0"), parseNumber("-0x10"), parseNumber("-3")]).toEqual([12, 16, -16, -3]);
    expect(() => parseNumber("abc")).toThrow();
    expect(() => toQuery({ ...emptyCondition(), a: "zz" })).toThrow();
  });
});

describe("結果の表示", () => {
  it("本文の要約は stream・kind ごとの読みやすい欄（本文のキーの有無で選ばない）", () => {
    const decoded = (body: object) => ev({ stream: "annotation", kind: "uart.byte", body });
    expect(summary(decoded({ text: "'A'", error: null }))).toBe("'A'");
    expect(summary(decoded({ text: "framing error", error: "framing" }))).toBe("framing error (framing)");
    expect(summary(ev({ stream: "annotation", kind: "mark", body: { text: "ここ" } }))).toBe("ここ");
    expect(summary(ev({ stream: "host", kind: "call", body: { client: "claude", method: "memory.read", error: "x" } })))
      .toBe("claude: memory.read — x");
    expect(summary(ev({ stream: "rtos", kind: "switch", body: { from: "main", to: "IDLE" } }))).toBe("main → IDLE");
    expect(summary(ev({ stream: "rtos", kind: "run", body: { task: "main", method: "itm" } }))).toBe("main");
    expect(summary(ev({ stream: "rtos", kind: "isr", body: { name: "SysTick" } }))).toBe("SysTick");
    expect(summary(ev({ stream: "user", kind: "value", body: { name: "temperature", value: 24.5 } })))
      .toBe("temperature = 24.5");
    expect(summary(ev({ stream: "target", kind: "state", body: { state: "HALTED", halt_reason: "breakpoint" } })))
      .toBe("HALTED breakpoint");
    // SWO のデータトレースの値は fprobe.dwarf.values の形（kind で表す）
    expect(summary(ev({ stream: "swo", kind: "data_access", body: { name: "counter", value: vint(7) } })))
      .toBe("counter = 7 (0x7)");
    expect(summary(ev({ stream: "swo", kind: "data_access", body: { name: "mode", value: venum("RUN", 1) } })))
      .toBe("mode = RUN (1)");
    // 古い記録の値（kind の無い形）は推して読み替えず JSON のまま
    expect(summary(ev({ stream: "swo", kind: "data_access", body: { name: "mode", value: { enum: "RUN", value: 1 } } })))
      .toBe('mode = {"enum":"RUN","value":1}');
    // 決めていない stream・kind は、本文に text・name 等があっても JSON
    expect(summary(ev({ stream: "flash", kind: "program", body: { name: "x" } }))).toBe('{"name":"x"}');
    expect(summary(ev({ stream: "uart", b: 0x41 }))).toBe("'A'");
    expect(summary(ev({ stream: "dap", a: 0x20000010, b: 255 }))).toBe("0x20000010 = 0xFF");
  });

  it("a・b の列は内容の列と同じ表記（番地・値・ハンドルは 16 進、量は 10 進）", () => {
    const dap = ev({ stream: "dap", a: 0x20000010, b: 255 });
    expect([fieldText(dap, "a"), fieldText(dap, "b")]).toEqual(["0x20000010", "0xFF"]);
    expect(fieldText(ev({ stream: "rtos", a: 0x20001234 }), "a")).toBe("0x20001234");
    expect(fieldText(ev({ stream: "uart", b: 0x41 }), "b")).toBe("0x41");
    expect(fieldText(ev({ stream: "aux", b: 3300 }), "b")).toBe("3300");
    expect(fieldText(ev({ stream: "host", a: 7 }), "a")).toBe("7");
  });

  it("時刻は始めからの秒を 1 単位が見える桁で", () => {
  });

  it("CSV は秒・元の時刻・欄・要約・本文", () => {
    const csv = eventsCsv([ev({ t: 10, stream: "annotation", kind: "mark", body: { text: "a,b" } })], 0, 0.1);
    expect(csv.split("\n")[1]).toBe('1.00000000000,10,,annotation,mark,0,0,"a,b","{""text"":""a,b""}"');
  });

  it("結果は時間軸の印に（区間は tEnd）", () => {
    expect(searchMarks([ev({ t: 5, t_end: 9, body: { name: "SysTick" } }), ev({ t: 7 })]))
      .toEqual([{ t: 5, tEnd: 9, label: "SysTick" }, { t: 7, label: "rtos.isr" }]);
  });
});

describe("区間の統計", () => {
  const hist = [{ lo: 0, hi: 10, count: 3 }, { lo: 10, hi: 20, count: 1 }];
  it("棒 → その区間（生データ）は timing.measure の interval_bins で選ぶ（画面で棒を求めない。b21）", () => {
    // 時間と棒の範囲が食い違う interval_bins を渡し、画面がデーモンの棒の番号だけを見ていることを確かめる
    const intervals: [number, number][] = [[100, 3], [200, 12], [300, 20]];
    expect(intervalsInBin(intervals, [1, 0, 1], 1)).toEqual([[100, 3], [300, 20]]);
    expect(intervalsInBin(intervals, [1, 0, 1], 0)).toEqual([[200, 12]]);
  });

  it("長い順の上位のある棒（longest の bin）・選んだ棒を目立たせる（外れ値の経験則は使わない。b21）", () => {
    const bars = histogramBars(hist, 10, null, [{ t: 300, t_end: 320, duration: 20, bin: 0 }]);
    expect(bars.map((b) => [b.x0, b.x1, b.y, b.mark])).toEqual([[0, 1, 3, true], [1, 2, 1, false]]);
    expect(histogramBars(hist, 10, 1, [])[1].mark).toBe(true);
  });

  it("長い順の上位は区間の印（普通の色。異常とは言わない）", () => {
    expect(longestMarks([{ t: 300, t_end: 320, duration: 20, bin: 1 }], 10_000_000))
      .toEqual([{ t: 300, tEnd: 320, label: "2 µs", tone: 0 }]);
  });
});

describe("調査の印とメモ", () => {
  it("置いた者は印の本文の事実（デーモンが残した client・フックの hook）だけ。無ければ null（操作の記録から推さない）", () => {
    const marks = [ev({ t: 100, stream: "annotation", kind: "mark",
                        body: { text: "ここ", client: { id: 4, name: "claude", role: "ai" } } }),
                   ev({ t: 200, stream: "annotation", kind: "mark", body: { text: "halt", hook: 3 } }),
                   ev({ t: 300, stream: "annotation", kind: "mark", body: { text: "古い印" } })];
    expect(notesOf(marks)).toEqual([
      { t: 100, text: "ここ", by: "claude", role: "ai" },
      { t: 200, text: "halt", by: "hook 3", role: "hook" },
      { t: 300, text: "古い印", by: null, role: null },
    ]);
    expect(notesOf.length).toBe(1);                                   // 操作の記録を受け取らない
  });
});

describe("記録の概要（recording.kinds の応答を描く）", () => {
  it("応答の行をそのまま表に（並び・割合・1 秒あたりは変えない）。max は棒の目盛り", () => {
    const out = kindRows({ total: 41, kinds: [
      { stream: "rtos", kind: "isr", count: 30, first: 100, last: 1100, share: 30 / 41, per_second: 15 },
      { stream: "uart", kind: "rx", count: 10, first: 0, last: 900, share: 10 / 41, per_second: null }] });
    expect(out.total).toBe(41);
    expect(out.max).toBe(30);
    expect(out.rows.map((r) => `${r.stream}.${r.kind}`)).toEqual(["rtos.isr", "uart.rx"]);
    expect(out.rows[0]).toMatchObject({ count: 30, first: 100, last: 1100, perSecond: 15 });
    expect(out.rows[1].perSecond).toBeNull();
    expect(kindRows({})).toEqual({ rows: [], total: 0, max: 0 });
  });
});

describe("区間の統計の引数（timing.measure）", () => {
  it("棒の数・長い順の上位の数は使う人が入れたものだけ。無ければ送らない（24 本・API の既定の 10 件を暗黙に使わない）", () => {
    const start = { ...emptyCondition(), stream: "rtos" }, end = { ...emptyCondition(), stream: "uart" };
    expect(measureParams(start, end, "", "5")).toBeNull();
    expect(measureParams(start, end, "12", "")).toBeNull();
    expect(measureParams(start, end, "0", "5")).toBeNull();
    expect(measureParams(start, end, "12", "5")).toEqual({ start: toQuery(start), end: toQuery(end), bins: 12, longest: 5 });
  });
});
