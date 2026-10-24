// クロストリガの窓の計算（源 × 行き先を 1 か所で。docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §3.3 の作る順の 2 番目）:
// - 構成（probe.info の capabilities）で使える源・行き先と、使えない理由（どのケイパビリティがどの値か）
// - 経路の表: xtrig.status の route（レジスタの事実）↔ xtrig.route の引数。出力の端子の源: xtrig.output_status ↔ xtrig.output_set
// - 源ごとの回数・時刻: 機器のレジスタの事実だけ（回路にレジスタが無いものは unreadable。画面で数えない・推さない。
//   操作の記録の最後の設定を「今の設定」として出さない）
// - 設定の読み戻し（xtrig.status の match・inject、la.trigger_status）を欄へ入れる
// - 条件の欄: API の仕様（fprobe_api.json）の引数そのまま（人の操作と AI の操作は一対一）
// Svelte に依らない。
import { buildParams, fieldsOf, type Field, type FieldError, type FieldValues } from "./apiform";
import { METHODS } from "./apispec";
import { SOURCES, type SequenceStatus, type TriggerSource } from "./logic";

export { SOURCES, type TriggerSource };

// --- 構成 ---------------------------------------------------------------------------------------

/** 行き先: ロジアナの取り込み（経路 to_la）・割り込み書き込み（経路 to_inject。既定は CPU の停止）・出力の端子（xtrig.output_*） */
export const ACTIONS = ["la", "inject", "outputs"] as const;
export type Action = (typeof ACTIONS)[number];

/** 使えるか。使えなければ、決めたケイパビリティの名前と申告された値（申告に無ければ undefined） */
export type Avail = { ok: true } | { ok: false; cap: string; value: unknown };

type Caps = Record<string, unknown> | null | undefined;

const YES: Avail = { ok: true };

/** ケイパビリティ cap が true のときだけ使える（申告に無い = 無い。暗黙に有るとしない） */
function flag(caps: Caps, cap: string): Avail {
  const value = caps?.[cap];
  return value === true ? YES : { ok: false, cap, value };
}

/** 数のケイパビリティが 1 以上のときだけ使える */
function positive(caps: Caps, cap: string): Avail {
  const value = caps?.[cap];
  return typeof value === "number" && value > 0 ? YES : { ok: false, cap, value };
}

/** クロストリガが無ければ全部その理由で無い */
function withXtrig(caps: Caps, each: () => Avail): Avail {
  const x = flag(caps, "xtrig");
  return x.ok ? each() : x;
}

export function sourceAvailability(caps: Caps): Record<TriggerSource, Avail> {
  const rule: Record<TriggerSource, () => Avail> = {
    la_trigger: () => positive(caps, "la_channels"),
    dap_match: () => YES,
    host: () => YES,
    sequence: () => flag(caps, "trig_seq"),
    obs_match: () => flag(caps, "obs_match"),
    sampler_match: () => flag(caps, "sampler_match"),
  };
  return Object.fromEntries(SOURCES.map((s) => [s, withXtrig(caps, rule[s])])) as Record<TriggerSource, Avail>;
}

export function actionAvailability(caps: Caps): Record<Action, Avail> {
  const rule: Record<Action, () => Avail> = {
    la: () => positive(caps, "la_channels"),
    inject: () => flag(caps, "inject"),
    outputs: () => positive(caps, "output_actions"),
  };
  return Object.fromEntries(ACTIONS.map((a) => [a, withXtrig(caps, rule[a])])) as Record<Action, Avail>;
}

// --- 経路の表（xtrig.status の route ↔ xtrig.route） -----------------------------------------------

export type RouteColumn = "to_la" | "to_inject";
export interface Route { to_la: TriggerSource[]; to_inject: TriggerSource[] }

const isSource = (s: unknown): s is TriggerSource => (SOURCES as readonly unknown[]).includes(s);

/** xtrig.status の route（レジスタから読んだ源の名前）。この画面の知らない名前は unknown に分けて見せる（黙って落とさない）。
 *  route を返さないデーモンなら null */
export function routeOf(status: XtrigStatus | null | undefined):
  (Route & { unknown: string[] }) | null {
  const r = status?.route;
  if (!r) return null;
  const all = [...(r.to_la ?? []), ...(r.to_inject ?? [])];
  return { to_la: (r.to_la ?? []).filter(isSource), to_inject: (r.to_inject ?? []).filter(isSource),
           unknown: [...new Set(all.filter((s) => !isSource(s)))] };
}

const ordered = (list: readonly TriggerSource[]) => SOURCES.filter((s) => list.includes(s));

export function toggleRoute(r: Route, column: RouteColumn, s: TriggerSource): Route {
  const list = r[column].includes(s) ? r[column].filter((x) => x !== s) : [...r[column], s];
  return { ...r, [column]: ordered(list) };
}

