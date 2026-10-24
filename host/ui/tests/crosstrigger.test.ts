// クロストリガの窓（lib/crosstrigger.ts）: 構成で使える源・行き先、経路の表（xtrig.status の route の事実 ↔ xtrig.route の
// 引数）、出力の端子の源（xtrig.output_status ↔ xtrig.output_set）、源ごとの回数・時刻（API が返す事実だけ）、条件の欄
import { describe, expect, it } from "vitest";
import {
  ACTIONS, actionAvailability, cellBlock, defaultText, formCall, formValues, hexText, injectFacts, methodFields, outputColumns,
  outputSourcesCall, routeChanged, routeOf, routeParams, sourceAvailability, sourceFacts, toggleRoute,
  type OutputPin, type Route,
} from "../src/lib/crosstrigger";
import { SOURCES } from "../src/lib/logic";

const FULL = { xtrig: true, trig_seq: true, inject: true, la_channels: 10, obs_match: true, sampler_match: true,
               output_actions: 2 };

describe("構成（ケイパビリティ）で使える源・行き先", () => {
  it("全部ある構成では全部使える", () => {
    const s = sourceAvailability(FULL);
    expect(SOURCES.every((x) => s[x].ok)).toBe(true);
    const a = actionAvailability(FULL);
    expect(ACTIONS.every((x) => a[x].ok)).toBe(true);
  });

  it("無い源は、どのケイパビリティがどの値だったかを返す（隠さない・推さない）", () => {
    const s = sourceAvailability({ ...FULL, sampler_match: false, trig_seq: false });
    expect(s.sampler_match).toEqual({ ok: false, cap: "sampler_match", value: false });
    expect(s.sequence).toEqual({ ok: false, cap: "trig_seq", value: false });
    expect(s.obs_match.ok).toBe(true);
  });

  it("申告に無いケイパビリティは「無い」（値 undefined）。暗黙に有るとしない", () => {
    const s = sourceAvailability({ xtrig: true, la_channels: 10 });
    expect(s.obs_match).toEqual({ ok: false, cap: "obs_match", value: undefined });
    expect(s.dap_match.ok).toBe(true);
    expect(actionAvailability({ xtrig: true }).inject).toEqual({ ok: false, cap: "inject", value: undefined });
  });

  it("クロストリガが無い構成では、全部の源・行き先が xtrig の理由で無い", () => {
    const caps = { ...FULL, xtrig: false };
    expect(Object.values(sourceAvailability(caps)).every((v) => !v.ok && v.cap === "xtrig")).toBe(true);
    expect(Object.values(actionAvailability(caps)).every((v) => !v.ok && v.cap === "xtrig")).toBe(true);
  });

  it("ロジアナの ch が 0 ならロジアナのトリガ・取り込みは無い。出力の端子は動作の数 output_actions", () => {
    expect(sourceAvailability({ ...FULL, la_channels: 0 }).la_trigger).toEqual({ ok: false, cap: "la_channels", value: 0 });
    expect(actionAvailability({ ...FULL, la_channels: 0 }).la).toEqual({ ok: false, cap: "la_channels", value: 0 });
    expect(actionAvailability({ ...FULL, output_actions: 0 }).outputs).toEqual({ ok: false, cap: "output_actions", value: 0 });
  });

  it("caps が無い（つながる前）は全部 xtrig の理由で無い", () => {
    expect(sourceAvailability(null).host).toEqual({ ok: false, cap: "xtrig", value: undefined });
  });
});

describe("経路の表", () => {
  it("xtrig.status の route（レジスタの事実）をそのまま。知らない名前は落とさず unknown に分ける", () => {
    expect(routeOf({ route: { to_la: ["dap_match", "obs_match"], to_inject: ["sampler_match", "future_bit"] } }))
      .toEqual({ to_la: ["dap_match", "obs_match"], to_inject: ["sampler_match"], unknown: ["future_bit"] });
    expect(routeOf({ match: { count: 0, time: 0 } as any })).toBeNull();          // 古いデーモン: 経路を返さない
    expect(routeOf(null)).toBeNull();
  });

  it("切り替えと、xtrig.route の引数（源の順にそろえる。指定しない行き先は切れるので両方を必ず送る）", () => {
    let r: Route = { to_la: ["host"], to_inject: [] };
    r = toggleRoute(r, "to_la", "dap_match");
    r = toggleRoute(r, "to_inject", "sequence");
    r = toggleRoute(r, "to_la", "host");
    expect(routeParams(r)).toEqual({ to_la: ["dap_match"], to_inject: ["sequence"] });
    expect(routeParams({ to_la: ["sampler_match", "la_trigger"], to_inject: [] }))
      .toEqual({ to_la: ["la_trigger", "sampler_match"], to_inject: [] });
  });

  it("事実と違うセル（書き換えた所）", () => {
    const facts: Route = { to_la: ["host"], to_inject: [] };
    const draft: Route = { to_la: [], to_inject: ["host"] };
    expect(routeChanged(facts, draft, "to_la", "host")).toBe(true);
    expect(routeChanged(facts, draft, "to_inject", "host")).toBe(true);
    expect(routeChanged(facts, draft, "to_la", "dap_match")).toBe(false);
    expect(routeChanged(null, draft, "to_la", "dap_match")).toBe(false);
  });

  it("触れないセル: 源が無い・行き先が無い・ロジアナのトリガ → ロジアナ（自分自身）", () => {
    const src = sourceAvailability({ ...FULL, obs_match: false });
    const act = actionAvailability({ ...FULL, inject: false });
    expect(cellBlock("la_trigger", "to_la", src, act)).toEqual({ kind: "self" });
    expect(cellBlock("obs_match", "to_la", src, act)).toEqual({ kind: "source", cap: "obs_match", value: false });
    expect(cellBlock("host", "to_inject", src, act)).toEqual({ kind: "action", cap: "inject", value: false });
    expect(cellBlock("host", "to_la", src, act)).toBeNull();
  });
});

