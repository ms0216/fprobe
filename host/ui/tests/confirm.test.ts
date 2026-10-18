// 画面の中の確認（lib/confirm.ts）: ブラウザ標準の confirm の代わり。1 つずつ順に聞き、答えで約束を解く
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { Confirmer, type ConfirmRequest } from "../src/lib/confirm";
import { allowed } from "../src/lib/guard";

describe("Confirmer", () => {
  it("聞いている間は窓に出し、答えで約束を解く（はい・いいえ）", async () => {
    const c = new Confirmer();
    let shown: ConfirmRequest | null = null;
    c.subscribe((r) => (shown = r));
    expect(shown).toBeNull();
    const yes = c.ask("消しますか？", { danger: true, ok: "消去" });
    expect(shown).toMatchObject({ message: "消しますか？", danger: true, ok: "消去" });
    c.answer(true);
    expect(await yes).toBe(true);
    expect(shown).toBeNull();
    const no = c.ask("続けますか？");
    c.answer(false);
    expect(await no).toBe(false);
  });

  it("重なった問いは 1 つずつ順に出す", async () => {
    const c = new Confirmer();
    const seen: (string | null)[] = [];
    c.subscribe((r) => seen.push(r?.message ?? null));
    const a = c.ask("A");
    const b = c.ask("B");
    expect(seen.at(-1)).toBe("A");
    c.answer(false);
    expect(await a).toBe(false);
    expect(seen.at(-1)).toBe("B");
    c.answer(true);
    expect(await b).toBe(true);
    expect(seen.at(-1)).toBeNull();
  });
});

describe("allowed（AI の作業中の確認）", () => {
  it("AI が作業していなければ聞かない。作業中なら画面の中で聞く", async () => {
    const c = new Confirmer();
    expect(await allowed({ aiWorking: false } as any, c)).toBe(true);
    let shown: ConfirmRequest | null = null;
    c.subscribe((r) => (shown = r));
    const asked = allowed({ aiWorking: true } as any, c);
    expect(shown).not.toBeNull();
    c.answer(true);
    expect(await asked).toBe(true);
  });
});

describe("画面の部品の確認の使い方", () => {
  const SRC = resolve(import.meta.dirname, "../src");
  const files = (dir: string): string[] => readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : /\.(svelte|ts)$/.test(name) ? [path] : [];
  });
  const lines = files(SRC).flatMap((path) => readFileSync(path, "utf8").split("\n").map((line) => ({ path, line })))
    .filter(({ line }) => !/^\s*(\/\/|\*)/.test(line));

  it("ブラウザ標準の confirm を使わない（画面の中の確認 confirmer.ask）", () => {
    expect(lines.filter(({ line }) => /(^|[^.\w])confirm\(/.test(line)).map(({ path, line }) => `${path}: ${line.trim()}`))
      .toEqual([]);
  });

  it("allowed は必ず await する（約束のままだと確認せずに進む）", () => {
    expect(lines.filter(({ line }) => /(^|[^\w])allowed\(/.test(line) && !/await allowed\(|function allowed\(/.test(line))
      .map(({ path, line }) => `${path}: ${line.trim()}`)).toEqual([]);
  });
});
