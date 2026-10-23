// 波形のタブの状態（TimelineState）と共通の計算。タイルは固定データ（tests/fixtures、Python が作った本物のフレーム）を
// 偽の接続で返し、間引きはその場で（InlineDecimator。Worker と同じ関数）。
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { AppState } from "../src/lib/state";
import { channelChoices, channelName, levelAt, panView, rateChoices, setChannelLayout, ticks, wheelFactor, zoomView } from "../src/lib/waveform";
import { InlineDecimator } from "../src/lib/timeline/decimator";
import { decodeFrame } from "../src/lib/timeline/frame";
import { TimelineState, type TimelineSnapshot } from "../src/lib/timeline/state";
import { FakeConn } from "./fake-conn";

describe("表示の区間の計算", () => {
  it("拡大縮小はマウスの位置を動かさない", () => {
    const v = zoomView({ start: 0, end: 1000 }, 0.5, 0.25, [0, 1000], 10);     // 250 の位置で 2 倍に拡大
    expect(v).toEqual({ start: 125, end: 625 });
    expect(zoomView(v, 2, 0.25, [0, 1000], 10)).toEqual({ start: 0, end: 1000 });
  });

  it("拡大の限界と、範囲の外へははみ出さない（範囲は 0 から始まらなくてよい）", () => {
    expect(zoomView({ start: 0, end: 100 }, 0.01, 0.5, [0, 1000], 10)).toEqual({ start: 45, end: 55 });
    expect(zoomView({ start: 900, end: 1000 }, 4, 1, [0, 1000], 10)).toEqual({ start: 600, end: 1000 });
    expect(zoomView({ start: 0, end: 1000 }, 4, 0.5, [0, 1000], 10)).toEqual({ start: 0, end: 1000 });
    expect(zoomView({ start: 5000, end: 6000 }, 4, 0, [5000, 7000], 10)).toEqual({ start: 5000, end: 7000 });
  });

  it("移動は端で止まる", () => {
    expect(panView({ start: 100, end: 200 }, 50, [0, 1000])).toEqual({ start: 150, end: 250 });
    expect(panView({ start: 100, end: 200 }, -500, [0, 1000])).toEqual({ start: 0, end: 100 });
    expect(panView({ start: 100, end: 200 }, 5000, [0, 1000])).toEqual({ start: 900, end: 1000 });
    expect(panView({ start: 100, end: 200 }, -500, [50, 1000])).toEqual({ start: 50, end: 150 });
  });

  it("チャンネルに名前を付ける", () => {
    expect([0, 7, 8, 9, 12].map(channelName)).toEqual(["ch0", "ch7", "SWDIO drive", "DAP busy", "ch12"]);   // 既定は英語
  });

  it("チャンネルの名前と選択肢はデバイスの申告（la_layout）に従う（U22）", () => {
    const layout = [...Array.from({ length: 8 }, (_, i) => ({ channel: i, kind: "pin", index: i })),
                    { channel: 8, kind: "internal", index: 0 }, { channel: 9, kind: "internal", index: 1 },
                    { channel: 10, kind: "input", index: 0 }, { channel: 11, kind: "input", index: 1 },
                    ...[12, 13, 14, 15].map((c) => ({ channel: c, kind: "none", index: 0 }))];
    try {
      setChannelLayout(layout);
      expect([7, 8, 10, 11].map(channelName)).toEqual(["ch7", "SWDIO drive", "in0", "in1"]);
      expect(channelChoices({ la_channels: 16, la_layout: layout })).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
    } finally {
      setChannelLayout(null);
    }
    expect(channelName(10)).toBe("ch10");
    // 並びを渡せば、それで名前を付ける（画面は申告が後から届いても描き直す: 申告を引数で依存させる）
    expect(channelName(10, layout)).toBe("in0");
    // 申告の無い古いデバイスは ch 0〜7 = ピン、8・9 = 内部
    expect(channelChoices({ la_channels: 16 })).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
    expect(channelChoices({})).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
  });

  it("1・2・5 の切りのよい間隔で、トリガを 0 とした時刻の目盛りを付ける", () => {
    // 40 ns/サンプル、トリガ = 100、表示 0..1000 サンプル（-4 µs .. 36 µs、幅 40 µs → 8 目盛りなら 5 µs おき）
    const t = ticks({ start: 0, end: 1000 }, 40e-9, 100, 8);
    expect(t.map((x) => x.label)).toEqual(["0", "5 µs", "10 µs", "15 µs", "20 µs", "25 µs", "30 µs", "35 µs"]);
    expect(t[0].sample).toBe(100);
    const neg = ticks({ start: 0, end: 1000 }, 40e-9, 500, 4);     // -20 µs .. 20 µs → 10 µs おき
    expect(neg.map((x) => x.label)).toEqual(["-20 µs", "-10 µs", "0", "10 µs", "20 µs"].slice(0, neg.length));
  });

  it("速度の選択肢はボードの最高速度を整数で割った値", () => {
    expect(rateChoices(100e6).slice(0, 4)).toEqual([100e6, 50e6, 25e6, 20e6]);
    expect(rateChoices(48e6)[0]).toBe(48e6);
    expect(rateChoices(0)).toEqual([]);
  });

  it("ホイールの量に比例して拡大し、横向きのスクロールでは拡大しない", () => {
    expect(wheelFactor(0)).toBe(1);
    expect(wheelFactor(100)).toBeGreaterThan(1);
    expect(wheelFactor(-100)).toBeCloseTo(1 / wheelFactor(100));
    expect(wheelFactor(4)).toBeLessThan(1.05);
  });
});