describe("出力の端子", () => {
  const pins: OutputPin[] = [
    { channel: 4, pull_up: false, configured: false },
    { channel: 6, pull_up: true, configured: true, enabled: true, active: false, mode: "while", level: "high",
      pattern: { mask: 0, value: 0, any: false }, sources: ["dap_match"] },
    { channel: 7, pull_up: false, configured: true, enabled: false, active: false, mode: "pulse", level: "low",
      pattern: { mask: 0, value: 0, any: false }, sources: ["host"], pulse_us: { min: 9.6, max: 10.24 } },
  ];

  it("列は設定済みの端子だけ（xtrig.output_status の事実）。脈は長さを読み戻せないので表では変えない", () => {
    expect(outputColumns(pins)).toEqual([
      { channel: 6, sources: ["dap_match"], editable: true },
      { channel: 7, sources: ["host"], editable: false, reason: "pulse" },
    ]);
    expect(outputColumns(null)).toEqual([]);
  });

  it("源を変える: 今の形・値・有効をそのまま xtrig.output_set に（読んだ事実だけで組み立てる）", () => {
    expect(outputSourcesCall(pins[1], ["dap_match", "sampler_match"])).toEqual({
      method: "xtrig.output_set",
      params: { channel: 6, mode: "while", level: "high", sources: ["dap_match", "sampler_match"], enable: true },
    });
  });

  it("端子の並びの条件があれば一緒に送る（消さない）。条件が全部無くなるなら呼ばない", () => {
    const withPattern: OutputPin = { ...pins[1], pattern: { mask: 3, value: 1, any: true } };
    expect(outputSourcesCall(withPattern, [])).toEqual({
      method: "xtrig.output_set",
      params: { channel: 6, mode: "while", level: "high", pattern: { mask: 3, value: 1, any: true }, enable: true },
    });
    expect(outputSourcesCall(pins[1], [])).toEqual({ error: "empty" });
    expect(outputSourcesCall(pins[0], ["host"])).toEqual({ error: "notConfigured" });
    expect(outputSourcesCall(pins[2], ["host"])).toEqual({ error: "pulse" });
  });
});

describe("回数・時刻（機器のレジスタの事実だけ。回路に無いものは unreadable、まだ無いものは null）", () => {
  const st = {
    xtrig: { inject: { busy: false, result: "ok", ack: 1, count: 2, time: 500, ap: 0, address: 0xE000EDF0, value: 0xA05F0003 },
             match: { count: 3, time: 1234, address: 0x20000010, address_mask: 0xFFFFFFFF, data: 0, data_mask: 0,
                      on_write: true, on_read: false } },
    sequence: { stage: 1, fired: true, time: 777, armed: true, stages: 2, stage_states: ["done", "done"] as const },
    obs: { condition: "state=BUSY", halt: true, la: false, count: 5 },
    sampler: { enabled: true, address: 0xE000EDF0, mask: 0x20000, value: 0x20000, halt: false, la: true, matched: false,
               seen_any: true, seen: 40, count: 1, time: 900, prev_time: 880 },
    la: { mask: 0, value: 0, rise: 0, fall: 0, external_only: false, rate_hz: 25e6, pretrigger: 1024,
          internal: { capturing: false, triggered: true, done: true, trigger_time: 4321 } },
  };

  it("源ごと。回数・時刻のレジスタが回路に無いものは unreadable（推さない）", () => {
    expect(sourceFacts("la_trigger", st)).toEqual({ count: "unreadable", time: null, laTimes: { internal: 4321, deep: null } });
    expect(sourceFacts("host", st)).toEqual({ count: "unreadable", time: "unreadable" });
    expect(sourceFacts("dap_match", st)).toEqual({ count: 3, time: 1234 });
    expect(sourceFacts("sequence", st)).toEqual({ count: "unreadable", time: 777 });
    expect(sourceFacts("obs_match", st)).toEqual({ count: 5, time: "unreadable" });
    expect(sourceFacts("sampler_match", st)).toEqual({ count: 1, time: 900 });
  });

  it("0 回なら時刻は無い。読めていなければ null", () => {
    expect(sourceFacts("dap_match", { xtrig: { match: { count: 0, time: 0 } as any } })).toEqual({ count: 0, time: null });
    expect(sourceFacts("sequence", { sequence: { ...st.sequence, fired: false } })).toEqual({ count: "unreadable", time: null });
    expect(sourceFacts("obs_match", { obs: { condition: null } })).toEqual({ count: null, time: "unreadable" });
    expect(sourceFacts("sampler_match", {})).toEqual({ count: null, time: null });
    expect(sourceFacts("la_trigger", {})).toEqual({ count: "unreadable", time: null, laTimes: { internal: null, deep: null } });
  });

  it("ロジアナのトリガの時刻: 内蔵と深いバッファのそれぞれ（両方なら両方。新しい方だけを選ばない）", () => {
    const deep = { capturing: false, triggered: true, done: true, trigger_time: 9000 };
    expect(sourceFacts("la_trigger", { la: { ...st.la, deep } }).laTimes).toEqual({ internal: 4321, deep: 9000 });
    expect(sourceFacts("la_trigger", { la: { ...st.la, internal: { ...st.la.internal, triggered: false, trigger_time: null } } })
      .laTimes).toEqual({ internal: null, deep: null });
  });

  it("割り込み書き込みの結果・回数・時刻", () => {
    expect(injectFacts(st.xtrig)).toEqual({ result: "ok", busy: false, count: 2, time: 500 });
    expect(injectFacts({ inject: { busy: false, result: "ok", ack: 0, count: 0, time: 0, ap: 0, address: 0, value: 0 } }))
      .toEqual({ result: "ok", busy: false, count: 0, time: null });
    expect(injectFacts(null)).toBeNull();
  });
});

