import { describe, expect, it } from "vitest";
import { hex, hex8 } from "../src/lib/hexfmt";

describe("hexfmt", () => {
  it("0x + 大文字・32 ビットの符号なし", () => {
    expect(hex(0)).toBe("0x0");
    expect(hex(0x1a2b)).toBe("0x1A2B");
    expect(hex(-1)).toBe("0xFFFFFFFF");
  });
  it("hex8 は 8 桁に揃える", () => {
    expect(hex8(0x1a2b)).toBe("0x00001A2B");
    expect(hex8(0x20000000)).toBe("0x20000000");
  });
});
