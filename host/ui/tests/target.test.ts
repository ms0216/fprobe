// つないでいるターゲット（lib/target.ts）: ステータス バーの線・速さ・名前と、コア クロックの既定
import { describe, expect, it } from "vitest";
import { coreClock, linkOf, typicalCoreClockMhz } from "../src/lib/target";

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
  it("ターゲット名からいつもの値（MHz）", () => {
    expect(typicalCoreClockMhz("nrf52840")).toBe(64);
    expect(typicalCoreClockMhz("nrf52832")).toBe(64);
    expect(typicalCoreClockMhz("stm32f401xe")).toBe(84);
    expect(typicalCoreClockMhz("stm32f411ce")).toBe(100);
    expect(typicalCoreClockMhz("rp2040")).toBe(125);
    expect(typicalCoreClockMhz("rp2350")).toBe(150);
    expect(typicalCoreClockMhz("cortex_m")).toBeNull();
    expect(typicalCoreClockMhz(undefined)).toBeNull();
  });

  it("ELF に SystemCoreClock（CMSIS）があればその値、無ければターゲット名から", async () => {
    const conn = (value: unknown) => ({
      call: async (method: string, params: any) => {
        expect(method).toBe("debug.variable.read");
        expect(params).toEqual({ name: "SystemCoreClock" });
        if (value instanceof Error) throw value;
        return { name: "SystemCoreClock", value };
      },
    });
    expect(await coreClock(conn(48_000_000) as any, "nrf52840")).toEqual({ mhz: 48, source: "SystemCoreClock" });
    expect(await coreClock(conn(new Error("無い")) as any, "nrf52840")).toEqual({ mhz: 64, source: "target" });
    expect(await coreClock(conn(0) as any, "stm32f401xe")).toEqual({ mhz: 84, source: "target" });   // まだ初期化前
    expect(await coreClock(conn(new Error("無い")) as any, "cortex_m")).toBeNull();
  });
});