describe("条件の欄（API の仕様の引数から）", () => {
  it("欄は仕様の引数そのまま。必須は required（例: sampler.trigger_set は 5 つとも必須）", () => {
    expect(methodFields("sampler.trigger_set").map((f) => [f.name, f.required]))
      .toEqual([["address", true], ["mask", true], ["value", true], ["halt", true], ["la", true]]);
    expect(methodFields("xtrig.set_match").filter((f) => f.required).map((f) => f.name)).toEqual(["address"]);
    expect(methodFields("no.such")).toEqual([]);
  });

  it("空の必須の欄があれば呼ばない。空の省ける欄は送らない（既定はデーモンが決める）", () => {
    expect(formCall("xtrig.set_match", {})).toEqual({ errors: { address: { code: "required" } } });
    expect(formCall("xtrig.set_match", { address: "0x20000010", on_read: "true" })).toEqual(
      { method: "xtrig.set_match", params: { address: 0x20000010, on_read: true } });
    expect(formCall("fpga_obs.trigger_set", { condition: "state=BUSY", halt: "", la: "true" })).toEqual(
      { method: "fpga_obs.trigger_set", params: { condition: "state=BUSY", la: true } });
  });

  it("既定の値の表示: 0 以上の整数は大きさによらず 16 進（値の大きさで意味を推さない）、ほかはそのまま", () => {
    expect(defaultText(methodFields("xtrig.set_inject").find((f) => f.name === "address")!)).toBe("0xE000EDF0");
    expect(defaultText(methodFields("xtrig.set_inject").find((f) => f.name === "ap")!)).toBe("0x0");
    expect(defaultText(methodFields("xtrig.set_match").find((f) => f.name === "on_write")!)).toBe("true");
    expect(defaultText(methodFields("xtrig.set_match").find((f) => f.name === "address")!)).toBeNull();
  });
});

describe("読み戻した設定を欄へ（機器の今の値。操作の記録からは推さない）", () => {
  it("メソッドの引数の名前の値だけを欄の文字に（0 以上の整数は 16 進、真偽は true / false）", () => {
    const match = { count: 3, time: 1, address: 0x20000010, address_mask: 0xFFFFFFFF, data: 5, data_mask: 0,
                    on_write: true, on_read: false };
    expect(formValues("xtrig.set_match", match)).toEqual({ address: "0x20000010", address_mask: "0xFFFFFFFF", data: "0x5",
                                                          data_mask: "0x0", on_write: "true", on_read: "false" });
    expect(formValues("xtrig.set_inject", { ap: 0, address: 0xE000EDF0, value: 0xA05F0003, count: 2 }))
      .toEqual({ ap: "0x0", address: "0xE000EDF0", value: "0xA05F0003" });
    expect(formValues("xtrig.set_match", null)).toEqual({});
  });

  it("欄の文字は formCall で同じ引数に戻る", () => {
    const v = formValues("xtrig.set_inject", { ap: 1, address: 0x40000000, value: 7 });
    expect(formCall("xtrig.set_inject", v)).toEqual({ method: "xtrig.set_inject", params: { ap: 1, address: 0x40000000, value: 7 } });
  });
});

describe("値の短い表示", () => {
  it("大きい整数は 16 進", () => {
    expect(hexText(0x20000010)).toBe("0x20000010");
    expect(hexText(5)).toBe("0x5");
  });
});
