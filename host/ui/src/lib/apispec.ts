// API の仕様（host/api/fprobe_api.json）から、画面が呼べるメソッドの一覧（MCP の道具と同じ範囲）。build 時に読み込む
import spec from "../../../api/fprobe_api.json";
import { apiMethods, type ApiSpec } from "./apiform";

export const METHODS = apiMethods(spec as unknown as ApiSpec);
const NAMES = new Set(METHODS.map((m) => m.name));

/** 仕様の引数の「例・推奨の値」（JSON Schema の examples の先頭）。無ければ undefined。画面の入力欄の最初の値の出どころ
 *  （見えて・変えられ・明示して送る。MCP の AI にも同じ値が見える） */
export function exampleOf(method: string, param: string): unknown {
  const props = (spec as unknown as { methods: Record<string, { params?: { properties?: Record<string, { examples?: unknown[] }> } }> })
    .methods[method]?.params?.properties;
  return props?.[param]?.examples?.[0];
}

/** 入力欄の最初の値の出どころ: 仕様の examples の先頭、無ければ default（null でないもの）。無ければ undefined。値は欄に
 *  見えて、書き換えられ、送るときは欄の値を明示して送る（画面のコードに値を書かない。MCP の AI にも同じ値が仕様で見える） */
export function suggestedOf(method: string, param: string): unknown {
  const p = (spec as unknown as { methods: Record<string, { params?: { properties?: Record<string, { examples?: unknown[]; default?: unknown }> } }> })
    .methods[method]?.params?.properties?.[param];
  return p?.examples?.[0] ?? (p?.default === null ? undefined : p?.default);
}

/** API パネルで開けるメソッドか（session.* 等は開けない） */
export const inApiPanel = (method: string): boolean => NAMES.has(method);
