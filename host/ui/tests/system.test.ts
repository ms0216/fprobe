// システム解析のパネルの計算: 止めずに読む番地の書き方・FPGA の値の書き方・条件の回数（デーモンが数える）
import { describe, expect, it } from "vitest";
import { fieldsText, parseAddress, sanitizeWatches } from "../src/lib/system";
import * as system from "../src/lib/system";
import { FakeConn } from "./fake-conn";

describe("番地の書き方", () => {
  it("16 進・10 進。32 ビットの語なので 4 の倍数だけ", () => {
    expect(parseAddress("0x20040800")).toBe(0x20040800);
    expect(parseAddress(" 536873984 ")).toBe(536873984);
    expect(parseAddress("0x20040802")).toBeNull();
    expect(parseAddress("0x1_0000_0000")).toBeNull();
    expect(parseAddress("abc")).toBeNull();
    expect(parseAddress("")).toBeNull();
  });
  it("保存した一覧の崩れたものは捨てる", () => {
    expect(sanitizeWatches([{ name: "count", address: 0x20040804 }, { name: "", address: 3 }, null, "x"]))
      .toEqual([{ name: "count", address: 0x20040804 }]);
  });
});

describe("画面で推さない・数えない（読んだ値と時刻、デーモンの事実を描く）", () => {
  it("メモリの毎秒の増え方を画面で計算しない（32 ビットの一周を前進とみなす仮定を持たない）", () => {
    expect(system).not.toHaveProperty("rates");
  });
  it("条件の回数を画面で数えない（暗黙の窓 2 秒・最後の 5000 件を持たない）", () => {
    expect(system).not.toHaveProperty("countCondition");
    expect(system).not.toHaveProperty("RATE_WINDOW_US");
  });
  it("watch を名前・番地の一致で自分のものと推さない", () => {
    expect(system).not.toHaveProperty("ownWatch");
  });
  it("記録のメモリの読みの速さを経験の値で決めない（使う人が欄で明示する）", () => {
    expect(system).not.toHaveProperty("memoryRateHz");
  });
});

describe("使う人が明示する値の欄", () => {
  it("読みの速さ（回/秒）: 正の数だけ。空・誤りは null（既定の値を持たない = 始められない）", () => {
    expect(system.parseRate("200")).toBe(200);
    expect(system.parseRate(" 12.5 ")).toBe(12.5);
    expect(system.parseRate("")).toBeNull();
    expect(system.parseRate("0")).toBeNull();
    expect(system.parseRate("-5")).toBeNull();
    expect(system.parseRate("abc")).toBeNull();
  });
  it("数える件数（最後の N 件）: 正の整数だけ。空・誤りは null（数えない）", () => {
    expect(system.parseCount("5000")).toBe(5000);
    expect(system.parseCount("")).toBeNull();
    expect(system.parseCount("0")).toBeNull();
    expect(system.parseCount("1.5")).toBeNull();
    expect(system.parseCount("x")).toBeNull();
  });
});

describe("条件の回数はデーモン（fpga_obs.count）が数える", () => {
  it("使う人が入れた件数で問い合わせ、条件ごとの事実（回数・数えた範囲）をそのまま返す。件数が無ければ問い合わせない", async () => {
    const r = { condition: "restart rose", count: 3, values: 40, t0_us: 0, t1_us: 2e6, span_s: 2, gaps: 0, lost: 0 };
    const conn = new FakeConn({ "fpga_obs.count": () => ({ last: 500, conditions: [r, { condition: "bad", error: "読めない" }] }) });
    expect(await system.readConditionCounts(conn as any, ["restart rose", "bad"], 500))
      .toEqual({ "restart rose": r, bad: { condition: "bad", error: "読めない" } });
    expect(conn.calls).toEqual([["fpga_obs.count", { conditions: ["restart rose", "bad"], last: 500 }]]);
    expect(conn.quiet).toContain("fpga_obs.count");
    expect(await system.readConditionCounts(conn as any, ["restart rose"], null)).toBeNull();
    expect(await system.readConditionCounts(conn as any, [], 500)).toBeNull();
    expect(conn.calls.length).toBe(1);
  });
});

describe("FPGA の値の書き方", () => {
  it("信号の名前があればそれを、無ければ組の値を 16 進で", () => {
    expect(fieldsText({ state: "BUSY", addr: 6 }, 0x00C5)).toBe("state=BUSY  addr=6");
    expect(fieldsText({ value: 0x00C5 }, 0x00C5)).toBe("0x00C5");
    expect(fieldsText(undefined, 0x12)).toBe("0x0012");
  });
});

