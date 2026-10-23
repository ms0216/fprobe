// Event Log: 検索の条件 → recording.find の query、要約・CSV・印、区間の統計の棒と外れ値、印とメモの置いた者
import { describe, expect, it } from "vitest";
import {
  binOf, emptyCondition, eventsCsv, histogramBars, intervalsInBin, kindQuery, kindStats, longestMarks, notesOf,
  fieldText, parseNumber, searchMarks, summary, toQuery, type RecEvent,
} from "../src/lib/eventlog";

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
  it("本文の要約は種類ごとの読みやすい欄", () => {
    expect(summary(ev({ body: { text: "'A'", error: null } }))).toBe("'A'");
    expect(summary(ev({ body: { text: "framing error", error: "framing" } }))).toBe("framing error (framing)");
    expect(summary(ev({ stream: "host", body: { client: "claude", method: "memory.read", error: "x" } })))
      .toBe("claude: memory.read — x");
    expect(summary(ev({ body: { from: "main", to: "IDLE" } }))).toBe("main → IDLE");
    expect(summary(ev({ body: { name: "temperature", value: 24.5 } }))).toBe("temperature = 24.5");
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
    const csv = eventsCsv([ev({ t: 10, body: { text: "a,b" } })], 0, 0.1);
    expect(csv.split("\n")[1]).toBe('1.00000000000,10,,rtos,isr,0,0,"a,b","{""text"":""a,b""}"');
  });

  it("結果は時間軸の印に（区間は tEnd）", () => {
    expect(searchMarks([ev({ t: 5, t_end: 9, body: { name: "SysTick" } }), ev({ t: 7 })]))
      .toEqual([{ t: 5, tEnd: 9, label: "SysTick" }, { t: 7, label: "rtos.isr" }]);
  });
});

describe("区間の統計", () => {
  const hist = [{ lo: 0, hi: 10, count: 3 }, { lo: 10, hi: 20, count: 1 }];
  it("棒の分け方は Python の Distribution.histogram と同じ（最後の棒は最大を含む）", () => {
    expect([binOf(0, hist), binOf(9.9, hist), binOf(10, hist), binOf(20, hist), binOf(21, hist)]).toEqual([0, 0, 1, 1, -1]);
    expect(binOf(5, [{ lo: 5, hi: 5, count: 4 }])).toBe(0);
  });

  it("棒 → その区間（生データ）。長い順の上位のある棒・選んだ棒を目立たせる（外れ値の経験則は使わない）", () => {
    const intervals: [number, number][] = [[100, 3], [200, 12], [300, 20]];
    expect(intervalsInBin(intervals, hist, 1)).toEqual([[200, 12], [300, 20]]);
    const bars = histogramBars(hist, 10, null, [{ t: 300, t_end: 320, duration: 20 }]);
    expect(bars.map((b) => [b.x0, b.x1, b.y, b.mark])).toEqual([[0, 1, 3, false], [1, 2, 1, true]]);
    expect(histogramBars(hist, 10, 0, [])[0].mark).toBe(true);
  });

  it("長い順の上位は区間の印（普通の色。異常とは言わない）", () => {
    expect(longestMarks([{ t: 300, t_end: 320, duration: 20 }], 10_000_000))
      .toEqual([{ t: 300, tEnd: 320, label: "2 µs", tone: 0 }]);
  });
});

describe("調査の印とメモ", () => {
  it("置いた者は同じ本文の recording.mark の操作から。フックの印は hook。分からなければ null", () => {
    const marks = [ev({ t: 100, stream: "annotation", kind: "mark", body: { text: "ここ" } }),
                   ev({ t: 200, stream: "annotation", kind: "mark", body: { text: "halt", hook: 3 } }),
                   ev({ t: 300, stream: "annotation", kind: "mark", body: { text: "古い印" } })];
    const calls = [ev({ t: 5000, stream: "host", kind: "call", body: { client: "claude", role: "ai", method: "recording.mark",
                                                                       params: { text: "ここ", t: 100 } } }),
                   ev({ t: 120, stream: "host", kind: "call", body: { client: "ui", role: "human", method: "target.halt" } })];
    expect(notesOf(marks, calls)).toEqual([
      { t: 100, text: "ここ", by: "claude", role: "ai" },
      { t: 200, text: "halt", by: "hook 3", role: "hook" },
      { t: 300, text: "古い印", by: null, role: null },
    ]);
  });
});

describe("記録の概要（stream・kind ごとの件数）", () => {
  it("問い合わせ: 全体は範囲の条件なし、範囲は t0 / t1（SQL の条件と、長期の記録の分割を選ぶ引数の両方）", () => {
    const all = kindQuery(null);
    expect(all.sql).toMatch(/GROUP BY stream, kind/);
    expect(all.sql).not.toMatch(/WHERE/);
    expect(all.params).toEqual([]);
    expect(all.t0).toBeUndefined();
    const part = kindQuery([100, 200]);
    expect(part.sql).toMatch(/WHERE t >= \? AND t < \?/);
    expect(part.params).toEqual([100, 200]);
    expect(part).toMatchObject({ t0: 100, t1: 200 });
  });

  it("結果の行 → 件数の多い順の表（割合・1 秒あたり・最初・最後）。合計と、件数の最大", () => {
    const out = kindStats({ rows:[["uart", "rx", 10, 0, 900], ["rtos", "isr", 30, 100, 1100], ["aux", "mv", 1, 50, 50]] },
                          1000, 2000);
    expect(out.total).toBe(41);
    expect(out.rows.map((r) => `${r.stream}.${r.kind}`)).toEqual(["rtos.isr", "uart.rx", "aux.mv"]);
    expect(out.rows[0]).toMatchObject({ count: 30, first: 100, last: 1100 });
    expect(out.rows[0].share).toBeCloseTo(30 / 41, 12);
    expect(out.rows[0].perSecond).toBeCloseTo(15, 12);    // 30 件 / 2000 単位（1 kHz = 2 秒）
    expect(out.max).toBe(30);
  });

  it("時間の長さが分からなければ 1 秒あたりは NaN。行が無ければ空", () => {
    const out = kindStats({ rows: [["uart", "rx", 3, 0, 9]] }, 0, 100);
    expect(Number.isNaN(out.rows[0].perSecond)).toBe(true);
    expect(kindStats({}, 1000, 10)).toEqual({ rows: [], total: 0, max: 0 });
  });
});