/** 固定データのフレームを結果の形で（id を変えた複写。区画は共有） */
function fixture(name: string) {
  const bytes = readFileSync(resolve(import.meta.dirname, "fixtures", name));
  return () => decodeFrame(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer).result;
}
const captureRaw = fixture("capture-raw.bin");          // 6 ch、20000 サンプル、生データ
const captureSummary = fixture("capture-summary.bin");  // 同じ捕捉の要約（bin 16）
const recordingRaw = fixture("recording-raw.bin");      // 記録: 2 つのかたまり、3 段の出来事

function setup(livePollMs?: number) {
  let id = 1;
  let summary = false;                                   // true なら要約だけを返す（取り直しを起こす）
  const conn = new FakeConn({
    "session.status": { boot: "a", device: null, control: { paused: false, paused_by: null, held: 0 }, clients: [], target: null },
    "session.activity.list": { entries: [] },
    "probe.info": { capabilities: {} },
    "timeline.tiles": (p: any) => {
      if (p.source === "recording") return recordingRaw();
      const r = summary ? captureSummary() : captureRaw();
      r.capture = { ...r.capture, id };
      return r;
    },
    "recording.status": { recording: false, name: "rec-1", span: [0, 19999], timebase_hz: 1e6 },
    "la.capture": () => {
      id += 1;
      return {};
    },
  });
  const app = new AppState(conn as any);
  const tl = new TimelineState(conn as any, app, new InlineDecimator(), livePollMs);
  let snap!: TimelineSnapshot;
  tl.subscribe((s) => (snap = s));
  return { conn, tl, get: () => snap, bump: () => (id += 1), summarize: (on: boolean) => (summary = on) };
}

const settle = async () => {
  for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0));
};
const tileCalls = (conn: FakeConn) => conn.calls.filter(([m]) => m === "timeline.tiles");

