// 画面の文言の表（英語・日本語）。画面の文字列はすべてここに置き、コンポーネントは $i18n("キー") で引く。
// 英語は Ozone・Keil µVision・VS Code の実際の語、日本語は VS Code / Visual Studio の日本語版の標準の訳。
// 説明（ツールチップ・空の状態の説明 = *.tip / *.hint）も、英語の表は英語、日本語の表は日本語（日本語の文の中の
// ボタン・パネルの名前は、日本語の表示のその名前）。英語の表に日本語を置かない（試験が見つける）。
// 既定は英語。Settings で切り替え、localStorage（fprobe.lang）に覚える。Svelte に依らない（store の約束だけ）。
// 仕様: docs/superpowers/specs/2026-10-18-ui-layout-design.md §3
import { load, save } from "./prefs";
import { EN_CORE } from "./i18n-dict/en-core";
import { JA_CORE } from "./i18n-dict/ja-core";
import { EN_SAMPLING } from "./i18n-dict/en-sampling";
import { JA_SAMPLING } from "./i18n-dict/ja-sampling";
import { EN_RTOS } from "./i18n-dict/en-rtos";
import { JA_RTOS } from "./i18n-dict/ja-rtos";
import { EN_EVENTLOG } from "./i18n-dict/en-eventlog";
import { JA_EVENTLOG } from "./i18n-dict/ja-eventlog";
import { EN_FAULT } from "./i18n-dict/en-fault";
import { JA_FAULT } from "./i18n-dict/ja-fault";
import { EN_TERMINAL } from "./i18n-dict/en-terminal";
import { JA_TERMINAL } from "./i18n-dict/ja-terminal";
import { addListener } from "./store";

// 文言の表はパネルの群ごとに i18n-dict/ のファイルに分けてある（英語と日本語は同じ分け方・同じキー）。ここで束ねる。
export const EN = {
  ...EN_CORE,
  ...EN_SAMPLING,
  ...EN_RTOS,
  ...EN_EVENTLOG,
  ...EN_FAULT,
  ...EN_TERMINAL,
} as const;


export type Key = keyof typeof EN;

export const JA: Record<Key, string> = {
  ...JA_CORE,
  ...JA_SAMPLING,
  ...JA_RTOS,
  ...JA_EVENTLOG,
  ...JA_FAULT,
  ...JA_TERMINAL,
};

export type Lang = "en" | "ja";
export const LANGS: { id: Lang; label: string }[] = [{ id: "en", label: "English" }, { id: "ja", label: "日本語" }];
const TABLES: Record<Lang, Record<Key, string>> = { en: EN, ja: JA };

export type Translate = (key: Key, params?: Record<string, string | number>) => string;

function translator(lang: Lang): Translate {
  const table = TABLES[lang];
  return (key, params) => {
    const text = table[key] ?? EN[key] ?? key;
    return params ? text.replace(/\{(\w+)\}/g, (m, k) => (k in params ? String(params[k]) : m)) : text;
  };
}

let lang: Lang = load("lang", "en") === "ja" ? "ja" : "en";
let current = translator(lang);
const listeners = new Set<(t: Translate) => void>();

/** store: 値は文言を引く関数（$i18n("cmd.continue")）。言語を変えると新しい関数が届き、画面が描き直される */
export const i18n = {
  subscribe(fn: (t: Translate) => void): () => void {
    return addListener(listeners, fn, current);
  },
};

export function getLang(): Lang {
  return lang;
}

export function setLang(next: Lang): void {
  if (next === lang) return;
  lang = next;
  current = translator(lang);
  save("lang", lang);
  if (typeof document !== "undefined") document.documentElement.lang = lang;
  listeners.forEach((fn) => fn(current));
}

/** コンポーネントの外（コマンドの定義など）で引く */
export function t(key: Key, params?: Record<string, string | number>): string {
  return current(key, params);
}
