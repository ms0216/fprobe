// テストポイント（docs/superpowers/specs/2026-11-02-ui-default-panels-design.md §4.1）: testpoint.start の欄（仕様の
// fieldsOf を方式ごとに出し分ける。出し分けは見せ方だけで、値・既定は API のまま）と、status・report・calls・coverage の
// 読み。分布・異常・切った数・欠けは API が返したまま（画面は数えない・並べ替えない）。Svelte に依らない
export { readQuiet } from "./api";
import { buildParams, type Field, type FieldError, type FieldValues } from "./apiform";
import { methodFields } from "./crosstrigger";
import type { Reveal } from "./timeline/state";

/** 方式ごとに出す欄（どの方式でも出す欄は COMMON）。仕様の引数はすべてどこかの組に入る（試験で確かめる） */
const GPIO = ["capture", "rate_hz", "rle", "trigger", "after_beats"];
export const METHOD_FIELDS: Record<string, readonly string[]> = {
  gpio_serial: ["clock_channel", "data_channel", "guard_samples", ...GPIO],
  gpio_parallel: ["data_channels", "strobe_channel", ...GPIO],
  itm: ["port", "swo_baud", "core_clock_hz"],
  rtt: ["rtt_channel", "core_clock_hz"],
};
export const COMMON_FIELDS: readonly string[] = ["map_path", "duration_s", "bucket_s", "silence_s", "open_s", "long_s",
                                                 "tree_max_depth"];

/** 方式の選択肢（仕様の method の列挙のまま） */
export function testpointMethods(): string[] {
  return (methodFields("testpoint.start").find((f) => f.name === "method")?.options ?? []).map(String);
}

/** その方式の欄（仕様の順）。表に無い方式（仕様に足された方式）は全部の欄を出す（隠さない） */
export function startFields(method: string): Field[] {
  const all = methodFields("testpoint.start").filter((f) => f.name !== "method");
  const own = METHOD_FIELDS[method];
  if (!own) return all;
  const shown = new Set([...own, ...COMMON_FIELDS]);
  return all.filter((f) => shown.has(f.name));
}

/** testpoint.start の引数。空の欄は送らない（既定はデーモン）。方式が無ければ欄の誤り */
export function startCall(method: string, values: FieldValues):
  { params: Record<string, unknown> } | { errors: Record<string, FieldError> } {
  if (!method) return { errors: { method: { code: "required" } } };
  const built = buildParams(startFields(method), values);
  return "errors" in built ? built : { params: { method, ...built.params } };
}

/** 状態の行（testpoint.status）に出す欄: API の名前と値のまま。まだ一度も始めていない（{running: false} だけ）は null */
export function statusItems(s: any): [string, string][] | null {
  if (!s || (s.running === false && s.method === undefined)) return null;
  const out: [string, string][] = [];
  const add = (k: string, v: unknown) => { if (v !== undefined && v !== null) out.push([k, String(v)]); };
  add("method", s.method);
  add("hits", s.hits);
  add("losses", s.losses);
  add("elapsed_s", typeof s.elapsed_s === "number" ? s.elapsed_s.toFixed(1) : s.elapsed_s);
  add("recorded", s.recorded);
  add("capture", s.capture);
  add("duration_s", s.duration_s);
  if (s.stream) add("stream.overrun", s.stream.overrun);
  add("cyccnt_wraps", s.cyccnt_wraps);
  add("unsynced_symbols", s.unsynced_symbols);
  add("skipped_before_sync", s.skipped_before_sync);
  return out;
}

/** 切った一覧の事実（表示した数・総数・上限）。切っていなければ null */
export interface Cut { shown: number; total: number; limit: number }

export function cut(shown: number, total: number | undefined, limit: number | undefined): Cut | null {
  return total !== undefined && total > shown ? { shown, total, limit: limit ?? shown } : null;
}

/** 時刻の欄の飛ぶ先: API が記録のタイムベース（t）を返したときだけ（t_s だけなら飛ばない） */
export function revealOf(item: { t?: number | null }): Reveal | null {
  return typeof item.t === "number" ? { source: "recording", t: item.t } : null;
}

/** 「通った / 全部」（API の [hit, total] の組のまま） */
export function pairText(p: [number, number] | undefined): string {
  return p ? `${p[0]} / ${p[1]}` : "—";
}

/** testpoint.calls の引数（仕様の欄。空は送らない） */
export function callsCall(values: FieldValues) {
  return buildParams(methodFields("testpoint.calls"), values);
}

/** testpoint.coverage の引数（仕様の欄。空は送らない） */
export function coverageCall(values: FieldValues) {
  return buildParams(methodFields("testpoint.coverage"), values);
}
