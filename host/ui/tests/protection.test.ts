// 保護（lib/protection.ts）: オプションバイトの欄から、変えた値だけを target.option_bytes.write の fields に
import { describe, expect, it } from "vitest";
import { changedFields } from "../src/lib/protection";

describe("changedFields", () => {
  const read = { nWRP: 255, IWDG_SW: 1, RDP: 170 };

  it("変えた欄だけを数で（10 進・0x）", () => {
    expect(changedFields(read, { nWRP: "0xF0", IWDG_SW: "1", RDP: "170" })).toEqual({ fields: { nWRP: 0xf0 } });
    expect(changedFields(read, { nWRP: "255", IWDG_SW: "0", RDP: "" })).toEqual({ fields: { IWDG_SW: 0 } });
  });

  it("何も変えていなければ・数でない欄は送らない", () => {
    expect(changedFields(read, { nWRP: "255", IWDG_SW: "1", RDP: "170" })).toEqual({ error: "nothing" });
    expect(changedFields(read, { nWRP: "x" })).toEqual({ error: "nWRP" });
  });
});
