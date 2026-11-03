// View メニューの項目名: パネルの名前だけでは中身が分からないもの（Terminal = RTT・SWO・UART・セミホスティング）は、中身を括弧で添える
import { describe, expect, it } from "vitest";
import { PANELS } from "../src/components/panels/registry";
import { panelMenuLabel } from "../src/lib/dock";
import { EN, JA, t } from "../src/lib/i18n";

describe("View メニューの項目名", () => {
  const byId = (id: string) => PANELS.find((p) => p.id === id)!;

  it("Terminal は中身（RTT・SWO・UART・Semihosting）を添える", () => {
    const label = panelMenuLabel(byId("terminal"), t);
    expect(label).toBe(`${t("panel.terminal")} (${t("panel.terminal.about")})`);
    expect(EN["panel.terminal.about"]).toBe("RTT · SWO · UART · Semihosting");
    for (const word of ["RTT", "SWO", "UART", "Semihosting"]) expect(EN["panel.terminal.about"]).toContain(word);
  });

  it("中身の説明を持たないパネルは、名前のまま", () => {
    expect(panelMenuLabel(byId("source"), t)).toBe(t("panel.source"));
  });

  it("説明のキーは英語・日本語の表にある（空でない）", () => {
    for (const p of PANELS.filter((x) => x.about)) {
      expect(EN[p.about!], p.id).toBeTruthy();
      expect(JA[p.about!], p.id).toBeTruthy();
    }
  });
});