describe("TimelineState", () => {
  it("幅が決まりつながったら全体を読み、間引いて描く。新しい捕捉では全体に戻す", async () => {
    const { conn, tl, get, bump } = setup();
    tl.setWidth(500);
    conn.link!("open", {});
    await settle();
    expect(get().capture?.id).toBe(1);
    expect(get().view).toEqual({ start: 0, end: 20000 });
    expect(get().extent).toEqual([0, 20000]);
    expect(tileCalls(conn).at(-1)?.[1]).toMatchObject({ source: "capture", t0: 0, t1: null });
    expect(get().frame?.ids.filter((id) => id.startsWith("wave:")).length).toBe(6);
    expect(get().frame?.width).toBe(500);
    expect(get().decimator).toBe("inline");
    const reads = tileCalls(conn).length;
    tl.zoomAt(0.5, 0.5);
    await settle();
    expect(get().view).toEqual({ start: 5000, end: 15000 });
    expect(get().frame).toMatchObject({ v0: 5000, v1: 15000 });
    expect(tileCalls(conn).length).toBe(reads);              // 生データのタイルで足りる: デーモンに頼まない
    bump();
    conn.emit("activity", { seq: 1, method: "la.deep.capture", client: "claude" });
    await settle();
    expect(get().capture?.id).toBe(2);
    expect(get().view).toEqual({ start: 0, end: 20000 });
    expect(get().capturedBy).toBe("claude");
  });

  it("捕捉が無ければ誤りにせず、空のまま", async () => {
    const { conn, tl, get } = setup();
    conn.responders["timeline.tiles"] = () => {
      throw new Error("RuntimeError: 捕捉が無い（la_capture / la_deep_capture を先に）");
    };
    tl.setWidth(500);
    conn.link!("open", {});
    await settle();
    expect(get().capture).toBeNull();
    expect(get().error).toBeNull();
    expect(get().loading).toBe(false);
  });

  it("要約が粗くなるまで拡大したら、視点の前後を細かく読み直す（続けて動かしても読みは重ならない）", async () => {
    const { conn, tl, get, summarize } = setup();
    summarize(true);
    tl.setWidth(100);
    conn.link!("open", {});
    await settle();
    expect(get().header?.wave.mode).toBe("summary");
    const before = tileCalls(conn).length;
    summarize(false);
    for (let i = 0; i < 4; i++) tl.zoomAt(0.5, 0.5);         // 20000 → 1250（1 画素 12.5 < bin 16）
    await settle();
    const reads = tileCalls(conn).slice(before);
    expect(reads.length).toBeGreaterThanOrEqual(1);
    expect(reads.length).toBeLessThanOrEqual(2);
    const last = reads.at(-1)![1];
    const span = get().view.end - get().view.start;
    expect(last.t0).toBe(Math.max(0, get().view.start - span));
    expect(last.bin).toBe(Math.floor(span / 100 / 2));
    expect(get().header?.wave.mode).toBe("raw");
  });

  it("画面から捕捉する", async () => {
    const { conn, tl, get } = setup();
    tl.setWidth(500);
    conn.link!("open", {});
    await settle();
    await tl.capture({ rate_hz: 25e6, pretrigger: 512, fall: 8, deep: false });
    await settle();
    expect(conn.calls.find(([m]) => m === "la.capture")?.[1]).toEqual({ rate_hz: 25e6, pretrigger: 512, fall: 8, rise: 0 });
    expect(get().capture?.id).toBe(2);
  });

  it("クロストリガだけで捕捉する（FPGA の条件など。値は 1 かつ立ち下がりの ch0 = 決して成立しないパターン）", async () => {
    const { conn, tl } = setup();
    tl.setWidth(500);
    conn.link!("open", {});
    await settle();
    conn.responders["la.deep.capture"] = { trigger_time: 12345, trigger_index: 7 };
    await tl.capture({ rate_hz: 25e6, pretrigger: 512, deep: false, crossOnly: true });
    expect(await tl.capture({ rate_hz: 25e6, pretrigger: 0, deep: true, crossOnly: true, rle: false }))
      .toMatchObject({ trigger_time: 12345 });                     // 呼び出し元がトリガの時刻（記録の時間軸）を使う
    expect(conn.calls.find(([m]) => m === "la.capture")?.[1]).toEqual({ rate_hz: 25e6, pretrigger: 512, mask: 1, value: 1, fall: 1, rise: 0 });
    expect(conn.calls.find(([m]) => m === "la.deep.capture")?.[1]).toMatchObject({ mask: 1, value: 1, fall: 1, rise: 0, rle: false });
  });

  it("捕捉の読み込みの途中で記録へ替えても、遅れて届いた捕捉の結果を記録に当てない（記録の時刻へ飛べる）", async () => {
    const { conn, tl, get } = setup();
    tl.setWidth(500);
    conn.link!("open", {});
    await settle();
    const tiles = conn.responders["timeline.tiles"];
    let release!: () => void;
    conn.responders["timeline.tiles"] = (p: any) => p.source === "capture"
      ? new Promise((resolve) => { release = () => resolve(tiles(p)); }) : tiles(p);
    tl.reload();                                                  // 捕捉を読み直している途中で…
    tl.reveal({ source: "recording", t: 10000, tEnd: 10100 });    // …記録の時刻へ
    release();
    await settle();
    expect(get().source).toBe("recording");
    expect(get().extent).toEqual([0, 20000]);
    expect(get().view.start).toBeGreaterThan(9000);               // 全体（0〜20000）に戻らない
    expect(get().view.end).toBeLessThan(11000);
  });

  it("伸びている記録の、まだ読んでいない時刻へ飛ぶなら、範囲を読み直してから飛ぶ（端に切り詰めない）", async () => {
    const { conn, tl, get } = setup();
    tl.setWidth(500);
    conn.link!("open", {});
    tl.setSource("recording");
    await settle();
    expect(get().extent).toEqual([0, 20000]);
    conn.responders["recording.status"] = { recording: true, name: "rec-1", span: [0, 39999], timebase_hz: 1e6 };
    tl.reveal({ source: "recording", t: 30000, tEnd: 30100 });     // 記録中に捕捉した直後（範囲がまだ古い）
    await settle();
    expect(get().selection).toBe(30000);
    expect(get().view.start).toBeGreaterThan(29000);
  });

  it("記録中、捕捉の後の読み直しと同時に記録の時刻へ飛んでも、その範囲を表示する（全体・追従に戻らない）", async () => {
    const { conn, tl, get } = setup();
    tl.setWidth(500);
    conn.link!("open", {});
    conn.responders["recording.status"] = { recording: true, name: "rec-1", span: [0, 19999], timebase_hz: 1e6 };
    tl.setSource("recording");
    await settle();
    tl.reload();                                                   // 捕捉が終わって読み直す（記録にも入った）
    tl.reveal({ source: "recording", t: 18000, tEnd: 18100 });
    await settle();
    expect(get().selection).toBe(18000);
    expect(get().view.end - get().view.start).toBeLessThan(1000);
  });

  it("つなぎ直しても捕捉が同じなら、拡大していた表示を保つ", async () => {
    const { conn, tl, get } = setup();
    tl.setWidth(500);
    conn.link!("open", {});
    await settle();
    tl.zoomAt(0.5, 0.5);
    await settle();
    conn.link!("closed");
    conn.link!("open", {});
    await settle();
    expect(get().view).toEqual({ start: 5000, end: 15000 });
    expect(get().frame).toMatchObject({ v0: 5000, v1: 15000 });
  });

  it("読みが失敗しても、溜まっていた最後の操作は読み直す", async () => {
    const { conn, tl, get, summarize } = setup();
    summarize(true);
    tl.setWidth(100);
    conn.link!("open", {});
    await settle();
    const ok = conn.responders["timeline.tiles"];
    let fail = true;
    summarize(false);
    conn.responders["timeline.tiles"] = (p: any) => {
      if (fail) {
        fail = false;
        throw new Error("一時的な誤り");
      }
      return ok(p);
    };
    for (let i = 0; i < 4; i++) tl.zoomAt(0.5, 0.5);
    await settle();
    tl.panBy(0.1);
    await settle();
    expect(get().header?.wave.mode).toBe("raw");
    expect(get().error).toBeNull();
  });

  it("カーソルの値を間引いたフレームから引く", async () => {
    const { conn, tl, get } = setup();
    tl.setWidth(20000);
    conn.link!("open", {});
    await settle();
    const f = get().frame!;
    expect(["0", "1"]).toContain(levelAt(f, "wave:0", 0));
    expect(levelAt(f, "wave:0", 20000)).toBeUndefined();            // 表示の外
    expect(levelAt(f, "wave:9", 10)).toBeUndefined();               // 無い段
  });

  it("記録を表示する: 記録の範囲と出来事の段", async () => {
    const { conn, tl, get } = setup();
    tl.setWidth(400);
    conn.link!("open", {});
    await settle();
    tl.setSource("recording");
    await settle();
    expect(get().recording).toBe("rec-1");
    expect(get().extent).toEqual([0, 20000]);
    expect(get().header?.lanes.map((l) => l.stream)).toEqual(["dap", "rtt", "uart"]);
    const f = get().frame!;
    expect(f.ids.filter((id) => id.startsWith("group:"))).toEqual(["group:logic", "group:dap", "group:rtt", "group:uart"]);
    expect(f.visible.reduce((a, b) => a + b, 0)).toBeGreaterThan(0);
    // 次の出来事を選び、基準からの差を測る。段の構成（畳む・隠す）は描く段の並びに出る
    tl.setReference(0);
    expect(await tl.seek(1)).toBe(true);
    expect(get().selection).toBeGreaterThan(0);
    expect(get().selected?.t).toBe(get().selection);
    tl.toggleFold("dap");
    expect(get().rows.filter((r) => r.def.group === "dap").length).toBe(1);
    const row = get().rows.find((r) => r.def.group === "uart" && !r.header)!.def.id;
    tl.setRowShown(row, false);
    expect(get().rows.some((r) => r.def.id === row)).toBe(false);
    tl.resetRows();
    expect(get().rows.some((r) => r.def.id === row)).toBe(true);
    expect(get().capture).toBeNull();
  });

  it("ほかのビューから時刻へ飛ぶ（reveal）: 源が違えば変えて、届いてから選ぶ。区間は見えるように、µs は換算", async () => {
    const { conn, tl, get } = setup();
    tl.setWidth(400);
    conn.link!("open", {});
    await settle();
    tl.reveal({ source: "recording", t: 1234 });
    await settle();
    expect(get().source).toBe("recording");
    expect(get().selection).toBe(1234);
    tl.reveal({ source: "recording", t: 5000, tEnd: 6000 });
    expect(get().view).toEqual({ start: 4500, end: 6500 });
    expect(get().selection).toBe(5000);
    tl.reveal({ source: "recording", us: 7000 });              // 記録の 1 単位 = 1 µs（tick_s）
    expect(get().selection).toBe(Math.round(7000e-6 / get().tickS));
    tl.reveal({ source: "recording", t: 99999 });              // 範囲の外は端に収める
    await settle();                                            // 読み直しても範囲に無ければ端
    expect(get().selection).toBe(19999);
  });

  it("ビューの印: owner ごとに置き換え、t の昇順。空で消す", () => {
    const { tl, get } = setup();
    tl.setMarks("eventLog", "recording", [{ t: 30 }, { t: 10, label: "a" }]);
    tl.setMarks("decode", "capture", [{ t: 5, tEnd: 9, tone: 1 }]);
    expect(get().marks.find((m) => m.owner === "eventLog")?.marks.map((m) => m.t)).toEqual([10, 30]);
    tl.setMarks("eventLog", "recording", [{ t: 1 }]);
    expect(get().marks.find((m) => m.owner === "eventLog")?.marks).toEqual([{ t: 1 }]);
    tl.setMarks("eventLog", "recording", []);
    expect(get().marks.map((m) => m.owner)).toEqual(["decode"]);
    // 同じ印の置き直しは知らせない（時間軸を読む $effect が印を置いても続かない）
    let told = 0;
    const stop = tl.subscribe(() => told++);
    told = 0;
    tl.setMarks("decode", "capture", [{ t: 5, tEnd: 9, tone: 1 }]);
    tl.setMarks("nobody", "recording", []);
    expect(told).toBe(0);
    stop();
  });

  it("時刻 0 は記録・捕捉の始め（捕捉はトリガでなくサンプル 0）。時間軸が捕捉でも記録の 0 を持つ", async () => {
    const { conn, tl, get } = setup();
    tl.setWidth(400);
    conn.link!("open", {});
    await settle();
    expect(get().source).toBe("capture");
    expect(tl.origin()).toBe(0);
    expect(get().origins.capture).toEqual({ t0: 0, tickS: get().tickS });
    expect(get().origins.recording).toEqual({ t0: 0, tickS: 1e-6 });
    const seen: unknown[] = [];
    const stop = tl.origins.subscribe((o) => seen.push(o));
    tl.zoomAt(0.5, 0.5);                                         // 視点が動いても 0 の store は配らない
    await settle();
    expect(seen).toHaveLength(1);
    stop();
  });

  it("ほかの利用者が記録を開いた・始めた・止めた（通知 recording）: 0 を読み直し、記録を見ていれば読み直す", async () => {
    const { conn, tl, get } = setup();
    let span = [0, 19999];
    conn.responders["recording.status"] = () => ({ recording: true, name: "rec-2", span, timebase_hz: 1e6 });
    tl.setWidth(400);
    conn.link!("open", {});
    await settle();
    span = [500, 19999];
    let tiles = tileCalls(conn).length;
    conn.emit("recording", { action: "start", name: "rec-2" });
    await settle();
    expect(get().origins.recording?.t0).toBe(500);
    expect(tileCalls(conn).length).toBe(tiles);                  // 捕捉を見ている: 時間軸は読み直さない
    tl.setSource("recording");
    await settle();
    tiles = tileCalls(conn).length;
    conn.emit("recording", { action: "stop", name: "rec-2" });
    await settle();
    expect(tileCalls(conn).length).toBe(tiles + 1);
    conn.emit("activity", { method: "recording.mark", client: "claude" });   // 印を置いた: 印の段を読み直す
    await settle();
    expect(tileCalls(conn).length).toBe(tiles + 2);
    conn.emit("activity", { method: "fpga_obs.signals_set", client: "claude" });   // FPGA の信号の名前を変えた: 段の名前
    await settle();
    expect(tileCalls(conn).length).toBe(tiles + 3);
    conn.emit("activity", { method: "recording.find", client: "claude" });   // 読みでは読み直さない
    await settle();
    expect(tileCalls(conn).length).toBe(tiles + 3);
    expect(conn.quiet.filter((m) => m === "timeline.tiles").length).toBe(tileCalls(conn).length);
  });

  it("記録中、全体の読み込みが重ければ読み直しの間を空ける（前の読みの 2 倍。重い記録で読み込みが途切れなく続かない）", async () => {
    const { conn, tl } = setup(5);
    let end = 19999;
    conn.responders["recording.status"] = () => ({ recording: true, name: "rec-live", span: [0, (end += 1000)], timebase_hz: 1e6 });
    const tiles = conn.responders["timeline.tiles"];
    conn.responders["timeline.tiles"] = (p: any) => new Promise((r) => setTimeout(() => r(tiles(p)), 40));   // 1 回 40 ms
    tl.setWidth(400);
    conn.link!("open", {});
    tl.setSource("recording");
    await new Promise((r) => setTimeout(r, 400));
    const reads = conn.calls.filter(([m, p]) => m === "timeline.tiles" && p.source === "recording").length;
    expect(reads).toBeLessThanOrEqual(5);                       // 間を空けなければ 400 / (40 + 5) ≒ 9 回
  });

  it("記録中は伸びていく端を追いかける。利用者が戻って見ているときは追わない（M8）", async () => {
    const { conn, tl, get } = setup(5);
    let span = [0, 19999], recording = true;
    conn.responders["recording.status"] = () => ({ recording, name: "rec-live", span, timebase_hz: 1e6 });
    tl.setWidth(400);
    conn.link!("open", {});
    await settle();
    tl.setSource("recording");
    await settle();
    expect(get()).toMatchObject({ live: true, follow: true, view: { start: 0, end: 20000 } });
    const wait = async (check: () => boolean) => {
      for (let i = 0; i < 100 && !check(); i++) await new Promise((r) => setTimeout(r, 5));
    };
    // 全体を見ていれば全体のまま伸びる
    span = [0, 39999];
    await wait(() => get().extent?.[1] === 40000);
    expect(get().view).toEqual({ start: 0, end: 40000 });
    // 端を拡大して見ていれば、幅を保って端へ動く
    tl.setView({ start: 30000, end: 40000 });
    expect(get().follow).toBe(true);
    span = [0, 59999];
    await wait(() => get().extent?.[1] === 60000);
    expect(get().view).toEqual({ start: 50000, end: 60000 });
    // 戻って見ていれば追わない。端へ戻ると（End・全体）また追う
    tl.panBy(-1);
    expect(get().follow).toBe(false);
    const looking = get().view;
    span = [0, 79999];
    await wait(() => get().extent?.[1] === 80000);
    expect(get().view).toEqual(looking);
    tl.toEdge(1);
    expect(get()).toMatchObject({ follow: true, view: { start: 70000, end: 80000 } });
    // 記録を止めたら読み続けない
    recording = false;
    span = [0, 89999];
    await wait(() => get().live === false);
    const reads = conn.calls.filter(([m]) => m === "recording.status").length;
    await new Promise((r) => setTimeout(r, 50));
    expect(conn.calls.filter(([m]) => m === "recording.status").length).toBe(reads);
    tl.dispose();
  });
});
