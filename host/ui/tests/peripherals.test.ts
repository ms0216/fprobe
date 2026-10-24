// Peripherals: フィールドの定義 → 書く部品、入力 → 値
import { describe, expect, it } from "vitest";
import { fieldControl, parseField, parseRegister, svdLoadParams, type FieldDef } from "../src/lib/peripherals";

const field = (over: Partial<FieldDef>): FieldDef => ({ name: "F", bit_offset: 0, bit_width: 1, access: "read-write", enums: [], ...over });

describe("Peripherals", () => {
  it("1 ビットはチェックボックス、値の名前があれば選択、ほかは数、読むだけは書かない", () => {
    expect(fieldControl(field({}))).toBe("check");
    expect(fieldControl(field({ bit_width: 2, enums: [{ name: "Output", value: 1 }] }))).toBe("select");
    expect(fieldControl(field({ bit_width: 2, enums: [{ name: "Other", value: null }] }))).toBe("number");
    expect(fieldControl(field({ bit_width: 4 }))).toBe("number");
    expect(fieldControl(field({ access: "read-only" }))).toBe("readonly");
    expect(fieldControl(field({ access: "" }), { access: "read-only" } as any)).toBe("readonly");
    expect(fieldControl(undefined)).toBe("readonly");
  });

  it("フィールドの値は 10 進・0x・0b で幅に入るもの、レジスタは 16 進", () => {
    expect(parseField("3", 2)).toBe(3);
    expect(parseField("4", 2)).toBeNull();
    expect(parseField("0xF", 4)).toBe(15);
    expect(parseField("0b101", 3)).toBe(5);
    expect(parseField("x", 3)).toBeNull();
    expect(parseRegister("0x0000ABCD", 32)).toBe(0xabcd);
    expect(parseRegister("1FFFF", 16)).toBeNull();
  });
});

describe("Load SVD の引数", () => {
  it("デバイス名かパスかは使う人が選ぶ（文字の形で推さない）", () => {
    expect(svdLoadParams("path", " C:\\svd\\STM32F401.SVD ")).toEqual({ path: "C:\\svd\\STM32F401.SVD" });
    expect(svdLoadParams("device", "vendor/part")).toEqual({ device: "vendor/part" });   // / を含むデバイス名もデバイス
    expect(svdLoadParams("device", "STM32F401.svd")).toEqual({ device: "STM32F401.svd" });
    expect(svdLoadParams("path", "  ")).toBeNull();
  });
});
