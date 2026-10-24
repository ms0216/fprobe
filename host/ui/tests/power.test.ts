// Power: 電圧の履歴の描き方・記録の電圧（統計・低下の区間はデーモンの probe.voltage_history・recording.voltage）
import { FakeConn } from "./fake-conn";
import { describe, expect, it } from "vitest";
import { findPages, formatVolts, historyCsv, historySeries, measuredVolts, recordingVolts } from "../src/lib/power";

const ok = (volts: number) => ({ volts, over_range: false, live: true });

describe("いまの電圧の表示（補助の Pico が無い・送っていないときは 0 V と出さない）", () => {
  it("測れているときだけ値、ほかは null（画面は「データなし」）", () => {
    expect(measuredVolts(ok(3.3))).toBe(3.3);
    expect(measuredVolts({ volts: 0, over_range: false, live: false })).toBeNull();
    expect(measuredVolts({ volts: null, over_range: true, live: true })).toBeNull();
    expect(measuredVolts(null)).toBeNull();
  });
});

describe("デーモンが読んだ電圧の履歴（probe.voltage_history）の描き方", () => {
  it("最初の読みからの秒の系列（測れない読みで線を切る）と CSV", () => {
    const readings = [{ time: 100, ...ok(3.3) }, { time: 101, volts: null, over_range: true, live: true },
                      { time: 102.5, volts: 3.2, over_range: false, live: false }];
    const series = historySeries(readings, "V");
    expect(series.x).toEqual([0, 1, 2.5]);
    expect(series.y[0]).toBe(3.3);
    expect(series.y[1]).toBeNaN();
    expect(series.y[2]).toBeNaN();
    expect(historyCsv(readings).split("\n")[0]).toBe("time,volts,over_range,live");
    expect(historyCsv(readings).split("\n")[2]).toContain(",,true,true");
  });
});

describe("記録の電圧", () => {
  it("aux の mV を V の列に（時刻の順）", () => {
    expect(recordingVolts([{ t: 20, b: 3290, kind: "mv" }, { t: 10, b: 3300, kind: "mv" }, { t: 5, b: 1, kind: "x" }]))
      .toEqual({ x: [10, 20], y: [3.3, 3.29] });
    expect(formatVolts(3.2954)).toBe("3.295 V");
    expect(formatVolts(null)).toBe("—");
  });
});

describe("記録の電圧を読み足す（recording.find）", () => {
  it("API の count（条件に合う数）だけ読み終えるまで続ける（1 ページの件数 1000 を仮定しない）。ページの上限で止めたら示す", async () => {
    const all = Array.from({ length: 7 }, (_, k) => ({ t: k * 10, b: 3300, kind: "mv" }));
    const conn = new FakeConn({ "recording.find": ({ query }: any) => {
      const rest = all.filter((e) => query.t0 === undefined || e.t >= query.t0);
      return { count: rest.length, events: rest.slice(0, 3) };              // 1 ページ 3 件のデーモン
    } });
    const r = await findPages(conn.call.bind(conn) as any, { stream: "aux", kind: "mv" }, 10);
    expect(r.events.map((e) => e.t)).toEqual(all.map((e) => e.t));
    expect(r).toMatchObject({ total: 7, complete: true });
    const cut = await findPages(conn.call.bind(conn) as any, { stream: "aux", kind: "mv" }, 2);
    expect(cut.events.length).toBe(6);
    expect(cut).toMatchObject({ total: 7, complete: false });
  });
});
