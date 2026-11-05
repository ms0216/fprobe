// クロストリガーの画面の見出しの対応表（源・行き先・列・状態の記号）。文言のキーと API の名前を結ぶだけの表で、
// Svelte に依らない（CrossTriggerPanel とその子が使う）。
import type { Key } from "./i18n";
import type { Action, RouteColumn, TriggerSource } from "./crosstrigger";

export const SOURCE_LABEL: Record<TriggerSource, Key> = {
  la_trigger: "logic.srcTrigger", dap_match: "logic.dapMatch", host: "logic.host", sequence: "logic.srcSequence",
  obs_match: "logic.srcObsMatch", sampler_match: "logic.srcSamplerMatch",
};
export const SOURCE_API: Record<TriggerSource, string> = {
  la_trigger: "la.arm", dap_match: "xtrig.set_match", host: "xtrig.fire", sequence: "xtrig.set_sequence",
  obs_match: "fpga_obs.trigger_set", sampler_match: "sampler.trigger_set",
};
export const ACTION_LABEL: Record<Action, Key> = { la: "xt.actLa", inject: "xt.actInject", outputs: "xt.actOutputs" };
export const COLUMN_ACTION: Record<RouteColumn, Action> = { to_la: "la", to_inject: "inject" };
export const ROUTE_COLUMNS: RouteColumn[] = ["to_la", "to_inject"];
export const EVENT_SHORT: Record<string, string> = { pattern_a: "A", pattern_b: "B", dap_match: "Match", host: "Host" };
export const STATE_MARK: Record<string, string> = { done: "✓", current: "●", waiting: "○", idle: "–" };

/** 無いものの理由に出す、ケイパビリティの名前と値 */
export const capText = (a: { cap: string; value: unknown }): string =>
  `${a.cap} = ${a.value === undefined ? "—" : JSON.stringify(a.value)}`;

/** 32 ビットの 16 進（0x 付き・8 桁） */
export const hex32 = (n: number): string => `0x${(n >>> 0).toString(16).toUpperCase().padStart(8, "0")}`;
