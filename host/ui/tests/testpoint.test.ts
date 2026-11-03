// テストポイント（lib/testpoint.ts）: 方式ごとの欄の出し分け（仕様の全引数を覆う）、testpoint.start の引数、状態の行、
// 切った事実、時刻の欄の飛ぶ先、読みの誤り
import { describe, expect, it } from "vitest";
import { methodFields } from "../src/lib/crosstrigger";
import {
  COMMON_FIELDS, METHOD_FIELDS, cut, pairText, readQuiet, revealOf, startCall, startFields, statusItems, testpointMethods,
} from "../src/lib/testpoint";
import { FakeConn, deviceError } from "./fake-conn";

describe("方式ごとの欄", () => {
  it("方式の選択肢は仕様の method の列挙のまま。表の方式と同じ", () => {
    expect(testpointMethods().sort()).toEqual(Object.keys(METHOD_FIELDS).sort());
  });

  it("仕様の testpoint.start の引数（method 以外）は、どれかの組に入る（仕様に足されたら気づく）", () => {
    const covered = new Set([...COMMON_FIELDS, ...Object.values(METHOD_FIELDS).flat()]);
    const spec = methodFields("testpoint.start").map((f) => f.name).filter((n) => n !== "method");
    expect(spec.filter((n) => !covered.has(n))).toEqual([]);
    expect([...covered].filter((n) => !spec.includes(n))).toEqual([]);      // 表に仕様に無い名前を書かない
  });

  it("itm は SWO の欄と共通の欄だけ（仕様の順）。表に無い方式は全部の欄を出す", () => {
    const names = startFields("itm").map((f) => f.name);
    expect(names).toContain("swo_baud");
    expect(names).toContain("map_path");
    expect(names).not.toContain("clock_channel");
    const all = methodFields("testpoint.start").filter((f) => f.name !== "method").map((f) => f.name);
    expect(names).toEqual(all.filter((n) => names.includes(n)));
    expect(startFields("future_method").map((f) => f.name)).toEqual(all);
  });
});

describe("startCall", () => {
  it("方式が無ければ欄の誤り。空の欄は送らない（既定はデーモン）", () => {
    expect(startCall("", {})).toEqual({ errors: { method: { code: "required" } } });
    expect(startCall("rtt", { rtt_channel: "", core_clock_hz: "64000000" }))
      .toEqual({ params: { method: "rtt", core_clock_hz: 64000000 } });
  });

  it("型に合わない欄は送らずに欄の誤り", () => {
    const r = startCall("gpio_serial", { clock_channel: "x" });
    expect("errors" in r && Object.keys(r.errors)).toEqual(["clock_channel"]);
  });
});

describe("状態の行（testpoint.status）", () => {
  it("まだ一度も始めていない（running: false だけ）は出さない", () => {
    expect(statusItems({ running: false })).toBeNull();
    expect(statusItems(null)).toBeNull();
  });

  it("API の名前と値のまま。無い欄は出さない。取りこぼし・overrun も出す", () => {
    expect(statusItems({ running: true, method: "gpio_serial", hits: 10, losses: 2, elapsed_s: 1.234, recorded: false,
                         stream: { overrun: 0 }, warnings: [] }))
      .toEqual([["method", "gpio_serial"], ["hits", "10"], ["losses", "2"], ["elapsed_s", "1.2"], ["recorded", "false"],
                ["stream.overrun", "0"]]);
  });
});

describe("切った事実・時刻・組", () => {
  it("総数が表示より多いときだけ切った事実（上限は API が使った値）", () => {
    expect(cut(20, 20, 20)).toBeNull();
    expect(cut(20, 31, 20)).toEqual({ shown: 20, total: 31, limit: 20 });
    expect(cut(3, undefined, 20)).toBeNull();
  });

  it("時間軸へは API が記録のタイムベース t を返したときだけ飛ぶ（t_s だけでは飛ばない）", () => {
    expect(revealOf({ t: 1234 })).toEqual({ source: "recording", t: 1234 });
    expect(revealOf({})).toBeNull();
    expect(revealOf({ t: null })).toBeNull();
  });

  it("通った / 全部は API の組のまま", () => {
    expect(pairText([3, 5])).toBe("3 / 5");
    expect(pairText(undefined)).toBe("—");
  });
});

describe("readQuiet", () => {
  it("quiet で読み、誤りは文で返す", async () => {
    const conn = new FakeConn({ "testpoint.report": { hits: 3 }, "testpoint.calls": () => { throw deviceError("結果が無い"); } });
    expect(await readQuiet(conn, "testpoint.report", { top: 5 })).toEqual({ result: { hits: 3 }, error: null });
    expect(conn.calls).toEqual([["testpoint.report", { top: 5 }]]);
    expect(conn.quiet).toEqual(["testpoint.report"]);
    expect(await readQuiet(conn, "testpoint.calls")).toEqual({ result: null, error: "結果が無い" });
  });
});
