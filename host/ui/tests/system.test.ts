// システム解析のパネルの計算: 止めずに読む番地の書き方・毎秒の増え方（カウンタ）・FPGA の値の書き方
import { describe, expect, it } from "vitest";
import { fieldsText, parseAddress, rates, sanitizeWatches } from "../src/lib/system";

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

describe("毎秒の増え方", () => {
  it("前の読みとの差を経過時間で割る（32 ビットの一周も）", () => {
    const prev = { t_us: 1_000_000, memory: { "0x20040804": 100, "0x20040808": 0xFFFF_FFF0 } };
    const cur = { t_us: 1_500_000, memory: { "0x20040804": 150, "0x20040808": 0x10 } };
    expect(rates(prev, cur)).toEqual({ "0x20040804": 100, "0x20040808": 64 });
  });
  it("前が無い・時間が進んでいない・番地が増えたものは出さない", () => {
    expect(rates(null, { t_us: 1, memory: { a: 1 } })).toEqual({});
    expect(rates({ t_us: 5, memory: { a: 1 } }, { t_us: 5, memory: { a: 2 } })).toEqual({});
    expect(rates({ t_us: 0, memory: {} }, { t_us: 1e6, memory: { a: 2 } })).toEqual({});
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
  it("値の並びで条件の回数と毎秒の速さを数える（変化は直前の値と比べる）", async () => {
    const { countCondition, parseCondition } = await import("../src/lib/system");
    const v = (t: number, state: string, restart: number) => ({ kind: "value", time_us: t, fields: { state, restart } });
    const items = [v(0, "IDLE", 0), v(100, "BUSY", 0), v(200, "BUSY", 1), v(300, "DONE", 1), v(400, "IDLE", 0),
                   { kind: "gap", time_us: 450 }, v(500, "BUSY", 1), v(1_000_000, "IDLE", 1)];
    const r = countCondition(items, parseCondition("restart changed and state=BUSY")!);
    expect(r.count).toBe(2);                                   // 200 と 500（450 の切れ目の後も直前の値と比べる）
    expect(r.perSecond).toBeCloseTo(2 / 1.0, 5);
    expect(countCondition([], parseCondition("state=BUSY")!)).toEqual({ count: 0, perSecond: null });
  });
  it("今の時刻を渡すと直近 2 秒で数える（スナップショット型は変化しか残さないので、値が止まったら 0）", async () => {
    const { countCondition, parseCondition } = await import("../src/lib/system");
    const v = (t: number, state: string) => ({ kind: "value", time_us: t, fields: { state } });
    const items = [v(0, "IDLE"), v(100_000, "BUSY"), v(200_000, "IDLE"), v(300_000, "BUSY"), v(1_000_000, "IDLE")];
    const terms = parseCondition("state changed and state=BUSY")!;
    expect(countCondition(items, terms).perSecond).toBeCloseTo(2, 5);
    expect(countCondition(items, terms, 1_000_000).perSecond).toBeCloseTo(2, 5);      // 窓が 2 秒より短ければその長さ
    expect(countCondition(items, terms, 2_200_000).perSecond).toBeCloseTo(0.5, 5);    // 0.2〜2.2 s: 0.3 s の 1 回
    expect(countCondition(items, terms, 4_000_000).perSecond).toBe(0);                // MCU が止まって 3 秒
    expect(countCondition([], terms, 9_000_000).perSecond).toBe(0);                   // 窓に値が無い = 変化なし
    expect(countCondition([], terms).perSecond).toBeNull();                           // 今の時刻が無ければ不明
    // 今の時刻（system.status）の後に値を読むので、窓の値が今より新しいこともある: 数えた回数を捨てない
    expect(countCondition([v(5_000_000, "IDLE"), v(5_100_000, "BUSY")], terms, 4_900_000).perSecond).toBeCloseTo(10, 5);
  });
});

describe("変化の無いピン", () => {
  it("記録の縮図の波形（ch ごとの画素の 0 / 1 / x）で、全体が同じ値の ch", async () => {
    const { flatChannels } = await import("../src/lib/system");
    expect(flatChannels(["1111", "0010", "0000", "00x0", "", "-11-", "----"])).toEqual([0, 2, 5]);
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

describe("記録のためのメモリの読みを引き継ぐ", () => {
  it("サンプラを使っている watch がこちらと同じ名前・番地の組なら自分のもの（並びは問わない）", async () => {
    const { ownWatch } = await import("../src/lib/system");
    const mine = [{ name: "誤り", address: 0x20040808 }, { name: "回数", address: 0x20040804 }];
    expect(ownWatch([{ name: "回数", address: "0x20040804" }, { name: "誤り", address: "0x20040808" }], mine)).toBe(true);
    expect(ownWatch([{ name: "counter", address: "0x20040804" }, { name: "誤り", address: "0x20040808" }], mine)).toBe(false);
    expect(ownWatch([{ name: "誤り", address: "0x20040808" }], mine)).toBe(false);
    expect(ownWatch([], [])).toBe(false);
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
    const { parseCondition, countCondition } = await import("../src/lib/system");
    expect(parseCondition("restart rose")).toEqual([{ field: "restart", op: "rose" }]);
    expect(parseCondition('"my flag" fell and state="WAIT ACK"')).toEqual(
      [{ field: "my flag", op: "fell" }, { field: "state", op: "=", value: "WAIT ACK" }]);
    const v = (t: number, restart: number) => ({ kind: "value", time_us: t, fields: { restart } });
    const items = [v(0, 0), v(10, 1), v(20, 0), v(30, 1), v(40, 1)];
    expect(countCondition(items, parseCondition("restart rose")!).count).toBe(2);
    expect(countCondition(items, parseCondition("restart fell")!).count).toBe(1);
  });
  it("FPGA の信号だけの条件か（条件の表に足せるか）", async () => {
    const { fpgaOnly } = await import("../src/lib/system");
    const names = ["state", "restart", "addr"];
    expect(fpgaOnly("restart changed and state=BUSY", names)).toBe(true);
    expect(fpgaOnly("誤り changed", names)).toBe(false);
    expect(fpgaOnly("in0 rose", names)).toBe(false);
    expect(fpgaOnly("fpga.restart rose", names)).toBe(true);
  });
  it("記録の MCU のメモリの読みの速さ: ピンも Ethernet で流すなら下げる（PYNQ の中継が詰まる）", async () => {
    const { memoryRateHz } = await import("../src/lib/system");
    expect(memoryRateHz(false, "eth")).toBe(1000);
    expect(memoryRateHz(true, "usb")).toBe(1000);
    expect(memoryRateHz(true, "eth")).toBe(200);
    expect(memoryRateHz(true, undefined)).toBe(1000);
  });
  it("回/秒は同じグループの直前の値と比べる（別のグループの値と比べない。欠落の後も直前の値と比べる）", async () => {
    const { countCondition, parseCondition } = await import("../src/lib/system");
    const terms = parseCondition("flag changed")!;
    const v = (t: number, group: number, fields: Record<string, number>) => ({ kind: "value", time_us: t, group, fields });
    const items = [v(0, 0, { flag: 0 }), v(10, 1, { other: 5 }), v(20, 0, { flag: 0 }), v(30, 1, { other: 6 }),
                   { kind: "gap", time_us: 35, group: 0 }, v(40, 0, { flag: 1 }), v(50, 0, { flag: 0 })];
    // 0→0（グループ 1 を挟んでも変化なし）、0→1（欠落をまたいで）、1→0 で 2 回
    expect(countCondition(items as any, terms).count).toBe(2);
  });
});

