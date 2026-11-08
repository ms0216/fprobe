// システム解析のパネルの計算: 止めずに読む番地の書き方・FPGA の値の書き方・条件の回数（デーモンが数える）
import { describe, expect, it } from "vitest";
import { fieldsText, parseAddress, sanitizeWatches } from "../src/lib/system";
import * as system from "../src/lib/system";
import { enumOf, suggestedOf } from "../src/lib/apispec";
import { FakeConn } from "./fake-conn";
import type { AppState } from "../src/lib/state";

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
  it("書き方はデーモン（recording.condition.parse）が確かめる。画面は構文解析しない", async () => {
    const { conditionError } = await import("../src/lib/system");
    const conn = new FakeConn({
      "recording.condition.parse": ({ condition }: { condition: string }) => {
        if (condition === "state >= 2") throw new Error("項 'state >= 2' が読めない");
        return { condition, terms: [] };
      },
    });
    expect(await conditionError(conn, " restart changed and state=BUSY ")).toBeNull();
    expect(conn.calls.at(-1)).toEqual(["recording.condition.parse", { condition: "restart changed and state=BUSY" }]);
    expect(await conditionError(conn, "state >= 2")).toContain("読めない");
    expect(conn.quiet).toEqual(["recording.condition.parse", "recording.condition.parse"]);
    expect("parseCondition" in system || "fpgaOnly" in system).toBe(false);
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
    // 書きの応答（target.memory.write）は使わない: 形は API の応答の型のまま（{} を返す偽物）
    return { calls, aiConnectedUnpaused: ai,
             act: (async (m: string, p: unknown) => { calls.push([m, p]); return {}; }) as AppState["act"] };
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

describe("ピンの垂れ流しの始め方（形式・サンプルレート・チャンネル数は使う人が選ぶ。画面で既定を決めない）", () => {
  it("RLE は、これまでどおり形式だけ（サンプルレートとチャンネル数は送らない）", () => {
    expect(system.pinsStreamStart("rle", "", "", "records")).toEqual({ rle: true, rle_format: "records" });
    expect(system.pinsStreamStart("rle", "50", "8", "tokens", "1000")).toEqual({ rle: true, rle_format: "tokens", tick_us: 1000 });
    expect(system.pinsStreamStart("rle", "", "", "tokens", "0")).toEqual({ rle: true, rle_format: "tokens", tick_us: 0 });
    expect(system.pinsStreamStart("rle", "", "", "tokens", "")).toBeNull();                                                  // tick_us は明示する
    expect(system.pinsStreamStart("rle", "50", "8", "tokens", "1000")).toBeTruthy();              // RAW の欄は無視（RLE は 16 ビットの値の繰り返し）
  });
  it("REPEAT は使う人が選んだときだけ送る（トークン形式のときだけ。選ばなければ API の既定 = 使わない）", () => {
    expect(system.pinsStreamStart("rle", "", "", "tokens", "1000", true)).toEqual({ rle: true, rle_format: "tokens", tick_us: 1000, rle_repeat: true });
    expect(system.pinsStreamStart("rle", "", "", "tokens", "1000", false)).toEqual({ rle: true, rle_format: "tokens", tick_us: 1000 });
    expect(system.pinsStreamStart("rle", "", "", "records", "", true)).toEqual({ rle: true, rle_format: "records" });        // records に REPEAT は無い
    expect(system.pinsStreamStart("rle", "", "", "tokens", "", true)).toBeNull();                                            // tick_us は明示する
  });
  it("変化点の上限は REPEAT を選んだときだけ・入れたときだけ送る（空 = 上限なし。入れたのに誤りなら始められない）", () => {
    expect(system.pinsStreamStart("rle", "", "", "tokens", "1000", true, "100000000")).toEqual({ rle: true, rle_format: "tokens", tick_us: 1000, rle_repeat: true, max_changes: 100000000 });
    expect(system.pinsStreamStart("rle", "", "", "tokens", "1000", true, "")).toEqual({ rle: true, rle_format: "tokens", tick_us: 1000, rle_repeat: true });
    expect(system.pinsStreamStart("rle", "", "", "tokens", "1000", true, "0")).toBeNull();
    expect(system.pinsStreamStart("rle", "", "", "tokens", "1000", true, "1e8")).toBeNull();
    expect(system.pinsStreamStart("rle", "", "", "tokens", "1000", false, "5")).toEqual({ rle: true, rle_format: "tokens", tick_us: 1000 });
  });
  it("RAW は、サンプルレート（MS/s）とチャンネル数の両方が要る。片方でも空・誤りなら始められない（null）", () => {
    expect(system.pinsStreamStart("raw", "", "16", "records")).toBeNull();
    expect(system.pinsStreamStart("raw", "50", "", "records")).toBeNull();
    expect(system.pinsStreamStart("raw", "0", "8", "records")).toBeNull();
    expect(system.pinsStreamStart("raw", "abc", "8", "records")).toBeNull();
    expect(system.pinsStreamStart("raw", "50", "5", "records")).toBeNull();                          // 詰められない数
    expect(system.pinsStreamStart("", "50", "8", "records")).toBeNull();                             // 形式が未選択
  });
  it("RLE の符号化（records / tokens）は使う人が選んだものを明示して送る。空・知らない値は始められない（API の既定に黙って任せない）", () => {
    expect(system.pinsStreamStart("rle", "", "", "")).toBeNull();
    expect(system.pinsStreamStart("rle", "", "", "bogus")).toBeNull();
    expect(system.pinsStreamStart("raw", "25", "16", "")).toEqual({ rle: false, rate_hz: 25e6, pack_channels: null });   // RAW には符号化の選択は関係しない
  });
  it("符号化の選択肢と最初の値は API の仕様から取る（画面のコードに値を書かない）", () => {
    expect(enumOf("recording.la_stream.start", "rle_format")).toEqual(["records", "tokens"]);
    expect(suggestedOf("recording.la_stream.start", "rle_format")).toBe("records");
    expect(enumOf("recording.la_stream.start", "no_such_param")).toEqual([]);
  });
  it("形式の統計は API の欄をそのまま見せる（割り算・判定をしない）。無い欄は — 、古いデーモンは出さない", () => {
    const base = { running: true, blocks: 1, error: null, events: null };
    expect(system.pinsStreamFacts({ ...base, rle_format: "tokens", wire_bytes: 1234567, pad_bytes: 37000, changes: 1180000, bytes_per_change: 1.046 }))
      .toEqual({ format: "tokens", wire: (1234567).toLocaleString(), pad: (37000).toLocaleString(), changes: (1180000).toLocaleString(), perChange: "1.046" });
    expect(system.pinsStreamFacts({ ...base, rle_format: "records", wire_bytes: 64, pad_bytes: 0, changes: null, bytes_per_change: null }))
      .toEqual({ format: "records", wire: "64", pad: "0", changes: "—", perChange: "—" });
    expect(system.pinsStreamFacts({ ...base })).toBeNull();
  });
  it("RAW: 16 ch は詰めない（pack_channels = null）、8・4・2 は詰める。MS/s は Hz にして送る", () => {
    expect(system.pinsStreamStart("raw", "25", "16", "records")).toEqual({ rle: false, rate_hz: 25e6, pack_channels: null });
    expect(system.pinsStreamStart("raw", "50", "8", "records")).toEqual({ rle: false, rate_hz: 50e6, pack_channels: 8 });
    expect(system.pinsStreamStart("raw", " 100 ", "4", "records")).toEqual({ rle: false, rate_hz: 100e6, pack_channels: 4 });
    expect(system.pinsStreamStart("raw", "12.5", "2", "records")).toEqual({ rle: false, rate_hz: 12.5e6, pack_channels: 2 });
  });
});
