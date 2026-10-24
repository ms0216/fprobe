// 画面の欄に暗黙の既定の値を持たない（特定の対象の品種・番地・速さの値を初めから入れておかない）。
// 使う人が入れるまで空にし、要る欄は赤いアスタリスクで示す
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(import.meta.dirname, "../src");
const source = (path: string) => readFileSync(resolve(SRC, path), "utf8");

describe("対象の品種に偏った既定の値を欄に入れない（?9）", () => {
  it("Flash の消去の範囲は空から（STM32 の 0x08000000・16K を入れない）", () => {
    const s = source("components/panels/FlashPanel.svelte");
    expect(s).not.toMatch(/eraseAddress = \$state\("0x08000000"\)/);
    expect(s).not.toMatch(/eraseLength = \$state\("16K"\)/);
    expect(s).toMatch(/eraseAddress = \$state\(""\)/);
    expect(s).toMatch(/eraseLength = \$state\(""\)/);
  });
  it("Memory の番地は空から（Cortex-M の SRAM 0x20000000 を入れない）", () => {
    expect(source("components/panels/MemoryPanel.svelte")).toMatch(/load\("memory\.address", ""\)/);
  });
  it("Connect のターゲット名・SWD の速さは空から（stm32f401xe・4 MHz を入れない）", () => {
    const s = source("components/shell/Dialogs.svelte");
    expect(s).toMatch(/load\("connect\.target", ""\)/);
    expect(s).toMatch(/load\("connect\.mhz", ""\)/);
  });
});

describe("画面で率を作らない（?13）", () => {
  it("System の FPGA のスキャン: 2 回の読みの差から毎秒を計算せず（32 ビットの巻き戻りを仮定しない）、API の数をそのまま出す", () => {
    const s = source("components/panels/SystemPanel.svelte");
    expect(s).not.toMatch(/scansPerSecond/);
    expect(s).not.toMatch(/2 \*\* 32/);
  });
});

describe("文字の形で「名前が引けたか」を決めない（?20）", () => {
  it("Fault の番地の場所・RTOS Objects の名前を 0x で始まるかで出し分けない", () => {
    expect(source("components/panels/FaultPanel.svelte")).not.toMatch(/startsWith\("0x"\)/);
    expect(source("components/panels/RtosObjectsPanel.svelte")).not.toMatch(/startsWith\("0x"\)/);
  });
});

describe("観測・取得の条件を画面の暗黙の既定で決めない（3 回目の点検 H4・?1）", () => {
  it("Dashboard の Live は周期を画面の既定（100 Hz）で始めない: 観測が動いていなければ使う人が入れた周期", () => {
    const s = source("components/panels/DashboardPanel.svelte");
    expect(s).not.toMatch(/LIVE_RATE/);
    expect(s).toMatch(/load\("dashboard\.rate", ""\)/);
  });
  it("Data Sampling の周期・条件での取得の前後の時間は空から（1000 Hz・10 ms・500 ms を入れない）", () => {
    const s = source("components/panels/DataSamplingPanel.svelte");
    expect(s).toMatch(/load\("dataSampling\.rate", ""\)/);
    expect(s).not.toMatch(/\|\| 1000\)/);
    expect(s).not.toMatch(/preMs: 10/);
    expect(s).not.toMatch(/postMs: 500/);
  });
  it("Profile の周期は空から（10 kHz を入れない）", () => {
    expect(source("components/panels/ProfilePanel.svelte")).toMatch(/load\("profile\.rate", ""\)/);
  });
  it("CPU Load の周期・窓の幅は空から（20 kHz・10 ms を入れない）", () => {
    const s = source("components/panels/CpuLoadPanel.svelte");
    expect(s).toMatch(/load\("cpuLoad\.rate", ""\)/);
    expect(s).toMatch(/load\("cpuLoad\.window", ""\)/);
  });
  it("2 点の間のサイクルの回数・ETM の命令の数は空から（1 回・32 を入れない）", () => {
    expect(source("components/panels/SourcePanel.svelte")).toMatch(/load\("cycles\.count", ""\)/);
    expect(source("components/panels/FaultPanel.svelte")).toMatch(/load\("fault\.etmCount", ""\)/);
  });
  it("Logic Analyzer の番地一致は API の仕様の欄（マスク・値を画面で埋めない）、段の事象・回数は空から", () => {
    const s = source("components/panels/LogicAnalyzerPanel.svelte");
    expect(s).not.toMatch(/0xFFFFFFFF/);
    expect(s).toMatch(/methodFields\("xtrig\.set_match"\)/);
    expect(s).not.toMatch(/count: 1/);
  });
  it("System は RLE / RAW を画面で決めず（使う人が選んだ形式）、止めるまでの秒も空から（1 秒を入れない）", () => {
    const s = source("components/panels/SystemPanel.svelte");
    expect(s).not.toMatch(/rle: (true|false)/);
    expect(s).toMatch(/load\("system\.triggerPost", ""\)/);
  });
});

describe("時刻 0 を「無い」とみなさない（3 回目の点検 ?5）", () => {
  it("Cross Trigger・Logic Analyzer のレジスタの時刻は回数・fired で起きたかを決め、0 を捨てない", () => {
    expect(source("components/panels/CrossTriggerPanel.svelte")).not.toMatch(/t > 0/);
    expect(source("components/panels/LogicAnalyzerPanel.svelte")).not.toMatch(/t > 0/);
  });
});

describe("情報が無いときに値を仮定しない（3 回目の点検 ?11）", () => {
  it("Peripherals はレジスタの大きさを API が添えた値で（無ければ 32 ビットとみなさず、書かない）", () => {
    expect(source("components/panels/PeripheralsPanel.svelte")).not.toMatch(/\?\? 32/);
  });
});

describe("黙って捨てない（3 回目の点検 ?10）", () => {
  it("Dashboard のグラフの部品は捨てた点の数と上限を出す", () => {
    expect(source("components/panels/DashboardPanel.svelte")).toMatch(/dashboard\.discarded/);
  });
});