/** xtrig.route の引数。指定しない行き先は切れるので、両方を必ず送る */
export function routeParams(r: Route): Route {
  return { to_la: ordered(r.to_la), to_inject: ordered(r.to_inject) };
}

/** 書き換えたセル（事実と違う） */
export function routeChanged(facts: Route | null, draft: Route, column: RouteColumn, s: TriggerSource): boolean {
  return facts !== null && facts[column].includes(s) !== draft[column].includes(s);
}

/** 触れないセルの理由: 源が無い・行き先が無い・ロジアナのトリガ → ロジアナ（自分自身） */
export type Block = { kind: "self" } | { kind: "source" | "action"; cap: string; value: unknown };

export function cellBlock(s: TriggerSource, column: RouteColumn, sources: Record<TriggerSource, Avail>,
                          actions: Record<Action, Avail>): Block | null {
  if (s === "la_trigger" && column === "to_la") return { kind: "self" };
  const src = sources[s];
  if (!src.ok) return { kind: "source", cap: src.cap, value: src.value };
  const act = actions[column === "to_la" ? "la" : "inject"];
  if (!act.ok) return { kind: "action", cap: act.cap, value: act.value };
  return null;
}

// --- 出力の端子（xtrig.output_status ↔ xtrig.output_set） ---------------------------------------------

/** xtrig.output_status の outputs の 1 つ（そのまま） */
export interface OutputPin {
  channel: number;
  pull_up: boolean;
  configured: boolean;
  enabled?: boolean;
  active?: boolean;
  claim?: boolean;
  mode?: string;
  level?: string;
  pattern?: { mask: number; value: number; any: boolean };
  sources?: string[];
  pulse_us?: { min: number; max: number };
}

export interface OutputColumn { channel: number; sources: TriggerSource[]; editable: boolean; reason?: "pulse" }

/** 表の列: 設定済みの端子だけ。脈（pulse）は長さが刻みに丸めた範囲でしか読めず、同じ長さで設定し直せないので表では変えない */
export function outputColumns(pins: OutputPin[] | null | undefined): OutputColumn[] {
  return (pins ?? []).filter((p) => p.configured).map((p) => ({
    channel: p.channel, sources: (p.sources ?? []).filter(isSource),
    ...(p.mode === "pulse" ? { editable: false, reason: "pulse" as const } : { editable: true }),
  }));
}

export type Call = { method: string; params: Record<string, unknown> };

/** 出力の端子の源を変える: 読んだ今の形・値・端子の並びの条件・有効をそのまま、源だけ替えて xtrig.output_set */
export function outputSourcesCall(pin: OutputPin, sources: TriggerSource[]):
  Call | { error: "notConfigured" | "pulse" | "empty" } {
  if (!pin.configured) return { error: "notConfigured" };
  if (pin.mode === "pulse") return { error: "pulse" };
  const pattern = pin.pattern && pin.pattern.mask ? { ...pin.pattern } : null;
  if (!pattern && !sources.length) return { error: "empty" };
  return {
    method: "xtrig.output_set",
    params: { channel: pin.channel, mode: pin.mode, level: pin.level, ...(pattern ? { pattern } : {}),
              ...(sources.length ? { sources: ordered(sources) } : {}), enable: !!pin.enabled },
  };
}

// --- 回数・時刻（API の事実） --------------------------------------------------------------------

export interface XtrigStatus {
  /** 割り込み書き込みの状態と、中身のレジスタの読み戻し（ap・address・value） */
  inject?: { busy: boolean; result: string; ack: number; count: number; time: number; ap: number; address: number;
             value: number };
  /** 番地一致の回数・時刻と、条件のレジスタの読み戻し（xtrig.set_match の引数と同じ名前） */
  match?: { count: number; time: number; address: number; address_mask: number; data: number; data_mask: number;
            on_write: boolean; on_read: boolean };
  route?: { to_la?: string[]; to_inject?: string[] };
}
export interface ObsTriggerStatus { condition: string | null; halt?: boolean; la?: boolean; count?: number; orphan?: boolean;
                                    restored?: boolean; mask?: number; value?: number; change?: number }
export interface SamplerTriggerStatus { enabled: boolean; address: number; mask: number; value: number; halt: boolean; la: boolean;
                                        matched: boolean; seen_any: boolean; seen: number; count: number; time: number;
                                        prev_time: number }
/** 捕捉の状態（la.trigger_status の internal・deep） */
export interface CaptureState { capturing: boolean; triggered: boolean; done: boolean; trigger_time: number | null }
/** la.trigger_status: ロジアナのトリガの条件と状態のレジスタの読み戻し */
export interface LaTriggerStatus {
  mask: number; value: number; rise: number; fall: number; external_only: boolean; rate_hz: number; pretrigger: number;
  filter?: { channels: number; cycles: number; min_width_ns: number | null };
  pulse?: { mode: "none" } | { mode: "width" | "timeout"; level: number; channel: number; min_ns: number; max_ns: number | null };
  internal?: CaptureState;
  deep?: CaptureState;
}
export interface Statuses {
  xtrig?: XtrigStatus | null;
  la?: LaTriggerStatus | null;
  sequence?: SequenceStatus | null;
  obs?: ObsTriggerStatus | null;
  sampler?: SamplerTriggerStatus | null;
}

