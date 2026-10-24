// FPGA 観測のパネルの計算: 列挙の書き方・ビットの範囲・定義の検査（デーモンの fprobe.fpga_signals と同じ規則）・
// TCK と半周期の換算・状態の文言
import { describe, expect, it } from "vitest";
import {
  editableGroups, formatBits, formatEnum, OBS_REGISTERS, parseBits, parseEnum, registerWriteCall, signalsChanged, statusKeys,
  tckToHalfPeriod,
  type SignalDef,
} from "../src/lib/fpgaobs";

const sig = (name: string, bits: string, format: SignalDef["format"] = "hex", enumText = ""): SignalDef =>
  ({ name, ...parseBits(bits)!, format, enum: enumText ? parseEnum(enumText)! : {} });

describe("列挙の書き方", () => {
  it("「0=IDLE, 1=RUN, 0x5=ERR」を値 → 名前に（10 進・16 進）", () => {
    expect(parseEnum("0=IDLE, 1=RUN, 0x5=ERR")).toEqual({ "0": "IDLE", "1": "RUN", "5": "ERR" });
    expect(parseEnum("")).toEqual({});
    expect(formatEnum({ "1": "RUN", "0": "IDLE" })).toBe("0=IDLE, 1=RUN");
  });
  it("形の崩れは null（画面がその場で知らせる）", () => {
    expect(parseEnum("0=IDLE, RUN")).toBeNull();
    expect(parseEnum("x=IDLE")).toBeNull();
    expect(parseEnum("0=")).toBeNull();
  });
});

describe("ビットの範囲", () => {
  it("「7:4」「3」と lsb・幅の行き来", () => {
    expect(parseBits("7:4")).toEqual({ lsb: 4, width: 4 });
    expect(parseBits("3")).toEqual({ lsb: 3, width: 1 });
    expect(parseBits("4:7")).toBeNull();
    expect(parseBits("a")).toBeNull();
    expect(formatBits(4, 4)).toBe("7:4");
    expect(formatBits(3, 1)).toBe("3");
  });
});

describe("TCK と半周期", () => {
  it("sys 100 MHz: 10 MHz → 半周期 5、12.5 MHz → 4、上限は半周期 2（25 MHz）・下限 65535", () => {
    expect(tckToHalfPeriod(10, 100e6)).toEqual({ half: 5, mhz: 10 });
    expect(tckToHalfPeriod(12.5, 100e6)).toEqual({ half: 4, mhz: 12.5 });
    // 欄が空・0 以下なら無し（NaN の半周期を接続に渡さない。R17）
    for (const bad of [NaN, 0, -1, null as unknown as number]) expect(tckToHalfPeriod(bad, 100e6)).toBeNull();
    expect(tckToHalfPeriod(30, 100e6)).toEqual({ half: 2, mhz: 25 });
    expect(tckToHalfPeriod(0.0001, 100e6)!.half).toBe(65535);
    expect(tckToHalfPeriod(7, 100e6)).toEqual({ half: 8, mhz: 6.25 });          // 求めた速さを超えない
  });
});

describe("状態の文言", () => {
  it("答えない・溢れ・動いている・始めている・止まっている: 立っている事実を全部（優先順で 1 つに畳まない）", () => {
    expect(statusKeys({ running: false, stuck: true, id_ok: false, overflow_seen: false, driving: false }))
      .toEqual(["fpgaObs.state.stuck"]);
    expect(statusKeys({ running: true, stuck: false, id_ok: true, overflow_seen: true, driving: true }))
      .toEqual(["fpgaObs.state.overflow", "fpgaObs.state.running"]);
    expect(statusKeys({ running: true, stuck: false, id_ok: true, overflow_seen: false, driving: true }))
      .toEqual(["fpgaObs.state.running"]);
    expect(statusKeys({ running: false, stuck: false, id_ok: false, overflow_seen: false, driving: false }))
      .toEqual(["fpgaObs.state.off"]);
    // 答えなくなったときも、それまでの溢れを落とさない
    expect(statusKeys({ running: false, stuck: true, id_ok: false, overflow_seen: true, driving: true }))
      .toEqual(["fpgaObs.state.stuck", "fpgaObs.state.overflow", "fpgaObs.state.starting"]);
  });
});

describe("信号の表で編集する組（観測する組とは別）", () => {
  it("定義のある組・申告の組の数・いま選んでいる組の和（観測につながっていなくても過去の記録に名前を付けられる）", () => {
    expect(editableGroups(["2", "0"], null, 0)).toEqual([0, 2]);
    expect(editableGroups([], 3, 0)).toEqual([0, 1, 2]);
    expect(editableGroups(["5"], 2, 7)).toEqual([0, 1, 5, 7]);
  });
});

describe("定義の共有（別の窓・AI の変更を読み直す）", () => {
  it("画面の状態の通知で fpga_signals が変わったときだけ", () => {
    expect(signalsChanged({ changed: ["fpga_signals"] })).toBe(true);
    expect(signalsChanged({ changed: ["view", "cursors"] })).toBe(false);
    expect(signalsChanged({})).toBe(false);
  });
});

describe("観測の回路の型の設定", () => {
  it("記録型は速い TCK と多い語数、標本型（1 クロック版）は遅い TCK・1 語・印なし", async () => {
    const { PRESETS } = await import("../src/lib/fpgaobs");
    expect(PRESETS.stream).toEqual({ tckMhz: 10, words: 64, syncPeriod: 20 });
    expect(PRESETS.snapshot).toEqual({ tckMhz: 2.5, words: 1, syncPeriod: 0 });
  });
});

