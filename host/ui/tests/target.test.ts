// つないでいるターゲット（lib/target.ts）: ステータス バーの線・速さ・名前と、コア クロックの既定
import { describe, expect, it } from "vitest";
import { coreClock, haltReasonKey, linkOf } from "../src/lib/target";

describe("linkOf", () => {
  it("ターゲットの状態から線・速さ・名前（切れていれば出さない）", () => {
    expect(linkOf({ state: "RUNNING", target: "nrf52840", frequency_hz: 4e6, wire: "swd" }))
      .toEqual({ wire: "SWD", speed: 4e6, cpu: "nrf52840" });
    expect(linkOf({ state: "HALTED", target: "cortex_a", frequency_hz: 1e6, wire: "jtag", core: "Cortex-A7" }))
      .toEqual({ wire: "JTAG", speed: 1e6, cpu: "Cortex-A7 · cortex_a" });
    expect(linkOf({ state: "DISCONNECTED" })).toBeNull();
    expect(linkOf(null)).toBeNull();
    expect(linkOf({ state: "RUNNING" })).toEqual({ wire: "", speed: null, cpu: "" });     // 古いデーモン
  });
});

describe("コア クロックの既定", () => {
  it("SystemCoreClock は読んだ値をそのまま示す（妥当な範囲という境界で捨てない。使うかは利用者が欄で確かめる）", async () => {
    const conn = (value: unknown) => ({ call: async () => ({ name: "SystemCoreClock", value }) });
    expect(await coreClock(conn(500) as any)).toEqual({ mhz: 0.0005, source: "SystemCoreClock" });
    expect(await coreClock(conn(3_000_000_000) as any)).toEqual({ mhz: 3000, source: "SystemCoreClock" });
    expect(await coreClock(conn(12_345_678) as any)).toEqual({ mhz: 12.345678, source: "SystemCoreClock" });   // 丸めない
    expect(await coreClock(conn(0) as any)).toEqual({ mhz: 0, source: "SystemCoreClock" });                   // 初期化前の 0 も読んだ値
    expect(await coreClock(conn("x") as any)).toBeNull();                                                     // 数でない
  });

  it("ELF に SystemCoreClock（CMSIS）があればターゲットのその値。無ければ null（品種名から典型の値を推測しない）", async () => {
    const conn = (value: unknown) => ({
      call: async (method: string, params: any) => {
        expect(method).toBe("debug.variable.read");
        expect(params).toEqual({ name: "SystemCoreClock" });
        if (value instanceof Error) throw value;
        return { name: "SystemCoreClock", value };
      },
    });
    expect(await coreClock(conn(48_000_000) as any, "nrf52840")).toEqual({ mhz: 48, source: "SystemCoreClock" });
    expect(await coreClock(conn(new Error("無い")) as any, "nrf52840")).toBeNull();
    expect(await coreClock(conn(new Error("無い")) as any, "cortex_m")).toBeNull();
  });
});

describe("haltReasonKey", () => {
  it("止まった理由（target.status の halt_reason）を文言のキーに。知らない理由・停止中でなければ null", () => {
    expect(haltReasonKey({ state: "HALTED", halt_reason: "bkpt_instruction" })).toBe("halt.bkpt_instruction");
    expect(haltReasonKey({ state: "HALTED", halt_reason: "request" })).toBe("halt.request");
    expect(haltReasonKey({ state: "HALTED", halt_reason: "lockup" })).toBeNull();
    expect(haltReasonKey({ state: "RUNNING", halt_reason: "breakpoint" })).toBeNull();
    expect(haltReasonKey(null)).toBeNull();
  });
});
