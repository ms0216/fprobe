// 文言の表: 英語と日本語のキーがそろい、画面の部品に表の外の文字列（日本語）を直書きしていない
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { EN, JA, getLang, i18n, setLang, t } from "../src/lib/i18n";

const SRC = resolve(import.meta.dirname, "../src");
const CJK = /[぀-ヿ㐀-鿿]/;

function svelteFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? svelteFiles(path) : name.endsWith(".svelte") ? [path] : [];
  });
}

/** コメントと <style> を除いた部分（画面に出る文字列とスクリプトの文字列） */
function visible(text: string): string {
  return text.replace(/<style[\s\S]*?<\/style>/g, "").replace(/<!--[\s\S]*?-->/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'])\/\/.*$/gm, "$1");
}

describe("文言の表", () => {
  it("英語と日本語のキーが同じで、空の訳が無い", () => {
    expect(Object.keys(JA).sort()).toEqual(Object.keys(EN).sort());
    for (const [k, v] of Object.entries(JA)) expect(v, k).not.toBe("");
  });

  it("英語の表に日本語の値が無い（ツールチップ・空の状態の説明も英語）", () => {
    expect(Object.entries(EN).filter(([, v]) => CJK.test(v)).map(([k]) => k)).toEqual([]);
  });

  it("日本語の文に、日本語の表示では別の名前のボタン・パネルの英語の名前を残さない", () => {
    // 日本語の表示の名前が英語と違うパネル・コマンド・メニューと、文の中で名指しされるボタンの英語の名前
    const buttons = ["Pause AI", "Measure", "Edit", "Live", "Stop", "Start", "Hit Count", "Go", "Refresh", "Clear",
                     "Capture", "Decode", "Program", "Verify", "Settings"];
    const names = [...Object.entries(EN).filter(([k, v]) => /^(panel|cmd|menu)\./.test(k) && JA[k as keyof typeof JA] !== v
                                                    && /^[A-Z]/.test(v)).map(([, v]) => v.replace(/…$/, "")), ...buttons]
      .filter((n) => n !== "Flash");             // Flash はメモリの名前（フラッシュのパネルの名前としてではない）
    const quoted = (n: string) => new RegExp(`(^|[^A-Za-z_.])${n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}([^A-Za-z_]|$)`);
    const offenders = Object.entries(JA).filter(([, v]) => CJK.test(v))
      .flatMap(([k, v]) => names.filter((n) => quoted(n).test(v)).map((n) => `${k}: ${n}: ${v}`));
    expect(offenders).toEqual([]);
  });

  it("主な語は競合ツールの語（英語）と VS Code の日本語版の訳", () => {
    expect([EN["cmd.continue"], EN["cmd.halt"], EN["cmd.stepOver"], EN["panel.callStack"], EN["panel.watch"],
            EN["panel.registers"], EN["panel.peripherals"]])
      .toEqual(["Continue", "Halt", "Step Over", "Call Stack", "Watch", "Registers", "Peripherals"]);
    expect([JA["cmd.continue"], JA["cmd.halt"], JA["cmd.stepOver"], JA["panel.callStack"], JA["panel.watch"],
            JA["panel.registers"]]).toEqual(["続行", "中断", "ステップ オーバー", "呼び出し履歴", "ウォッチ", "レジスタ"]);
  });

  it("既定は英語。切り替えると store が新しい訳を配り、{名前} を埋める", () => {
    expect(getLang()).toBe("en");
    let seen = "";
    const stop = i18n.subscribe((tr) => (seen = tr("cmd.continue")));
    setLang("ja");
    expect(seen).toBe("続行");
    expect(t("status.held", { n: 3 })).toBe("保留 3");
    setLang("en");
    expect(seen).toBe("Continue");
    stop();
  });

  it("画面の部品（.svelte）に日本語の文字列を直書きしていない（コメントは除く）", () => {
    const offenders = svelteFiles(SRC).flatMap((path) =>
      visible(readFileSync(path, "utf8")).split("\n").filter((line) => CJK.test(line)).map((line) => `${path}: ${line.trim()}`));
    expect(offenders).toEqual([]);
  });
});
