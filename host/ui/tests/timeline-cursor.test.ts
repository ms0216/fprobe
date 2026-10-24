// 共通タイムライン（G01）のマウスの縦線の値（全段）と「時刻へ」: 段の値の文字、問い合わせのまとめ（最後の位置だけ）、
// TimelineState.valuesAt・goTo。タイルは固定データ（rows-raw.bin: 値・状態・区間・印の段。host/tests/ui_fixture.py）
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { AppState } from "../src/lib/state";
import { CursorProbe, cursorRows, cursorText } from "../src/lib/timeline/cursor";
import { InlineDecimator } from "../src/lib/timeline/decimator";
import { decodeFrame } from "../src/lib/timeline/frame";
import type { PickedItem } from "../src/lib/timeline/query";
import type { VisibleRow } from "../src/lib/timeline/rows";
import { TimelineState, type TimelineSnapshot } from "../src/lib/timeline/state";
import { FakeConn } from "./fake-conn";

const bytes = readFileSync(resolve(import.meta.dirname, "fixtures", "rows-raw.bin"));
const rowsRaw = () => decodeFrame(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer).result;

const settle = async () => {
  for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0));
};

function setup() {
  const conn = new FakeConn({
    "session.status": { boot: "a", device: null, control: { paused: false, paused_by: null, held: 0 }, clients: [], target: null },
    "session.activity.list": { entries: [] },
    "probe.info": { capabilities: {} },
    "timeline.tiles": () => rowsRaw(),
    "recording.status": { recording: false, name: "rec-rows", span: [0, 19999], timebase_hz: 1e6 },
  });
  const tl = new TimelineState(conn as any, new AppState(conn as any), new InlineDecimator());
  let snap!: TimelineSnapshot;
  tl.subscribe((s) => (snap = s));
  return { conn, tl, get: () => snap };
}

const item = (p: Partial<PickedItem>): PickedItem => ({
  row: "r", type: "values", stream: "aux", kind: "mv", t: 0, tEnd: 0, label: null, value: null, unit: null, tone: 0,
  depth: 0, address: null, jump: null, ...p,
});

const row = (id: string, type: VisibleRow["def"]["type"], header = false): VisibleRow =>
  ({ def: { id, group: "g", type, title: null, color: 0 }, header, folded: false, count: 0 });

describe("マウスの縦線の値（段の名前の列）", () => {
  it("値を持つ段（値・状態）だけを問い合わせる。見出し・波形・区間・印は問い合わせない", () => {
    const rows = [row("group:aux", "group", true), row("aux.voltage", "values"), row("target.state", "state"),
                  row("wave:0", "logic"), row("rtos.task:1", "intervals"), row("annotation.mark", "points")];
    expect(cursorRows(rows)).toEqual(["aux.voltage", "target.state"]);
  });

  it("値は普通の表記と単位、状態はその名前。無ければ空", () => {
    expect(cursorText(item({ value: 3.3, unit: "V" }))).toBe("3.3 V");
    expect(cursorText(item({ value: 1e-7 }))).toBe("0.0000001");
    expect(cursorText(item({ type: "state", label: "HALTED" }))).toBe("HALTED");
    expect(cursorText(item({ type: "state", label: null, value: 2 }))).toBe("2");
    expect(cursorText(null)).toBe("");
  });

  it("問い合わせは同時に 1 つ。動き続けても最後の位置で答え、外れたら空にする", async () => {
    const asked: (number | null)[] = [];
    let release!: () => void;
    const results: Record<string, PickedItem>[] = [];
    const probe = new CursorProbe(async (at) => {
      asked.push(at);
      if (asked.length === 1) await new Promise<void>((r) => (release = r));
      return { "aux.voltage": item({ value: at }) };
    }, (m) => results.push(m));
    probe.ask(1);
    probe.ask(2);
    probe.ask(3);
    await settle();
    release();
    await settle();
    expect(asked).toEqual([1, 3]);                         // 途中の 2 は問い合わせない
    expect(results.at(-1)!["aux.voltage"].value).toBe(3);
    probe.ask(null);
    await settle();
    expect(results.at(-1)).toEqual({});
    expect(asked).toEqual([1, 3]);                         // 外れたときは問い合わせない
  });
});

describe("TimelineState の縦線の値と「時刻へ」", () => {
  it("valuesAt: 見えている値・状態の段の、その時刻に保たれている値", async () => {
    const { conn, tl } = setup();
    tl.setWidth(300);
    tl.setSource("recording");
    conn.link!("open", {});
    await settle();
    const values = await tl.valuesAt(4300);
    expect(values["target.state"]).toMatchObject({ label: "HALTED" });
    expect(values["aux.voltage"]).toMatchObject({ value: expect.any(Number), unit: "V" });
    expect(Object.values(values).every((v) => v.type === "values" || v.type === "state")).toBe(true);
    tl.setRowShown("target.state", false);                 // 隠した段は問い合わせない
    expect((await tl.valuesAt(4300))["target.state"]).toBeUndefined();
    tl.dispose();
  });

  it("goTo: 始めからの秒の時刻を選び、見えるように。範囲の外は端に収める", async () => {
    const { conn, tl, get } = setup();
    tl.setWidth(300);
    tl.setSource("recording");
    conn.link!("open", {});
    await settle();
    tl.zoomAt(0, 0.1);                                     // 0..2000 を見る
    await settle();
    expect(tl.goTo(0.005)).toBe(true);                     // 5 ms = タイムベース 5000（1 µs）
    expect(get().selection).toBe(5000);
    expect(get().view.start).toBeLessThanOrEqual(5000);
    expect(get().view.end).toBeGreaterThan(5000);
    expect(tl.goTo(99)).toBe(true);
    expect(get().selection).toBe(19999);
    tl.dispose();
  });

  it("goTo: データが無ければ何もしない", () => {
    const { tl } = setup();
    expect(tl.goTo(0.001)).toBe(false);
    tl.dispose();
  });
});

describe("頼まれずに隠したピンの段（変化の無いピン）", () => {
  it("隠した段を覚えて知らせ、ワンクリックで見せ直す（前から隠していた段は見せない）", () => {
    const { tl, get } = setup();
    tl.setConfig({ ...get().config, hidden: ["wave:1"] });
    tl.arrangeForSystem([1, 2, 3]);
    expect(get().config.hidden).toEqual(expect.arrayContaining(["wave:1", "wave:2", "wave:3"]));
    expect(get().autoHidden).toEqual(["wave:2", "wave:3"]);
    tl.showAutoHidden();
    expect(get().config.hidden).toEqual(["wave:1"]);
    expect(get().autoHidden).toEqual([]);
    tl.dispose();
  });
});