describe("FPGA の条件", () => {
  it("「変化」と「= 値」を and / かつ でつなぐ", async () => {
    const { parseCondition } = await import("../src/lib/system");
    expect(parseCondition("restart changed and state=BUSY")).toEqual(
      [{ field: "restart", op: "changed" }, { field: "state", op: "=", value: "BUSY" }]);
    expect(parseCondition("restart が変化 かつ state = BUSY")).toEqual(
      [{ field: "restart", op: "changed" }, { field: "state", op: "=", value: "BUSY" }]);
    expect(parseCondition("")).toBeNull();
    expect(parseCondition("state >= 2")).toBeNull();
  });
});


describe("結果の直前に多い出来事の例へ飛ぶ", () => {
  it("候補から結果までを、前後に余白（間の半分、最小 50 µs）を付けた範囲にし、基準 = 候補・選択 = 結果のカーソル", async () => {
    const { exampleView } = await import("../src/lib/system");
    expect(exampleView({ cause_t: 1_000_000, effect_t: 1_400_000 }, 100e6)).toEqual({
      view: { source: "recording", t0: 800_000, t1: 1_600_000 },
      cursors: { reference: 1_000_000, selection: 1_400_000 } });                   // 差 = 遅れ
    expect(exampleView({ cause_t: 1_000_000, effect_t: 1_000_000 }, 100e6).view)   // 同じ時刻: 最小の余白
      .toEqual({ source: "recording", t0: 995_000, t1: 1_005_000 });
  });
});

describe("条件の中の名前", () => {
  it("空白・= ・\" を含む名前は \"…\" で囲む（結果の直前に多い出来事の条件で指せるように）", async () => {
    const { termName } = await import("../src/lib/system");
    expect(termName("誤り")).toBe("誤り");
    expect(termName("Error count")).toBe('"Error count"');
    expect(termName("a=b")).toBe('"a=b"');
  });
});

describe("表と条件をデーモンの画面の状態で分け合う（別の PC・AI が同じものを見る）", () => {
  const tick = () => new Promise((r) => setTimeout(r, 0));
  const W = [{ name: "誤り", address: 0x20040808 }];
  const C = [{ name: "計算中の頼み直し", expr: "restart changed and state=BUSY" }];

  it("デーモンにあればそれを使い、無ければ手元のものを上げる。ほかの窓の変更を受け、自分の通知は読み飛ばす", async () => {
    const { SharedSystem } = await import("../src/lib/system");
    const { FakeConn } = await import("./fake-conn");
    let state: any = { system: { watches: W, conditions: C } };
    const conn = new FakeConn({ "ui.state.get": () => state, "ui.state.set": (p: any) => { state = { system: p.system }; return state; } });
    const got: any[] = [];
    const shared = new SharedSystem(conn as any, (w, c) => got.push([w, c]));
    await shared.load([], []);
    expect(got).toEqual([[W, C]]);
    state = { system: null };
    const other = new SharedSystem(conn as any, () => {});
    await other.load(W, []);
    expect(conn.calls.at(-1)).toEqual(["ui.state.set", { system: { watches: W, conditions: [] }, origin: other.origin }]);
    conn.emit("ui.state", { origin: "w-other", changed: ["system"], state: { system: { watches: [], conditions: C } } });
    conn.emit("ui.state", { origin: shared.origin, changed: ["system"], state: { system: { watches: W, conditions: [] } } });
    await tick();
    expect(got.at(-1)).toEqual([[], C]);
    expect(got.length).toBe(2);
    shared.dispose();
  });
});

describe("変数名で MCU のメモリを足す", () => {
  it("ELF の変数（debug.symbol.info）の番地を使う。名前が空なら変数名。関数・語の境でない番地は断る", async () => {
    const { isSymbolName, symbolWatch } = await import("../src/lib/system");
    expect(isSymbolName("g_errors")).toBe(true);
    expect(isSymbolName("stats.count")).toBe(true);
    expect(isSymbolName("0x20040808")).toBe(false);
    expect(isSymbolName("a b")).toBe(false);
    expect(symbolWatch("g_errors", "", { address: 0x20000010, kind: "object", size: 4 })).toEqual({ name: "g_errors", address: 0x20000010 });
    expect(symbolWatch("g_errors", "誤り", { address: 0x20000010, kind: "object" })).toEqual({ name: "誤り", address: 0x20000010 });
    expect(symbolWatch("main", "", { address: 0x08000100, kind: "function" })).toBe("system.notVariable");
    expect(symbolWatch("g_flag", "", { address: 0x20000011, kind: "object", size: 1 })).toBe("system.unaligned");
  });
});

