import { describe, expect, it } from "vitest";
import { EN, JA } from "../src/lib/i18n";
import { EN_CORE } from "../src/lib/i18n-dict/en-core";
import { EN_EVENTLOG } from "../src/lib/i18n-dict/en-eventlog";
import { EN_FAULT } from "../src/lib/i18n-dict/en-fault";
import { EN_RTOS } from "../src/lib/i18n-dict/en-rtos";
import { EN_SAMPLING } from "../src/lib/i18n-dict/en-sampling";
import { EN_TERMINAL } from "../src/lib/i18n-dict/en-terminal";

describe("i18n-dict（文言の表の分割）", () => {
  const parts = [EN_CORE, EN_SAMPLING, EN_RTOS, EN_EVENTLOG, EN_FAULT, EN_TERMINAL];

  it("分けた表のキーは重ならない（束ねたときに黙って上書きされない）", () => {
    const total = parts.reduce((n, p) => n + Object.keys(p).length, 0);
    expect(Object.keys(EN).length).toBe(total);
  });

  it("英語と日本語は同じキーの組", () => {
    expect(Object.keys(JA).sort()).toEqual(Object.keys(EN).sort());
  });
});
