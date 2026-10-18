// 時刻・時間の表し方（lib/time.ts）: 全ビュー・時間軸・TimeLink の共通
import { describe, expect, it } from "vitest";
import {
  formatDuration, formatSeconds, formatTick, originFor, parseSeconds, secondsOf, sinceStart, timeText, type Origins,
} from "../src/lib/time";

const origins: Origins = { capture: { t0: 0, tickS: 40e-9 }, recording: { t0: 1000, tickS: 1e-7 } };

describe("時刻・時間の表し方", () => {
  it("時間の長さは単位を大きさで選び、有効数字 4 桁", () => {
    expect(formatDuration(0)).toBe("0 s");
    expect(formatDuration(40e-9)).toBe("40 ns");
    expect(formatDuration(-1.25e-6)).toBe("-1.25 µs");
    expect(formatDuration(0.0012346)).toBe("1.235 ms");
    expect(formatDuration(2)).toBe("2 s");
    expect(formatDuration(NaN)).toBe("—");
  });

  it("時刻は値の大きさで単位を選び、分解能が見える桁（有効数字の上限つき）", () => {
    expect(formatSeconds(1.2345678, 1e-7)).toBe("1.2345678 s");
    expect(formatSeconds(1.2345678, 1e-7, 7)).toBe("1.234568 s");
    expect(formatSeconds(0.05, 1e-3)).toBe("50 ms");
    expect(formatSeconds(12.5e-6, 10e-9)).toBe("12.50 µs");          // 表は桁をそろえる
    expect(formatSeconds(0, 1e-6)).toBe("0 µs");                       // 0 は分解能の単位で
    expect(formatSeconds(-3e-3, 1e-4)).toBe("-3.0 ms");
    expect(formatSeconds(123.456, 1e-9, 6)).toBe("123.456 s");
  });

  it("目盛りは間隔の桁で、末尾の 0 を落とす", () => {
    expect(formatTick(0, 1e-6)).toBe("0");
    expect(formatTick(5e-6, 5e-6)).toBe("5 µs");
    expect(formatTick(1.5e-3, 5e-4)).toBe("1.5 ms");
    expect(formatTick(-2e-5, 1e-5)).toBe("-20 µs");
  });

  it("始めからの秒: 記録は範囲の始め、捕捉はサンプル 0 を 0 とする。µs（タイムベース）も同じ 0", () => {
    expect(sinceStart(origins, { source: "recording", t: 11000 })).toBeCloseTo(1e-3);
    expect(sinceStart(origins, { source: "recording", us: 1100 })).toBeCloseTo(1e-3);
    expect(sinceStart(origins, { source: "capture", t: 100 })).toBeCloseTo(4e-6);
    expect(sinceStart({ capture: null, recording: null }, { source: "recording", t: 5 })).toBeNull();
    expect(timeText(origins, { source: "recording", t: 11000 })).toBe("1.0000 ms");       // 1 単位 = 0.1 µs の桁
    expect(timeText({ capture: null, recording: null }, { source: "recording", t: 5 })).toBeNull();
    expect(secondsOf(origins, "recording")(21000)).toBeCloseTo(2e-3);
    expect(secondsOf({ capture: null, recording: null }, "recording", { t0: 10, tickS: 1 })(15)).toBe(5);
  });
});

describe("データの時刻の 0（L2: 横軸が負にならない）", () => {
  const origins: Origins = { capture: null, recording: { t0: 5000, tickS: 1e-6 } };
  const data = { t0: 1000, tickS: 1e-6 };

  it("開いている記録のデータは記録の始め、別のデータはデータの始めを 0 に", () => {
    expect(originFor(origins, "recording", { t0: 7000, tickS: 1e-6 }, true)).toEqual({ t0: 5000, tickS: 1e-6 });
    expect(originFor(origins, "recording", data, false)).toEqual(data);
    expect(originFor(origins, "recording", data, true)).toEqual(data);         // 記録の始めより前: 別のデータ
    expect(originFor({ capture: null, recording: null }, "recording", data, true)).toEqual(data);
  });
});

describe("時刻の入力（時間軸の「時刻へ」）", () => {
  it("単位（ns・us・µs・ms・s）付き・無し（秒）の数を秒に。空白・大文字小文字は問わない", () => {
    expect(parseSeconds("1.5 ms")).toBeCloseTo(1.5e-3, 15);
    expect(parseSeconds("12us")).toBeCloseTo(12e-6, 15);
    expect(parseSeconds("12 µs")).toBeCloseTo(12e-6, 15);
    expect(parseSeconds("12 μs")).toBeCloseTo(12e-6, 15);
    expect(parseSeconds("40ns")).toBeCloseTo(40e-9, 18);
    expect(parseSeconds(" 2 S ")).toBe(2);
    expect(parseSeconds("3")).toBe(3);
    expect(parseSeconds("-2 ms")).toBeCloseTo(-2e-3, 15);
    expect(parseSeconds("1e-3")).toBeCloseTo(1e-3, 15);
  });

  it("読めない文字は null", () => {
    for (const bad of ["", "  ", "abc", "1.2.3 ms", "5 min", "ms", "1 ms 2"]) expect(parseSeconds(bad), bad).toBeNull();
  });
});
