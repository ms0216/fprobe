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
    expect(await coreClock(conn(0) as any, "stm32f401xe")).toBeNull();                               // まだ初期化前
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
