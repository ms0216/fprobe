// Fault Analyzer の計算: 故障のハンドラの判定・故障で止まったら開く見張り・結果の整理
import { describe, expect, it } from "vitest";
import { causeRegister, handlerKind, headline, isFault, reportText, watchFaults, type FaultReport } from "../src/lib/fault";
import { FakeConn } from "./fake-conn";

const FAULT: FaultReport = {
  architecture: "ARMv7-M / v8-M Mainline",
  exception: { number: 3, name: "HardFault" },
  faulted: true,
  causes: [{ bit: "FORCED", meaning: "格上げ" }, { bit: "PRECISERR", meaning: "バスエラー", hint: "BFAR" },
           { bit: "BFARVALID", meaning: "BFAR に番地" }],
  registers: { CFSR: 0x8200, HFSR: 0x40000000, DFSR: 8, MMFAR: null, BFAR: 0x20018000, CPACR: 0xf00000 },
  fault_address: { address: 0x20018000, where: "0x20018000" },
  frame: { stack: "MSP", sp: 0x20017fc8, exc_return: 0xfffffff9, fp_context: false, caller_sp: 0x20017fe8,
           registers: { r0: 0x300, r1: 0x20018000, r2: 0xff, r3: 0, r12: 0x2a, lr: 0x200000b5, pc: 0x2000004c, xpsr: 0x61000000 },
           pc: { address: 0x2000004c, where: "reached_milestone+12 (sram_demo.c:13)" },
           lr: { address: 0x200000b4, where: "reset_handler+64 (sram_demo.c:28)" } },
};

/** AppState の代わり: 状態を流す */
function source() {
  let fn: (s: { target: any }) => void = () => {};
  return { subscribe: (f: typeof fn) => ((fn = f), () => {}), push: (target: any) => fn({ target }) };
}

describe("Fault Analyzer", () => {
  it("故障のハンドラの名前（CMSIS・pico-sdk・小文字の流儀）と、共通のハンドラ（確かめが要る）", () => {
    for (const n of ["HardFault_Handler", "MemManage_Handler", "BusFault_Handler", "UsageFault_Handler",
                     "SecureFault_Handler", "isr_hardfault", "hard_fault_handler"]) expect(handlerKind(n), n).toBe("fault");
    for (const n of ["Default_Handler", "default_handler", "isr_invalid", "UnhandledInterrupt"]) expect(handlerKind(n), n).toBe("maybe");
    for (const n of ["main", "tick", "SysTick_Handler", undefined, null]) expect(handlerKind(n)).toBeNull();
  });

  it("故障のハンドラで止まったらすぐ開き、共通のハンドラなら target.fault で確かめる。同じ止まりでは 1 回", async () => {
    let report: FaultReport = { ...FAULT };
    const conn = new FakeConn({ "target.fault": () => report });
    const app = source();
    let opened = 0;
    const stop = watchFaults(conn.call.bind(conn) as any, app, () => opened++);
    app.push({ state: "HALTED", pc: 0x100, run: 1, function: "HardFault_Handler" });
    expect(opened).toBe(1);
    app.push({ state: "HALTED", pc: 0x100, run: 1, function: "HardFault_Handler" });     // 同じ止まり
    expect(opened).toBe(1);
    app.push({ state: "HALTED", pc: 0x200, run: 2, function: "tick" });
    expect(opened).toBe(1);
    expect(conn.calls).toEqual([]);
    app.push({ state: "HALTED", pc: 0xb8, run: 3, function: "default_handler" });
    await Promise.resolve();
    await Promise.resolve();
    expect(opened).toBe(2);
    expect(conn.quiet).toEqual(["target.fault"]);
    report = { ...FAULT, faulted: false, exception: { number: 15, name: "SysTick" } };      // 割り込みで止まった
    app.push({ state: "RUNNING" });
    app.push({ state: "HALTED", pc: 0xb8, run: 4, function: "default_handler" });
    await Promise.resolve();
    await Promise.resolve();
    expect(opened).toBe(2);
    stop();
  });

  it("結果の整理: 故障か、見出し、原因のレジスタ、コピー用の文字", () => {
    expect(isFault(FAULT)).toBe(true);
    expect(isFault({ ...FAULT, faulted: false, exception: null, causes: [] })).toBe(false);
    expect(isFault({ ...FAULT, faulted: false, causes: [] })).toBe(true);           // v6-M: HardFault の中
    expect(headline(FAULT)).toBe("HardFault: PRECISERR");
    expect(headline({ ...FAULT, faulted: false, exception: null })).toBeNull();
    expect([causeRegister("FORCED"), causeRegister("DACCVIOL"), causeRegister("PRECISERR"), causeRegister("NOCP"),
            causeRegister("x")]).toEqual(["HFSR", "MMFSR", "BFSR", "UFSR", ""]);
    const text = reportText(FAULT);
    expect(text).toContain("PC: 0x2000004C reached_milestone+12 (sram_demo.c:13)");
    expect(text).toContain("LR: 0x200000B4");
    expect(text).toContain("Fault address: 0x20018000");
    expect(text).toContain("r1=0x20018000");
    expect(text).toContain("BFAR=0x20018000");
    expect(text).not.toContain("MMFAR");
  });
});
