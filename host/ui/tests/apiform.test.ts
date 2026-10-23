// API パネルの計算（lib/apiform.ts）: 仕様から欄を作り、欄の文字列から引数を組み立てる
import { describe, expect, it } from "vitest";
import spec from "../../api/fprobe_api.json";
import { apiMethods, buildParams, confirmLevel, fieldsOf, valuesOf } from "../src/lib/apiform";

describe("apiMethods", () => {
  it("MCP の道具と同じ範囲（session.* と binary を除く）を名前の順に", () => {
    const methods = apiMethods(spec as any);
    const names = methods.map((m) => m.name);
    const all = Object.keys((spec as any).methods);
    expect(names.some((n) => n.startsWith("session."))).toBe(false);
    expect(names).not.toContain("timeline.tiles");
    expect(names.length).toBe(all.length - all.filter((n) => n.startsWith("session.")).length - 1);
    expect(names).toEqual([...names].sort());
    expect(methods.find((m) => m.name === "target.flash.erase")?.irreversible).toBe(true);
    expect(methods.find((m) => m.name === "target.memory.read")?.irreversible).toBe(false);
  });
});

const params = {
  type: "object",
  properties: {
    address: { type: "integer", minimum: 0, description: "番地" },
    count: { anyOf: [{ type: "integer", minimum: 1 }, { type: "null" }], default: null },
    rate: { type: ["number", "null"], exclusiveMinimum: 0 },
    halt: { type: "boolean", default: false },
    mode: { enum: ["range", "chip"] },
    level: { enum: [0, 1, 2] },
    name: { anyOf: [{ type: "string" }, { type: "null" }] },
    fields: { type: "object" },
    names: { anyOf: [{ type: "array", items: { type: "string" } }, { type: "null" }] },
  },
  required: ["address"],
};

describe("fieldsOf", () => {
  it("型から欄の種類を決め、null を許す形をほどく", () => {
    const f = Object.fromEntries(fieldsOf(params).map((x) => [x.name, x]));
    expect(f.address).toMatchObject({ kind: "integer", required: true, nullable: false, min: 0, description: "番地" });
    expect(f.count).toMatchObject({ kind: "integer", nullable: true, min: 1, required: false });
    expect(f.rate).toMatchObject({ kind: "number", nullable: true, exclusiveMin: 0 });
    expect(f.halt).toMatchObject({ kind: "boolean", default: false });
    expect(f.mode).toMatchObject({ kind: "enum", options: ["range", "chip"] });
    expect(f.level).toMatchObject({ kind: "enum", options: [0, 1, 2] });
    expect(f.name).toMatchObject({ kind: "string", nullable: true });
    expect(f.fields.kind).toBe("json");
    expect(f.names).toMatchObject({ kind: "json", nullable: true });
  });

  it("引数の無いメソッドは欄が無い", () => {
    expect(fieldsOf({ type: "object", properties: {} })).toEqual([]);
    expect(fieldsOf({})).toEqual([]);
  });
});

describe("buildParams", () => {
  const fields = fieldsOf(params);

  it("空の欄は送らない。整数は 10 進・16 進・負の数", () => {
    expect(buildParams(fields, { address: "0x2000_0000".replace("_", ""), count: "" })).toEqual({
      params: { address: 0x20000000 } });
    expect(buildParams(fields, { address: "16", halt: "true", mode: "chip", level: "2", name: "a b",
                                 fields: '{"x": 1}', names: '["r0"]', rate: "1.5e6" })).toEqual({
      params: { address: 16, halt: true, mode: "chip", level: 2, name: "a b", fields: { x: 1 }, names: ["r0"],
                rate: 1.5e6 } });
  });

  it("必須の空・整数でない・範囲外・壊れた JSON は送らずに欄の誤り", () => {
    const r = buildParams(fields, { address: "", count: "0", rate: "0", halt: "yes", level: "5", fields: "{x" });
    expect("errors" in r && Object.keys(r.errors).sort()).toEqual(["address", "count", "fields", "halt", "level", "rate"]);
    const bad = buildParams(fields, { address: "1.5" });
    expect("errors" in bad && bad.errors.address).toBeTruthy();
    const neg = buildParams(fields, { address: "-1" });
    expect("errors" in neg && neg.errors.address).toBeTruthy();       // minimum 0
  });

  it("負の整数は範囲があれば受ける", () => {
    const f = fieldsOf({ type: "object", properties: { delta: { type: "integer" } } });
    expect(buildParams(f, { delta: "-0x10" })).toEqual({ params: { delta: -16 } });
  });
});

describe("valuesOf", () => {
  it("引数（Activity の行）から欄の文字列へ。組み立て直すと同じ引数", () => {
    const fields = fieldsOf(params);
    const p = { address: 4096, halt: true, mode: "range", fields: { a: 1 }, names: ["pc"], level: 1 };
    const v = valuesOf(fields, p).values;
    expect(v.address).toBe("4096");
    expect(buildParams(fields, v)).toEqual({ params: p });
  });
});

