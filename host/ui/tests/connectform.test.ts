// 接続の窓の最初の値（仕様の target.connect）と、前に使ったターゲット名の一覧
import { describe, expect, it } from "vitest";
import { suggestedOf } from "../src/lib/apispec";
import { connectDefaults, recentTargets, rememberTarget, RECENT_TARGETS } from "../src/lib/connectform";

describe("connectform", () => {
  it("最初の値は仕様の値（ターゲット名と MHz）", () => {
    expect(connectDefaults(suggestedOf)).toEqual({ target: "cortex_m", mhz: "4" });
    expect(connectDefaults(() => undefined)).toEqual({ target: "", mhz: "" });
  });
  it("前に使ったターゲット名は新しい順・重複なし・上限つき", () => {
    let list = recentTargets("not json");
    expect(list).toEqual([]);
    list = rememberTarget(list, "stm32f401xe");
    list = rememberTarget(list, "rp2040");
    list = rememberTarget(list, " stm32f401xe ");
    expect(list).toEqual(["stm32f401xe", "rp2040"]);
    for (let i = 0; i < 20; i++) list = rememberTarget(list, `t${i}`);
    expect(list.length).toBe(RECENT_TARGETS);
    expect(recentTargets(JSON.stringify(["a", 3, "", "b"]))).toEqual(["a", "b"]);
  });
});