describe("開いた設定を画面の欄へ", () => {
  it("デーモンが覚えている設定（fpga_obs.status の config）を欄の値にする（TCK は半周期から）", async () => {
    const { configToFields, tckToHalfPeriod } = await import("../src/lib/fpgaobs");
    const f = configToFields({ chain: { ir_len: 6, ir_before: 4, dr_after: 1 }, user_ir: 0x22, half_period: 20, words: 1,
                               sync_period: 0 }, 100e6);
    expect(f).toEqual({ irLen: 6, irBefore: 4, irAfter: null, drBefore: null, drAfter: 1, userIr: "0x22", tckMhz: 2.5,
                        halfPeriod: 20, words: 1, syncPeriod: 0, idScans: null });
    for (let half = 2; half <= 200; half++) {                            // 欄から開き直すと同じ半周期（丸めの誤差でずれない）
      const back = configToFields({ chain: {}, user_ir: 2, half_period: half, words: 1, sync_period: 0 }, 100e6);
      expect(tckToHalfPeriod(back.tckMhz!, 100e6)!.half).toBe(half);
    }
    // 開いた設定に chain が無い: 欄は空（4 等の値を仮定して見せない）
    expect(configToFields({ chain: null, user_ir: 2, half_period: 3, words: 64, sync_period: 20 }, 100e6).irLen).toBeNull();
    // プローブがシステムのクロックを申告しない: TCK は分からない（null）。半周期はそのまま
    const unknown = configToFields({ chain: null, user_ir: 2, half_period: 3, words: 64, sync_period: 20 }, null);
    expect([unknown.tckMhz, unknown.halfPeriod]).toEqual([null, 3]);
  });
});

describe("fpga_obs.open の引数", () => {
  const base = { irLen: null, irBefore: null, irAfter: null, drBefore: null, drAfter: null, userIr: "0x2",
                 tckMhz: 10, halfPeriod: null, words: 64, syncPeriod: 20, idScans: null, autoReopen: false };
  it("クロックの申告があれば TCK から半周期。無ければ仮定せず、半周期を直接入れたときだけ", async () => {
    const { openParams } = await import("../src/lib/fpgaobs");
    expect(openParams(base, 100e6)).toEqual({ chain: null, user_ir: 2, half_period: 5, words: 64, sync_period: 20,
                                               auto_reopen: false });
    expect(openParams(base, null)).toBeNull();                                // 100 MHz 等を仮定しない
    expect(openParams({ ...base, halfPeriod: 7 }, null)!.half_period).toBe(7);
    expect(openParams({ ...base, halfPeriod: 1 }, null)).toBeNull();          // 範囲の外
  });
  it("チェーンは入れた欄だけ（全部空なら null）。User IR が読めなければ null", async () => {
    const { openParams } = await import("../src/lib/fpgaobs");
    expect(openParams({ ...base, irLen: 6, drAfter: 1 }, 100e6)!.chain).toEqual({ ir_len: 6, dr_after: 1 });
    expect(openParams({ ...base, userIr: "x" }, 100e6)).toBeNull();
  });
  it("語数・印の周期・User IR・TCK は使う人が入れる（空なら開かない。画面の既定を持たない）", async () => {
    const { openParams } = await import("../src/lib/fpgaobs");
    expect(openParams({ ...base, words: null }, 100e6)).toBeNull();
    expect(openParams({ ...base, syncPeriod: null }, 100e6)).toBeNull();
    expect(openParams({ ...base, userIr: "" }, 100e6)).toBeNull();
    expect(openParams({ ...base, tckMhz: null }, 100e6)).toBeNull();
  });
});

describe("観測の回路のレジスタ（CONTROL・SELECT）を書く", () => {
  it("番地は API の説明の 14 CONTROL・15 SELECT。値は 16 ビットに収まる整数", () => {
    expect(OBS_REGISTERS).toEqual([{ name: "CONTROL", addr: 14 }, { name: "SELECT", addr: 15 }]);
    expect(registerWriteCall(15, "0x2")).toEqual({ method: "fpga_obs.register_write", params: { addr: 15, value: 2 } });
    expect(registerWriteCall(14, "70000")).toEqual({ error: "range" });
    expect(registerWriteCall(14, "-1")).toEqual({ error: "range" });
    expect(registerWriteCall(14, "on")).toEqual({ error: "value" });
  });
});

describe("ID の応答を待つ上限（使う側が渡す）", () => {
  it("入れたときだけ id_scans を送り、開いた設定から欄に写す", async () => {
    const { configToFields, openParams } = await import("../src/lib/fpgaobs");
    const f = { irLen: null, irBefore: null, irAfter: null, drBefore: null, drAfter: null, userIr: "0x2", tckMhz: 10,
                halfPeriod: null, words: 64, syncPeriod: 20, autoReopen: false };
    expect(openParams({ ...f, idScans: null }, 100e6)).not.toHaveProperty("id_scans");
    expect(openParams({ ...f, idScans: 16 }, 100e6)).toMatchObject({ id_scans: 16 });
    expect(configToFields({ chain: null, user_ir: 2, half_period: 3, words: 64, sync_period: 20, id_scans: 8 }, null).idScans)
      .toBe(8);
  });
});