/** 機器から読み戻せない（回路にその回数・時刻のレジスタが無い）。画面は「読み戻せない」とだけ示し、埋めない */
export const UNREADABLE = "unreadable" as const;
export type Fact = number | null | typeof UNREADABLE;
/** ロジアナのバッファごとのトリガの時刻（トリガしていなければ null） */
export interface LaTimes { internal: number | null; deep: number | null }
/** 回数・最後の時刻（タイムベースのカウント）。null = まだ読めていない・まだ起きていない。
 *  ロジアナのトリガは内蔵・深いバッファの時刻をそれぞれ laTimes に（1 つを選んで落とさない。time は null） */
export interface Facts { count: Fact; time: Fact; laTimes?: LaTimes }

const NONE: Facts = { count: null, time: null };
const counted = (count: number | undefined, time: number | undefined): Facts =>
  count === undefined ? NONE : { count, time: count > 0 && time !== undefined ? time : null };

/** ロジアナのトリガの時刻: 内蔵・深いバッファのそれぞれ（両方トリガしていれば両方） */
function laTriggerTimes(la: LaTriggerStatus | null | undefined): LaTimes {
  const at = (c: { triggered: boolean; trigger_time: number | null } | null | undefined) =>
    c?.triggered && c.trigger_time !== null ? c.trigger_time : null;
  return { internal: at(la?.internal), deep: at(la?.deep) };
}

export function sourceFacts(s: TriggerSource, st: Statuses): Facts {
  switch (s) {
    case "la_trigger":          // トリガの回数のレジスタは無い。時刻は捕捉のトリガの時刻（バッファごと）
      return { count: UNREADABLE, time: null, laTimes: laTriggerTimes(st.la) };
    case "host":                // 発火の回数・時刻のレジスタは無い
      return { count: UNREADABLE, time: UNREADABLE };
    case "dap_match":
      return counted(st.xtrig?.match?.count, st.xtrig?.match?.time);
    case "sequence":            // 単発: 発火したか・その時刻だけ（回数のレジスタは無い）
      return { count: UNREADABLE, time: st.sequence?.fired ? st.sequence.time : null };
    case "obs_match":           // 成り立った時刻のレジスタは無い
      return { count: st.obs?.condition !== null || st.obs?.orphan ? st.obs?.count ?? null : null, time: UNREADABLE };
    case "sampler_match":
      return counted(st.sampler?.count, st.sampler?.time);
  }
}

export function injectFacts(x: XtrigStatus | null | undefined):
  { result: string; busy: boolean; count: number; time: number | null } | null {
  const i = x?.inject;
  if (!i) return null;
  return { result: i.result, busy: i.busy, count: i.count, time: i.count > 0 ? i.time : null };
}

// --- 条件の欄（API の仕様の引数） --------------------------------------------------------------------

/** メソッドの引数の欄（仕様の型・必須・既定）。仕様に無ければ空 */
export function methodFields(method: string): Field[] {
  const m = METHODS.find((x) => x.name === method);
  return m ? fieldsOf(m.params) : [];
}

/** 欄から呼び出し。空の必須の欄があれば呼ばない。空の省ける欄は送らない（既定はデーモンが決める） */
export function formCall(method: string, values: FieldValues): Call | { errors: Record<string, FieldError> } {
  const built = buildParams(methodFields(method), values);
  return "errors" in built ? built : { method, params: built.params };
}

export const hexText = (n: number) => `0x${(n >>> 0).toString(16).toUpperCase()}`;

/** 欄の値の表示: 0 以上の整数は大きさによらず 16 進（この窓の欄は番地・マスク・バスの語。大きさで意味を推さない。
 *  欄は 16 進のまま formCall で同じ数に戻る）。負の数・ほかはそのまま */
function valueText(v: unknown): string {
  if (typeof v === "number" && Number.isInteger(v) && v >= 0) return `0x${v.toString(16).toUpperCase()}`;
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** 省ける欄の既定（仕様の default）の表示。既定が無ければ null */
export function defaultText(f: Field): string | null {
  return f.default === undefined ? null : valueText(f.default);
}

/** 読み戻した設定（xtrig.status の match・inject 等）から、メソッドの欄の文字（その引数の名前の値だけ） */
export function formValues(method: string, read: Record<string, unknown> | null | undefined): FieldValues {
  if (!read) return {};
  const out: FieldValues = {};
  for (const f of methodFields(method)) {
    const v = read[f.name];
    if (v !== undefined && v !== null) out[f.name] = valueText(v);
  }
  return out;
}
