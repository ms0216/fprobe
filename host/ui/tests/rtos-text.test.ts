// RTOS のタスクの状態・待っているもの・オブジェクトを待つタスクの文言（lib/rtos.ts）: 日本語では英語の語を出さない
import { afterEach, describe, expect, it } from "vitest";
import { setLang, t } from "../src/lib/i18n";
import { taskStateText, waitText, waiterText } from "../src/lib/rtos";

afterEach(() => setLang("en"));

describe("RTOS の文言", () => {
  it("タスクの状態は訳す（知らない状態はそのまま）", () => {
    expect(["running", "ready", "blocked", "suspended", "deleted"].map((s) => taskStateText(s, t)))
      .toEqual(["Running", "Ready", "Blocked", "Suspended", "Deleted"]);
    setLang("ja");
    expect(["running", "ready", "blocked", "suspended", "deleted"].map((s) => taskStateText(s, t)))
      .toEqual(["実行中", "実行可能", "待ち", "停止", "削除待ち"]);
    expect(taskStateText("dormant", t)).toBe("dormant");
  });

  it("待っているもの: 理由・名前・持ち主・残りのティック", () => {
    const w = { reason: "receive", name: "rxQueue", holder: "net", remaining_ticks: 12 };
    expect(waitText(w, t)).toBe("Receive rxQueue (held by net) 12 ticks left");
    expect(waitText({ reason: "delay", remaining_ticks: 0 }, t)).toBe("Delay 0 ticks left");
    expect(waitText(null, t)).toBe("");
    setLang("ja");
    expect(waitText(w, t)).toBe("受信待ち rxQueue （所有者 net） 残り 12 ティック");
    expect(["delay", "send", "event", "notification", "suspended", "sleep", "pend", "bits"]
      .map((reason) => waitText({ reason }, t)))
      .toEqual(["時間待ち", "送信待ち", "イベント待ち", "通知待ち", "停止中", "スリープ", "待ち行列", "ビット待ち"]);
    expect(waitText({ reason: "custom" }, t)).toBe("custom");
  });

  it("オブジェクトを待つタスク: 名前・優先度・理由", () => {
    expect(waiterText({ task: "rx", priority: 3, reason: "receive" }, t)).toBe("rx (3) Receive");
    expect(waiterText({ task: "rx", priority: null, reason: "pend" }, t)).toBe("rx Pend");
    setLang("ja");
    expect(waiterText({ task: "rx", priority: 3, reason: "bits" }, t)).toBe("rx (3) ビット待ち");
  });
});