describe("confirmLevel", () => {
  it("read は確かめない、control は確かめ、取り返しのつかない操作は赤", () => {
    expect(confirmLevel({ kind: "read", irreversible: false })).toBe("none");
    expect(confirmLevel({ kind: "control", irreversible: false })).toBe("confirm");
    expect(confirmLevel({ kind: "control", irreversible: true })).toBe("danger");
  });
});

describe("UiState.openApi（Activity の行から API パネルへ）", async () => {
  const { UiState } = await import("../src/lib/ui");
  it("メソッドと引数を渡し、頼むたびに seq が増える", () => {
    const ui = new UiState();
    expect(ui.snapshot.api).toBeNull();
    ui.openApi("target.memory.read", { address: 4096, length: 4 });
    expect(ui.snapshot.api).toEqual({ method: "target.memory.read", params: { address: 4096, length: 4 }, seq: 1 });
    ui.openApi("target.memory.read", {});
    expect(ui.snapshot.api?.seq).toBe(2);
  });
});

describe("レビューの指摘（null・要約・空白・整数か文字列）", () => {
  const p = {
    type: "object",
    properties: {
      tpiu_base: { anyOf: [{ type: "integer" }, { type: "null" }], default: 3758358528 },
      text: { type: "string" },
      register: { anyOf: [{ type: "integer" }, { type: "string" }] },
      value: { type: ["integer", "number", "string"] },
      channel: { anyOf: [{ type: "integer", minimum: 0 }, { enum: ["semihosting"] }] },
      fields: { type: "object" },
      pulse_us: { type: "number", exclusiveMinimum: 0 },
    },
    required: ["register"],
  };
  const fields = fieldsOf(p);

  it("null を許す欄は null を明示して送れる（既定が null でない引数で「触らない」を選べる）", () => {
    expect(buildParams(fields, { register: "1", tpiu_base: null })).toEqual({ params: { register: 1, tpiu_base: null } });
    const { values } = valuesOf(fields, { register: 1, tpiu_base: null });
    expect(values.tpiu_base).toBeNull();
    expect(buildParams(fields, values)).toEqual({ params: { register: 1, tpiu_base: null } });
  });

  it("欄の型に合わない値（Activity の要約）は入れずに名前を返す", () => {
    const summarized = { length: 500, head: "abc" };
    const r = valuesOf(fields, { register: 2, text: summarized, fields: { length: 40, head: [1] } as any, value: [1] as any });
    expect(r.values.text).toBeUndefined();
    expect(r.values.value).toBeUndefined();
    expect(r.unrestored.sort()).toEqual(["text", "value"]);
    expect(r.values.fields).toBe('{"length":40,"head":[1]}');          // json の欄は形を問わない（確かめで中身が見える）
  });

  it("文字列は空白・改行を削らずに送る", () => {
    expect(buildParams(fields, { register: "1", text: "reset\n" })).toEqual({ params: { register: 1, text: "reset\n" } });
    expect(buildParams(fields, { register: "1", text: " " })).toEqual({ params: { register: 1, text: " " } });
  });

  it("整数か文字列の欄は 16 進数を整数に、ほかは文字列に。列挙は照合", () => {
    expect(fields.find((f) => f.name === "register")?.kind).toBe("scalar");
    expect(buildParams(fields, { register: "0x24" })).toEqual({ params: { register: 0x24 } });
    expect(buildParams(fields, { register: "CTRL/STAT" })).toEqual({ params: { register: "CTRL/STAT" } });
    expect(buildParams(fields, { register: "1", value: "1.5" })).toEqual({ params: { register: 1, value: 1.5 } });
    expect(buildParams(fields, { register: "1", channel: "semihosting" })).toEqual({ params: { register: 1, channel: "semihosting" } });
    const bad = buildParams(fields, { register: "1", channel: "x" });
    expect("errors" in bad && bad.errors.channel).toBeTruthy();
  });

  it("安全な範囲を超える整数は json の欄でも断る", () => {
    const r = buildParams(fields, { register: "1", fields: '{"v": 9007199254740993}' });
    expect("errors" in r && r.errors.fields?.code).toBe("unsafe");
  });

  it("exclusiveMinimum の誤りは「より大きく」", () => {
    const r = buildParams(fields, { register: "1", pulse_us: "0" });
    expect("errors" in r && r.errors.pulse_us).toEqual({ code: "exclusiveMin", limit: 0 });
  });
});

describe("UiState の API パネルへの依頼は 1 回だけ当たる", async () => {
  const { UiState } = await import("../src/lib/ui");
  it("受け取ったら消す（パネルを付け直しても前の依頼が当たらない）。お気に入りから開いたら、その id も渡す", () => {
    const ui = new UiState();
    ui.openApi("recording.list", {}, "fav-1");
    expect(ui.takeApi()).toMatchObject({ method: "recording.list", favorite: "fav-1" });
    expect(ui.takeApi()).toBeNull();
    expect(ui.snapshot.api).toBeNull();
  });
});
