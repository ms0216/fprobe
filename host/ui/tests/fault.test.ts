// Fault Analyzer の計算: 故障のハンドラの判定・故障で止まったら開く見張り・結果の整理
import { describe, expect, it } from "vitest";
import { headline, isFault, reportText, watchFaults, type FaultReport } from "../src/lib/fault";
import { FakeConn } from "./fake-conn";

const FAULT: FaultReport = {
  architecture: "ARMv7-M / v8-M Mainline",
  exception: { number: 3, name: "HardFault" },
  faulted: true,
  causes: [{ bit: "FORCED", register: "HFSR", meaning: "格上げ" },
           { bit: "PRECISERR", register: "BFSR", meaning: "バスエラー", hint: "BFAR" },
           { bit: "BFARVALID", register: "BFSR", meaning: "BFAR に番地" }],
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
  it("止まるたびに target.fault で確かめ、故障の例外（IPSR）の中なら開く。関数の名前では決めない。同じ止まりでは 1 回", async () => {
    let report: FaultReport = { ...FAULT };
    const conn = new FakeConn({ "target.fault": () => report });
    const app = source();
    let opened = 0;
    const tick = async () => { await Promise.resolve(); await Promise.resolve(); };
    const stop = watchFaults(conn.call.bind(conn) as any, app, () => opened++);
    app.push({ state: "HALTED", pc: 0x100, run: 1, function: "my_fault_hook" });       // 名前は関係ない
    await tick();
    expect(opened).toBe(1);
    app.push({ state: "HALTED", pc: 0x100, run: 1, function: "my_fault_hook" });       // 同じ止まり
    await tick();
    expect(opened).toBe(1);
    // 故障のビットは残っている（CFSR は消すまで残る）が、今は故障の例外の中ではない: 開かない
    report = { ...FAULT, faulted: true, exception: null };
    app.push({ state: "HALTED", pc: 0x200, run: 2, function: "HardFault_Handler" });
    await tick();
    expect(opened).toBe(1);
    report = { ...FAULT, faulted: false, exception: { number: 15, name: "SysTick" } };      // 割り込みで止まった
    app.push({ state: "HALTED", pc: 0xb8, run: 3, function: "Default_Handler" });
    await tick();
    expect(opened).toBe(1);
    expect(conn.quiet.every((m: string) => m === "target.fault")).toBe(true);
    stop();
  });

  it("結果の整理: 故障か、見出し、原因のレジスタ、コピー用の文字", () => {
    expect(isFault(FAULT)).toBe(true);
    expect(isFault({ ...FAULT, faulted: false, exception: null, causes: [] })).toBe(false);
    expect(isFault({ ...FAULT, faulted: false, causes: [] })).toBe(true);           // v6-M: HardFault の中
    // 見出しは API の立っているビットを全部、API の順で（画面が「主な原因」を選ばない）
    expect(headline(FAULT)).toBe("HardFault: FORCED, PRECISERR, BFARVALID");
    expect(headline({ ...FAULT, faulted: false, exception: null })).toBeNull();
    const text = reportText(FAULT);
    expect(text).toContain("PC: 0x2000004C reached_milestone+12 (sram_demo.c:13)");
    expect(text).toContain("LR: 0x200000B4");
    expect(text).toContain("Fault address: 0x20018000");
    expect(text).toContain("r1=0x20018000");
    expect(text).toContain("BFAR=0x20018000");
    expect(text).not.toContain("MMFAR");
  });
});
