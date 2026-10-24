// 窓を浮かせる・別の窓に出す（lib/dockwindows.ts）: タブの右クリックのメニューと、見出しのボタンが呼ぶもの
import { describe, expect, it } from "vitest";
import { POPOUT_URL, popOut, tabMenu, floatOut } from "../src/lib/dockwindows";

function fakeApi() {
  const calls: any[] = [];
  return { calls, api: { addFloatingGroup: (p: any) => calls.push(["float", p]),
                         addPopoutGroup: (p: any, o: any) => (calls.push(["popout", p, o]), Promise.resolve(true)) } };
}

describe("窓を外に出す", () => {
  it("浮かせる・別の窓に出す（同じ origin の popout.html）", async () => {
    const { calls, api } = fakeApi();
    const panel = { id: "timeline", api: { close: () => calls.push(["close"]) } };
    floatOut(api as any, panel as any);
    await popOut(api as any, panel as any);
    expect(calls).toEqual([["float", panel], ["popout", panel, { popoutUrl: POPOUT_URL }]]);
  });

  it("タブの右クリックのメニュー: 浮かせる・別の窓に出す・閉じる（文言は翻訳から）", () => {
    const { calls, api } = fakeApi();
    const panel = { id: "watch", api: { close: () => calls.push(["close"]) } };
    const items = tabMenu(api as any, panel as any, (k: string) => `[${k}]`) as any[];
    expect(items.filter((i) => typeof i === "object").map((i) => i.label))
      .toEqual(["[dock.float]", "[dock.popout]", "[dock.close]"]);
    items.filter((i) => typeof i === "object").forEach((i) => i.action());
    expect(calls.map((c) => c[0])).toEqual(["float", "popout", "close"]);
  });
});

describe("窓の場所ごとの見出しのボタン", async () => {
  const { actionsFor, popoutTitle } = await import("../src/lib/dockwindows");
  it("画面の中では両方、浮いていれば別の窓だけ、別の窓の中では出さない", () => {
    expect(actionsFor("grid")).toEqual({ float: true, popout: true });
    expect(actionsFor("floating")).toEqual({ float: false, popout: true });
    expect(actionsFor("popout")).toEqual({ float: false, popout: false });
  });
  it("別の窓の題名は前に出ている窓の名前", () => {
    expect(popoutTitle("呼び出し履歴")).toBe("呼び出し履歴 — fprobe");
    expect(popoutTitle(undefined)).toBe("fprobe");
  });
});
