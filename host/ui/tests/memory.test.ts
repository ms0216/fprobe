// Memory のパネルの計算: 番地の式・書く値
import { describe, expect, it } from "vitest";
import { parseCell, resolveAddress, rowBytes, textEdit, unitHex } from "../src/lib/memory";
import { FakeConn } from "./fake-conn";

function conn() {
  return new FakeConn({
    "debug.evaluate": ({ expression }: any) => {
      const table: Record<string, any> = {
        counter: { type: "uint32_t", address: 0x200000d4, value: 42 },
        active: { type: "struct Sensor *", address: 0x20000480, value: { pointer: 0x20000420, to: "struct Sensor" } },
        "0x20000000 + 16": { type: "int", value: 0x20000010 },
        dead: { type: "uint8_t[4]", address: 0x20000600, value: [1, 2, 3, 4] },
      };
      if (!(expression in table)) throw new Error(`シンボル ${expression} が無い`);
      return table[expression];
    },
    "debug.symbol.info": ({ location }: any) => {
      if (location === "main") return { address: 0x08000100 };
      throw new Error("無い");
    },
  });
}

describe("Memory", () => {
  it("番地の式: 数は 16 進、変数はその番地、ポインタは指す先、整数の式はその値、関数名はシンボル", async () => {
    const c = conn();
    const call = c.call.bind(c) as any;
    expect(await resolveAddress(call, "0x20000100")).toBe(0x20000100);
    expect(await resolveAddress(call, "20000100")).toBe(0x20000100);
    expect(await resolveAddress(call, "counter")).toBe(0x200000d4);
    expect(await resolveAddress(call, "active")).toBe(0x20000420);
    expect(await resolveAddress(call, "0x20000000 + 16")).toBe(0x20000010);
    expect(await resolveAddress(call, "main")).toBe(0x08000100);
    expect(await resolveAddress(call, "dead")).toBe(0x20000600);        // 16 進にも読めるが、変数があれば変数
    expect(await resolveAddress(call, "beef")).toBe(0xbeef);            // 変数もシンボルも無ければ 16 進
    await expect(resolveAddress(call, "nope")).rejects.toThrow();
    expect(c.calls.find(([m]) => m === "debug.evaluate")?.[1]).toEqual({ expression: "counter", frame: 0 });
  });

  it("書く値は単位の幅の 16 進、バイト列はリトルエンディアン", () => {
    expect(parseCell("ff", 8)).toBe(255);
    expect(parseCell("100", 8)).toBeNull();
    expect(parseCell("0x1234", 16)).toBe(0x1234);
    expect(parseCell("zz", 8)).toBeNull();
    expect(parseCell("DEADBEEF", 32)).toBe(0xdeadbeef);
    expect(unitHex(0xab, 8)).toBe("ab");
    expect(unitHex(0x1234, 16)).toBe("3412");
    expect(unitHex(0xdeadbeef, 32)).toBe("efbeadde");
  });

  it("文字の列の編集: 変わった文字のバイトだけを書き、\".\" のまま残した所は元のバイト", () => {
    expect(rowBytes([0x6c6c6548, 0x0000006f], 32)).toEqual([0x48, 0x65, 0x6c, 0x6c, 0x6f, 0, 0, 0]);
    expect(rowBytes([0x4241], 16)).toEqual([0x41, 0x42]);
    const bytes = [0x48, 0x65, 0x6c, 0x6c, 0x6f, 0x00, 0x01, 0x02];
    expect(textEdit(bytes, "Hello...", "Hello...")).toBeNull();
    expect(textEdit(bytes, "Hello...", "Jello...")).toEqual({ offset: 0, data_hex: "4a" });
    // 間の "."（0x00）は元のまま、変えた 2 か所の間をまとめて書く
    expect(textEdit(bytes, "Hello...", "Hello.!Z")).toEqual({ offset: 6, data_hex: "215a" });
    expect(textEdit(bytes, "Hello...", "HelLo!..")).toEqual({ offset: 3, data_hex: "4c6f21" });
    expect(() => textEdit(bytes, "Hello...", "Hellö...")).toThrow();
  });
});
