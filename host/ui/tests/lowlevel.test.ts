// 低レベルの窓（lib/lowlevel.ts）: DP / AP / CSR / ピン / GPIO / 出力の端子を読んだ値の横で書く引数の組み立て
import { describe, expect, it } from "vitest";
import {
  apRegisterParams, apWriteCall, bitsOf, csrReadParams, csrWriteCall, DP_REGISTERS, dpWriteCall, gpioSetCall, optionsOf,
  outputForm, outputSetCall, pinsWriteCall,
} from "../src/lib/lowlevel";

describe("DP", () => {
  it("レジスタは API の名前。番地 0 は読むと DPIDR・書くと ABORT（ADI の定義）なので、読む行と書く行を分ける", () => {
    expect(DP_REGISTERS.map((r) => r.name)).toEqual(
      ["DPIDR", "ABORT", "CTRL/STAT", "SELECT", "RDBUFF", "TARGETID", "DLPIDR", "EVENTSTAT", "BASEPTR0"]);
    expect(DP_REGISTERS.find((r) => r.name === "DPIDR")).toMatchObject({ read: true, write: false });
    expect(DP_REGISTERS.find((r) => r.name === "ABORT")).toMatchObject({ read: false, write: true });
    expect(DP_REGISTERS.find((r) => r.name === "CTRL/STAT")).toMatchObject({ read: true, write: true });
  });

  it("書く値は 32 ビットの符号なし", () => {
    expect(dpWriteCall("ABORT", "0x1E")).toEqual({ method: "target.dp.write", params: { register: "ABORT", value: 0x1E } });
    expect(dpWriteCall("CTRL/STAT", "0x100000000")).toEqual({ error: "range" });
    expect(dpWriteCall("CTRL/STAT", "x")).toEqual({ error: "value" });
  });
});

describe("AP", () => {
  it("AP の番号とレジスタの番地の欄（0x も）。読めなければ誤りの種類", () => {
    expect(apRegisterParams("0", "0xFC")).toEqual({ ap: 0, register: 0xFC });
    expect(apRegisterParams("", "0")).toEqual({ error: "value" });
    expect(apRegisterParams("-1", "0")).toEqual({ error: "range" });
    expect(apWriteCall(1, 0x04, "0x20000000")).toEqual(
      { method: "target.ap.write", params: { ap: 1, register: 4, value: 0x20000000 } });
  });
});

describe("CSR", () => {
  it("番地と語の数の欄、1 語の書き込み", () => {
    expect(csrReadParams("0x40", "4")).toEqual({ address: 0x40, count: 4 });
    expect(csrReadParams("0x40", "0")).toEqual({ error: "range" });
    expect(csrReadParams("zz", "1")).toEqual({ error: "value" });
    expect(csrWriteCall(0x44, "7")).toEqual({ method: "probe.csr.write", params: { address: 0x44, words: [7] } });
  });
});

describe("ピンと GPIO", () => {
  it("ピンは押すと今の値の逆を書く", () => {
    expect(pinsWriteCall("nreset", 1)).toEqual({ method: "probe.pins.write", params: { nreset: 0 } });
    expect(pinsWriteCall("swclk", 0)).toEqual({ method: "probe.pins.write", params: { swclk: 1 } });
  });

  it("GPIO は 1 本だけ（mask）、出力の値か出力にするかの一方を反転し、もう一方は今の値のまま", () => {
    const state = { drive: 0b0000_0101, enable: 0b0000_0100 };
    expect(gpioSetCall(state, 2, "drive")).toEqual({ method: "gpio.set", params: { drive: 0b0000_0001, enable: 0b0000_0100, mask: 0b100 } });
    expect(gpioSetCall(state, 1, "enable")).toEqual({ method: "gpio.set", params: { drive: 0b0000_0101, enable: 0b0000_0110, mask: 0b10 } });
    expect(bitsOf(0b1010_0001)).toEqual([1, 0, 0, 0, 0, 1, 0, 1]);
  });
});

describe("出力の端子（xtrig.output_set）", () => {
  it("形・出す値・源の選択肢は API の仕様の列挙から", () => {
    expect(optionsOf("xtrig.output_set", "mode")).toEqual(["pulse", "hold", "while"]);
    expect(optionsOf("xtrig.output_set", "level")).toEqual(["high", "low"]);
    expect(optionsOf("xtrig.output_set", "sources")).toEqual(["la_trigger", "dap_match", "host", "sequence", "obs_match", "sampler_match"]);
    expect(optionsOf("xtrig.output_set", "nope")).toEqual([]);
  });

  it("欄の初めの値は、設定済みならその設定（xtrig.output_status）。設定が無ければ形・出す値も空（画面の既定を持たない）", () => {
    expect(outputForm({ channel: 4, configured: false })).toMatchObject({ channel: 4, mode: "", level: "", sources: [],
                                                                          mask: "", value: "", any: false, pulseUs: "", enable: false });
    expect(outputForm({ channel: 5, configured: true, enabled: true, mode: "pulse", level: "low", sources: ["host"],
                        pattern: { mask: 1, value: 1, any: false }, pulse_us: { min: 9.9, max: 10.1 } }))
      .toMatchObject({ mode: "pulse", level: "low", sources: ["host"], mask: "0x1", value: "0x1", enable: true, pulseUs: "" });
  });

  it("条件は源か端子の並び（どちらも無ければ誤り）。脈の長さは pulse のときだけ", () => {
    const empty = outputForm({ channel: 4, configured: false });
    expect(outputSetCall({ ...empty, sources: ["host"] })).toEqual({ error: "choice" });          // 形・出す値は選ぶ
    expect(outputSetCall({ ...empty, sources: ["host"], mode: "while" })).toEqual({ error: "choice" });
    const f = { ...empty, mode: "while", level: "high" };
    expect(outputSetCall({ ...f })).toEqual({ error: "condition" });
    expect(outputSetCall({ ...f, mask: "0x3", value: "" })).toEqual({ error: "value" });          // 空の値を 0 とみなさない
    expect(outputSetCall({ ...f, sources: ["host"], enable: true })).toEqual({ method: "xtrig.output_set",
      params: { channel: 4, mode: "while", level: "high", sources: ["host"], enable: true } });
    expect(outputSetCall({ ...f, mode: "pulse", mask: "0x3", value: "0x1", pulseUs: "10" })).toEqual({ method: "xtrig.output_set",
      params: { channel: 4, mode: "pulse", level: "high", pattern: { mask: 3, value: 1, any: false }, pulse_us: 10, enable: false } });
    expect(outputSetCall({ ...f, mask: "0x100", value: "0" })).toEqual({ error: "range" });
    expect(outputSetCall({ ...f, mode: "pulse", sources: ["host"], pulseUs: "abc" })).toEqual({ error: "value" });
  });
});
