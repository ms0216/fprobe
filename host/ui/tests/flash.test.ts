// Flash のパネルの計算: 設定・引数・番地・表示・段の表・進捗
import { describe, expect, it } from "vitest";
import {
  DEFAULT_OPTIONS, algoParams, baseName, defaultReadPath, eraseMethodKey, formatOf, loadOptions, overall, parseNumber,
  programParams, programStages, remaining, sectorsText, seconds, size, speed, verifyParams,
} from "../src/lib/flash";

describe("Flash", () => {
  it("設定は覚えた値を読み、壊れていれば既定", () => {
    expect(loadOptions("{")).toEqual(DEFAULT_OPTIONS);
    expect(loadOptions(JSON.stringify({ erase: "chip", verify: false, smart: "x" })))
      .toEqual({ erase: "chip", verify: false, smart: true, reset: true });
  });

  it("形式は拡張子、番地は 16 進と K / M、BIN だけ番地を渡す（無ければ誤り）", () => {
    expect([formatOf("a.elf"), formatOf("C:\\x\\a.HEX"), formatOf("a.bin"), formatOf("a.axf"), formatOf("noext")])
      .toEqual(["elf", "hex", "bin", "elf", "elf"]);
    expect([parseNumber("0x08004000"), parseNumber("8004000"), parseNumber("16K"), parseNumber("1M"), parseNumber("x")])
      .toEqual([0x08004000, 0x8004000, 16384, 1 << 20, null]);
    expect(programParams("a.bin", "0x08000000", DEFAULT_OPTIONS)).toEqual(
      { path: "a.bin", address: 0x08000000, erase: "auto", verify: true, smart: true, reset: true });
    expect(programParams("a.elf", "0x08000000", { ...DEFAULT_OPTIONS, erase: "chip", reset: false })).toEqual(
      { path: "a.elf", erase: "chip", verify: true, smart: true, reset: false });
    expect(() => programParams("a.bin", "", DEFAULT_OPTIONS)).toThrow();
    expect(verifyParams("a.hex", "0x1")).toEqual({ path: "a.hex" });
    expect(() => verifyParams("a.bin", "zz")).toThrow();
  });

  it("大きさ・速さ・時間の表示", () => {
    expect([size(512), size(16384), size(1536), size(3 << 20), size(null)]).toEqual(["512 B", "16 KB", "1.5 KB", "3.0 MB", "-"]);
    expect([speed(43008), speed(2 << 20), speed(null)]).toEqual(["42.0 KB/s", "2.00 MB/s", "-"]);
    expect([seconds(0.0042), seconds(0.25), seconds(1.5)]).toEqual(["4.2 ms", "250 ms", "1.5 s"]);
  });

  it("書き込みの結果を段の表に（消去・書き込み・RAM・検証・全体）", () => {
    const result = {
      seconds: 0.62,
      program: { bytes: 16384, programmed_bytes: 8192, skipped_bytes: 8192, erase_method: "sector", ram_bytes: 0,
                 erase: { bytes: 16384, seconds: 0.17, bytes_per_s: 96000, sectors: 1 },
                 program: { bytes: 8192, seconds: 0.19, bytes_per_s: 42000 }, seconds: 0.4, bytes_per_s: 40960 },
      verify: { match: true, read: { bytes: 16384, seconds: 0.04, bytes_per_s: 380000 } },
    };
    expect(programStages(result)).toEqual([
      { stage: "erase", bytes: 16384, seconds: 0.17, bps: 96000, sectors: 1, method: "sector" },
      { stage: "program", bytes: 8192, seconds: 0.19, bps: 42000, skipped: 8192 },
      { stage: "verify", bytes: 16384, seconds: 0.04, bps: 380000 },
      { stage: "total", bytes: 16384, seconds: 0.62, bps: 40960 },
    ]);
    expect(programStages({ program: { ...result.program, ram_bytes: 206 }, seconds: 0.1 }).map((s) => s.stage))
      .toEqual(["erase", "program", "ram", "total"]);
    expect(programStages(null)).toEqual([]);
  });

  it("進捗: 書き込みは消去 → 書き込み → 検証を 1 本の帯に", () => {
    expect(overall({ operation: "program", phase: "erase", fraction: 0.5, elapsed_s: 0 })).toBeCloseTo(1 / 6);
    expect(overall({ operation: "program", phase: "verify", fraction: 1, elapsed_s: 0 })).toBe(1);
    expect(overall({ operation: "program", phase: "program", fraction: 1, elapsed_s: 0 }, false)).toBe(1);
    expect(overall({ operation: "read", phase: "read", fraction: 0.3, elapsed_s: 0 })).toBe(0.3);
  });

  it("セクタの並び・ファイルの名前・読み出しの保存先", () => {
    expect(sectorsText([{ start: 0, count: 4, size: 0x4000 }, { start: 0, count: 3, size: 0x20000 }]))
      .toBe("4 × 16 KB, 3 × 128 KB");
    expect(baseName("C:\\fw\\app.hex")).toBe("app.hex");
    expect(defaultReadPath("C:\\fw\\app.elf")).toBe("C:\\fw\\readback.hex");
    expect(defaultReadPath(null)).toBe("readback.hex");
  });

  it("残りの時間の見積もり: 経過 × (1 − 進み) / 進み（書き込みは段をつないだ進み）", () => {
    expect(remaining({ operation: "program", phase: "program", fraction: 0.5, elapsed_s: 3 })).toBeCloseTo(3);
    expect(remaining({ operation: "read", phase: "read", fraction: 0.25, elapsed_s: 1 })).toBeCloseTo(3);
    expect(remaining({ operation: "read", phase: "read", fraction: 0, elapsed_s: 1 })).toBeNull();
    expect(remaining({ operation: "erase", phase: "erase", fraction: 1, elapsed_s: 2 })).toBe(0);
  });

  it("外付けのアルゴリズム（.FLM）の引数: 空の欄は省き（FLM の値）、数は 16 進と K / M、誤りは欄の名前で投げる", () => {
    expect(algoParams({ flm: " C:/flm/W25Q.FLM ", address: "0x90000000", size: "16M", ramAddress: "", ramSize: "", name: "" }))
      .toEqual({ flm: "C:/flm/W25Q.FLM", address: 0x90000000, size: 16 << 20 });
    expect(algoParams({ flm: "a.FLM", address: "", size: "", ramAddress: "20000000", ramSize: "8K", name: " qspi " }))
      .toEqual({ flm: "a.FLM", ram_address: 0x20000000, ram_size: 8192, name: "qspi" });
    expect(() => algoParams({ flm: " ", address: "", size: "", ramAddress: "", ramSize: "", name: "" })).toThrow("flm");
    expect(() => algoParams({ flm: "a.FLM", address: "zz", size: "", ramAddress: "", ramSize: "", name: "" }))
      .toThrow("address");
  });

  it("消去の方法の名前（文言のキー）", () => {
    expect([eraseMethodKey("sector"), eraseMethodKey("chip"), eraseMethodKey("mass"), eraseMethodKey("x")])
      .toEqual(["flash.eraseSector", "flash.eraseChip", "flash.eraseMass", null]);
  });
});
