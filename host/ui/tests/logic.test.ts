// Logic Analyzer: 解読器の欄 → settings、注釈 → 印、トリガの欄 ↔ xtrig の引数、段の図、測定の範囲
import { describe, expect, it } from "vitest";
import {
  decodeMarks, hiddenKinds, invalidSettings, isChannelParam, measureCall, measureRange, measuredChannels, missingSettings, newForm,
  patternForm, patternOf, settingValue, settingsOf,
  stageForms, stageLabel, stageState, stagesOf, type DecoderInfo,
} from "../src/lib/logic";

const DECODERS: DecoderInfo[] = [
  { name: "uart", lower: [], summary: "UART", params: [{ name: "channel", required: true, nullable: false, type: "channel" },
                                                       { name: "baud", required: true, nullable: false, type: "number" }],
    default_kinds: null, origin: "builtin" },
  { name: "spi", lower: [], summary: "SPI", params: [{ name: "sck", required: true, nullable: false, type: "channel" },
                                                     { name: "mosi", required: true, nullable: true, type: "channel" },
                                                     { name: "cs", required: true, nullable: true, type: "channel" },
                                                     { name: "mode", required: true, nullable: false, type: "integer" }],
    default_kinds: null, origin: "builtin" },
  { name: "modbus", lower: ["uart"], summary: "Modbus RTU",
    params: [{ name: "address", required: false, nullable: true, default: null, type: "integer" }],
    default_kinds: null, origin: "builtin" },
];

describe("解読器の設定", () => {
  it("欄はすべて空から（チャンネルも割り当てない: 既定の値を画面に持たない）。積み重ねる解読器は下の欄も", () => {
    expect(newForm("spi", DECODERS)).toEqual({ protocol: "spi", values: { sck: "", mosi: "", cs: "", mode: "" }, lower: null });
    const f = newForm("modbus", DECODERS);
    expect(f.lower).toEqual({ protocol: "uart", values: { channel: "", baud: "" }, lower: null });
  });

  it("必須の欄が空なら呼べない: 足りない設定の名前（下の解読は 下の名前.設定）", () => {
    const f = newForm("modbus", DECODERS);
    expect(missingSettings(f, DECODERS)).toEqual(["uart.channel", "uart.baud"]);
    f.lower!.values.channel = "0";
    f.lower!.values.baud = "9600";
    expect(missingSettings(f, DECODERS)).toEqual([]);
    const s = newForm("spi", DECODERS);
    Object.assign(s.values, { sck: "0", mosi: "1", mode: "0" });
    expect(missingSettings(s, DECODERS)).toEqual(["cs"]);
    s.values.cs = "null";                                        // つながない線は null を選んで明示する
    expect(missingSettings(s, DECODERS)).toEqual([]);
    expect(settingsOf(s, DECODERS)).toEqual({ sck: 0, mosi: 1, cs: null, mode: 0 });
  });

  it("欄 → settings（空は送らない。下の解読は下の名前のキー）", () => {
    const f = newForm("modbus", DECODERS);
    f.values.address = "0x11";
    f.lower!.values.channel = "0";
    f.lower!.values.baud = "9600";
    expect(settingsOf(f, DECODERS)).toEqual({ address: 17, uart: { channel: 0, baud: 9600 } });
  });

  it("チャンネルの欄かは解読器が申告した型（type = channel）で決める。設定の名前では推さない", () => {
    expect(isChannelParam({ name: "rx", required: true, type: "channel" })).toBe(true);        // 利用者の解読器の線の名前
    expect(isChannelParam({ name: "enable", required: true, type: "boolean" })).toBe(false);   // 線でない enable
    expect(isChannelParam({ name: "channel", required: true })).toBe(false);                   // 型の申告が無い
  });

  it("欄の文字は申告した型で読む（文字の形から型を推さない）。読めなければ誤りの欄", () => {
    const p = (type: any, nullable = false) => ({ name: "x", required: true, nullable, type });
    expect(settingValue("0x10", p("integer"))).toBe(16);
    expect(settingValue("1,2", p("string"))).toBe("1,2");              // 文字の設定は数の並びに変えない
    expect(settingValue("0x10", p("string"))).toBe("0x10");
    expect(settingValue("0.75", p("number"))).toBe(0.75);
    expect(settingValue("115200", p("number"))).toBe(115200);
    expect(settingValue("true", p("boolean"))).toBe(true);
    expect(settingValue("1, 2, 0x3", p("channels"))).toEqual([1, 2, 3]);
    expect(settingValue("[4, 5]", p("channels"))).toEqual([4, 5]);
    expect(settingValue("null", p("channel", true))).toBeNull();
    expect(settingValue('{"a": 1}', p("object"))).toEqual({ a: 1 });
    expect(() => settingValue("abc", p("integer"))).toThrow();
    expect(() => settingValue("1.5", p("channel"))).toThrow();
    expect(() => settingValue("yes", p("boolean"))).toThrow();
    expect(() => settingValue("null", p("integer"))).toThrow();          // null を渡せない設定
    // 型の申告が無い（利用者の解読器の注釈が無い等）: JSON として読めれば JSON、読めなければ文字（決まった規則）
    expect(settingValue("12", p(null))).toBe(12);
    expect(settingValue("arm_dp", p(null))).toBe("arm_dp");
    const f = newForm("spi", DECODERS);
    Object.assign(f.values, { sck: "a", mosi: "1", cs: "null", mode: "0" });
    expect(invalidSettings(f, DECODERS)).toEqual(["sck"]);
  });

  it("注釈は区間の印、誤りは tone 1", () => {
    const a = { start: 10, end: 20, start_rel_s: 0, end_rel_s: 0, kind: "data", text: "'U'", value: 85, error: null, fields: null };
    expect(decodeMarks([a, { ...a, start: 30, end: 30, error: "framing", text: "x" }]))
      .toEqual([{ t: 10, tEnd: 20, label: "'U'" }, { t: 30, label: "x", tone: 1 }]);
  });
});