describe("FPGA の条件を結果の直前に多い出来事と同じ書き方に", () => {
  it("rose / fell と \"…\" で囲んだ名前・値も読める", async () => {
    const { parseCondition } = await import("../src/lib/system");
    expect(parseCondition("restart rose")).toEqual([{ field: "restart", op: "rose" }]);
    expect(parseCondition('"my flag" fell and state="WAIT ACK"')).toEqual(
      [{ field: "my flag", op: "fell" }, { field: "state", op: "=", value: "WAIT ACK" }]);
  });
  it("FPGA の信号だけの条件か（条件の表に足せるか）", async () => {
    const { fpgaOnly } = await import("../src/lib/system");
    const names = ["state", "restart", "addr"];
    expect(fpgaOnly("restart changed and state=BUSY", names)).toBe(true);
    expect(fpgaOnly("誤り changed", names)).toBe(false);
    expect(fpgaOnly("in0 rose", names)).toBe(false);
    expect(fpgaOnly("fpga.restart rose", names)).toBe(true);
  });
});

describe("記録の欠けと分解能はデーモンの事実を描く（b18・b19）", () => {
  it("記録中にメモリの読みが止まった = recording.status の watch_running が false（watch.status を別に読まない）", () => {
    const { memoryStopped } = system;
    expect(memoryStopped({ recording: true, watch_running: false }, true)).toBe(true);
    expect(memoryStopped({ recording: true, watch_running: true }, true)).toBe(false);
    expect(memoryStopped({ recording: true }, true)).toBe(false);               // 申告の無いデーモン: 止まったとは言わない
    expect(memoryStopped({ recording: false, watch_running: false }, true)).toBe(false);
    expect(memoryStopped({ recording: true, watch_running: false }, false)).toBe(false);   // この画面はメモリを記録していない
  });
  it("recording.correlate の sampling は変数ごとにそのまま（最大を取って 1 つにしない）", () => {
    expect(system.samplingRows({ errors: 0.001, "fpga.state": null, mailbox: 0.0005 })).toEqual(
      [{ name: "errors", ms: 1 }, { name: "fpga.state", ms: null }, { name: "mailbox", ms: 0.5 }]);
    expect(system.samplingRows({})).toEqual([]);
  });
});

describe("MCU の番地の語を書く（target.memory.write。走行中も）", () => {
  const fakeApp = (ai: boolean) => {
    const calls: [string, unknown][] = [];
    return { calls, aiConnectedUnpaused: ai, act: async (m: string, p: unknown) => { calls.push([m, p]); return {}; } };
  };
  const answer = (ok: boolean) => ({ ask: async () => ok }) as any;

  it("10 進・16 進の 32 ビットの値 → リトルエンディアンの 4 バイト", async () => {
    const app = fakeApp(false);
    expect(await system.writeWord(app, 0x20000010, "0x12345678")).toBeNull();
    expect(await system.writeWord(app, 0x20000014, " 4_294_967_295 ")).toBeNull();
    expect(app.calls).toEqual([["target.memory.write", { address: 0x20000010, data_hex: "78563412" }],
                               ["target.memory.write", { address: 0x20000014, data_hex: "ffffffff" }]]);
  });
  it("32 ビットに入らない・数でない値は書かない", async () => {
    const app = fakeApp(false);
    for (const bad of ["", "abc", "0x1_0000_0000", "-1", "1.5"]) expect(await system.writeWord(app, 0x20000010, bad)).toBe("system.badWordValue");
    expect(app.calls).toEqual([]);
  });
  it("AI がつながっていて一時停止していなければ確かめる（人の操作と AI の操作は同じ道。断れば書かない）", async () => {
    const refused = fakeApp(true);
    expect(await system.writeWord(refused, 0x20000010, "1", answer(false))).toBeNull();
    expect(refused.calls).toEqual([]);
    const accepted = fakeApp(true);
    await system.writeWord(accepted, 0x20000010, "1", answer(true));
    expect(accepted.calls).toEqual([["target.memory.write", { address: 0x20000010, data_hex: "01000000" }]]);
  });
});


describe("結果の直前に多い出来事の引数（recording.correlate）", () => {
  it("結果の条件は使う人が入れたものだけ。空なら送らない（表の先頭の変数の changed を暗黙に使わない）", () => {
    expect(system.correlateParams("", ["a rose"], false, 5, null)).toBeNull();
    expect(system.correlateParams("   ", ["a rose"], true, 5, 20)).toBeNull();
    expect(system.correlateParams(" err changed ", ["a rose"], false, 5, null))
      .toEqual({ effect: "err changed", causes: ["a rose"], window_s: 0.005 });
    expect(system.correlateParams("err changed", ["a rose"], true, 5, 7))
      .toEqual({ effect: "err changed", causes: null, window_s: 0.005, limit: 7 });
  });
  it("窓が無い・候補が無い（自動でない）ときも送らない", () => {
    expect(system.correlateParams("err changed", ["a rose"], false, null, null)).toBeNull();
    expect(system.correlateParams("err changed", [], false, 5, null)).toBeNull();
  });
  it("自動の候補は返す数（上位の数）を使う人が入れたときだけ（API の既定の 20 を暗黙に使わない）", () => {
    expect(system.correlateParams("err changed", [], true, 5, null)).toBeNull();
  });
});
