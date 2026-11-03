// パネル・メニューを名前で探す検索: 画面が持つ名前（メニューの名前・項目名・View の中身の説明）に対する、語の一致だけ。
// 並べ替え・得点・語ごとの案内は持たない（結果はメニューの並びのまま）
import { describe, expect, it } from "vitest";
import { PANELS } from "../src/components/panels/registry";
import { panelMenuLabel } from "../src/lib/dock";
import { t } from "../src/lib/i18n";
import { matchEntries } from "../src/lib/menusearch";

const viewEntries = PANELS.map((p) => ({ menu: t("menu.view"), label: panelMenuLabel(p, t), id: p.id }));
const entries = [...viewEntries,
  { menu: t("menu.debug"), label: "Step Over", id: "stepOver" }, { menu: t("menu.debug"), label: "Step Into", id: "stepInto" },
  { menu: t("menu.target"), label: "Download", id: "download" }];

describe("パネル・メニューを探す", () => {
  it("中身の説明に含まれる語（RTT）で、Terminal が見つかる", () => {
    expect(matchEntries(entries, "rtt").map((e) => e.id)).toContain("terminal");
    expect(matchEntries(entries, "semihosting").map((e) => e.id)).toContain("terminal");
  });

  it("大文字小文字は区別せず、語はすべて含むもの（空白で区切った AND）", () => {
    expect(matchEntries(entries, "STEP").map((e) => e.id)).toEqual(["stepOver", "stepInto"]);
    expect(matchEntries(entries, "step into").map((e) => e.id)).toEqual(["stepInto"]);
    expect(matchEntries(entries, "debug step over").map((e) => e.id)).toEqual(["stepOver"]);          // メニューの名前も対象
  });

  it("結果の並びはメニューの並びのまま（得点で並べ替えない）", () => {
    const ids = matchEntries(entries, "e").map((e) => e.id);
    expect(ids).toEqual(entries.filter((e) => ids.includes(e.id)).map((e) => e.id));
  });

  it("空の入力は何も返さない・一致しなければ空", () => {
    expect(matchEntries(entries, "  ")).toEqual([]);
    expect(matchEntries(entries, "zzzz-no-such")).toEqual([]);
  });

  it("特定の語を別のパネルに結びつけない: 名前に無い語は見つからない", () => {
    expect(matchEntries([{ menu: "View", label: "Source" }], "rtt")).toEqual([]);
  });
});