describe("トリガ", () => {
  it("ピンの条件: x = 見ない、0 / 1 = 値、立ち上がり・立ち下がり。往復できる", () => {
    const f = patternForm(null, 4);
    f.level[1] = "1";
    f.level[2] = "0";
    f.rise[3] = true;
    f.fall[0] = true;
    const p = patternOf(f);
    expect(p).toEqual({ mask: 0b0110, value: 0b0010, rise: 0b1000, fall: 0b0001 });
    expect(patternForm(p, 4)).toEqual(f);
  });

  it("段: 事象が無ければ誤り、段 0 の制限時間は送らない、回数は使う人が入れる 1 以上の整数（空・0 を黙って 1 にしない）", () => {
    const forms = [{ when: ["pattern_a" as const], count: "1", withinUs: "5" },
                   { when: ["pattern_b" as const, "host" as const], count: "2", withinUs: "10" }];
    expect(stagesOf(forms)).toEqual([{ when: ["pattern_a"], count: 1 }, { when: ["pattern_b", "host"], count: 2, within_us: 10 }]);
    expect(() => stagesOf([{ when: [], count: "1", withinUs: "" }])).toThrow();
    expect(() => stagesOf([{ when: ["host"], count: "", withinUs: "" }])).toThrow();
    expect(() => stagesOf([{ when: ["host"], count: "0", withinUs: "" }])).toThrow();
    expect(() => stagesOf([{ when: ["host"], count: "1.5", withinUs: "" }])).toThrow();
    expect(stageForms([{ when: ["dap_match", "bogus"], count: 2, within_us: 3 }]))
      .toEqual([{ when: ["dap_match"], count: "2", withinUs: "3" }]);
  });

  it("段の図の文字と状態", () => {
    expect(stageLabel({ when: ["pattern_a", "host"], count: "3", withinUs: "10" }, 1)).toEqual({ events: "A | Host", detail: "×3 ≤10 µs" });
    expect(stageLabel({ when: ["dap_match"], count: "1", withinUs: "10" }, 0)).toEqual({ events: "Match", detail: "" });
  });

  it("段の状態は xtrig.sequence_status の stage_states をそのまま描く（画面で今の段と比べない。b28）", () => {
    // stage・fired と食い違う stage_states を渡し、画面が stage_states だけを見ていることを確かめる
    const st = { stage: 0, fired: false, armed: true, stage_states: ["done", "current", "waiting"] as const };
    expect([0, 1, 2].map((i) => stageState(i, st))).toEqual(["done", "current", "waiting"]);
    expect(stageState(3, st)).toBe("idle");          // 申告の無い段（設定の外）
    expect(stageState(0, null)).toBe("idle");        // 読めていない
  });
});

describe("測定", () => {
  it("基準と選択のカーソルの間（両方あるとき）、無ければ全体", () => {
    expect(measureRange(50, 10)).toEqual([10, 51]);
    expect(measureRange(null, 10)).toBeNull();
    expect(measureCall("capture", [10, 51])).toEqual({ method: "la.measure", params: { start: 10, end: 51 } });
    expect(measureCall("recording", null)).toEqual({ method: "recording.waveform_measure", params: { t0: null, t1: null } });
  });

  it("la.measure（channels 省略）が返したチャンネルをそのまま描く（画面で変化の有無を選ばない。b29）", () => {
    const ch = (n: number) => ({ rises: n, falls: n, high_ratio: 0.5 });
    // 変化の無いチャンネルを返すかはデーモンが決める（channels を指定すれば返る）。画面は返ったものを全部描く
    expect(measuredChannels({ "2": ch(3), "4": ch(0), "10": ch(1) }).map(([n]) => n)).toEqual([2, 4, 10]);
  });
});

describe("解読の件数と表", () => {
  it("既定の種類の外で表に出ない件数と種類（件数と表の食い違いを説明する）", () => {
    expect(hiddenKinds({ kinds: ["transfer"], counts: { line_reset: 1 } })).toEqual({ n: 1, kinds: ["line_reset"] });
    expect(hiddenKinds({ kinds: ["transfer"], counts: { transfer: 3, line_reset: 2, idle: 1 } }))
      .toEqual({ n: 3, kinds: ["idle", "line_reset"] });
    expect(hiddenKinds({ kinds: null, counts: { frame: 2 } })).toEqual({ n: 0, kinds: [] });
    expect(hiddenKinds({ counts: { frame: 2 } })).toEqual({ n: 0, kinds: [] });              // 古いデーモン
  });
});

describe("laExportParams（捕捉の保存。la.export）", async () => {
  const { laExportParams } = await import("../src/lib/logic");
  it("既定のパスは日時と形式の拡張子。手で変えたパスはそのまま", () => {
    const at = new Date(2026, 9, 22, 21, 5, 9);
    expect(laExportParams("sr", null, at)).toEqual({ format: "sr", path: "capture-20261022-210509.sr" });   // 2 回目に上書きしない
    expect(laExportParams("vcd", null, at)).toEqual({ format: "vcd", path: "capture-20261022-210509.vcd" });
    expect(laExportParams("vcd", "D:/x/a.vcd", at)).toEqual({ format: "vcd", path: "D:/x/a.vcd" });
  });
});
