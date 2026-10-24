// 時刻からコードの位置へ（lib/codeat.ts）: recording.code_at の応答 → ソースの行の印・開くファイル・欠けの一覧、
// 時間軸の選択カーソルに合わせた問い合わせ（使う窓が無ければ呼ばない・最後の位置だけ・記録でなければ呼ばない）
import { describe, expect, it } from "vitest";
import { CodeAtLink, cursorKey, haltedAt, lineMarks, missingList, missingText, sampleText, signedDuration,
         type CodeAt, type CodeAtSnapshot } from "../src/lib/codeat";
import { EN, type Key } from "../src/lib/i18n";
import { FakeConn } from "./fake-conn";

const t = (k: Key, p: Record<string, string | number> = {}) => EN[k].replace(/\{(\w+)\}/g, (_, n) => String(p[n]));

const sample = (t: number, file: string | null, line: number | null, dt = 0) => ({
  t, dt_s: dt, value: 0x20000054, pc: 0x20000054, function: "tick", offset: 4, file, line,
  where: file ? `tick+4 (${file}:${line})` : null,
});

const response = (p: Partial<CodeAt> = {}): CodeAt => ({
  t: 1500, timebase_hz: 1e8, elf: "a.elf", recorded_elf: "a.elf", pc_before: sample(1000, "main.c", 17, -5e-6),
  pc_after: sample(2000, "main.c", 19, 5e-6), pc_gap_s: 1e-5, state: { t: 0, dt_s: -1.5e-5, uncertainty_s: null, state: "RUNNING" },
  halt_before: null, halt_after: null, task: null, missing: {}, ...p,
});

describe("応答 → 描く値", () => {
  it("ソースの行の印は直前・直後のサンプルの行（そのファイルだけ）", () => {
    const marks = lineMarks(response(), "main.c");
    expect([...marks]).toEqual([[17, ["before"]], [19, ["after"]]]);
    expect(lineMarks(response(), "other.c").size).toBe(0);
    expect(lineMarks(null, "main.c").size).toBe(0);
  });

  it("同じ行の直前と直後は両方の印。t で止まっていれば止まった場所の印も（走っていれば付けない）", () => {
    const halted = { t: 1200, dt_s: -3e-6, uncertainty_s: 1e-6, state: "HALTED", file: "main.c", line: 17, pc: 0x20000054 };
    const r = response({ pc_after: sample(2000, "main.c", 17), state: halted, halt_before: halted });
    expect(lineMarks(r, "main.c").get(17)).toEqual(["before", "after", "halt"]);
    expect(haltedAt(r)).toBe(halted);
    expect(haltedAt(response({ halt_before: halted }))).toBeNull();        // 止まったのは前で、t では走っていた
  });

  it("欠けは決まった順で、理由はデーモンの符号のまま", () => {
    const r = response({ missing: { task: { reason: "no_rtos" }, pc: { reason: "no_samples" } } });
    expect(missingList(r)).toEqual([{ key: "pc", reason: { reason: "no_samples" } }, { key: "task", reason: { reason: "no_rtos" } }]);
    expect(missingList(null)).toEqual([]);
  });

  it("欠けの文とサンプルの文（場所、無ければ PC、PC が無ければ理由）。表に無い理由は符号のまま", () => {
    expect(missingText(t, "task", { reason: "gap", detail: "overflow" })).toBe("The RTOS timeline has a gap here (overflow)");
    expect(missingText(t, "pc", { reason: "something_new" })).toBe("pc: something_new");
    expect(sampleText(t, sample(1, "main.c", 17))).toBe("tick+4 (main.c:17)");
    expect(sampleText(t, { ...sample(1, null, null), where: null })).toBe("PC 0x20000054");
    expect(sampleText(t, { ...sample(1, null, null), where: null, pc: null, reason: "pcsr_invalid" }))
      .toBe("PCSR 0xFFFFFFFF (core halted or not sampleable)");
    expect(sampleText(t, { ...sample(1, null, null), where: null, pc: null, reason: "read_failed", ack: 4 }))
      .toBe("read failed (ACK 4)");
  });

  it("差は符号つき", () => {
    expect(signedDuration(-5e-6, (s) => `${s * 1e6} µs`)).toBe("−5 µs");
    expect(signedDuration(2e-6, (s) => `${s * 1e6} µs`)).toBe("+2 µs");
  });
});

// --- 問い合わせ -------------------------------------------------------------------------------------

const settle = async () => {
  for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0));
};

function timeline() {
  let fn: ((s: any) => void) | null = null;
  return {
    subscribe(f: (s: any) => void) {
      fn = f;
      return () => (fn = null);
    },
    set(s: { source: "capture" | "recording"; recording: string | null; selection: number | null }) {
      fn?.(s);
    },
  };
}

describe("時間軸のカーソルに合わせた問い合わせ", () => {
  it("記録を見ていて選択カーソルがあるときだけ鍵がある（捕捉はサンプル番号なので問い合わせない）", () => {
    expect(cursorKey({ source: "recording", recording: "r", selection: 5 })).toBe("r@5");
    expect(cursorKey({ source: "capture", recording: null, selection: 5 })).toBeNull();
    expect(cursorKey({ source: "recording", recording: "r", selection: null })).toBeNull();
  });

  it("使う窓が無ければ呼ばない。使い始めたら今のカーソルで呼び、応答を配る", async () => {
    const conn = new FakeConn({ "recording.code_at": (p: any) => response({ t: p.t }) });
    const tl = timeline();
    const link = new CodeAtLink(conn as any, tl);
    let snap!: CodeAtSnapshot;
    link.subscribe((s) => (snap = s));
    tl.set({ source: "recording", recording: "r", selection: 1500 });
    await settle();
    expect(conn.calls).toEqual([]);
    const stop = link.use();
    await settle();
    expect(conn.calls).toEqual([["recording.code_at", { t: 1500 }]]);
    expect(conn.quiet).toEqual(["recording.code_at"]);                      // 読みは操作の記録に残さない
    expect(snap).toMatchObject({ t: 1500, loading: false, error: null });
    expect(snap.result?.t).toBe(1500);
    stop();
    tl.set({ source: "recording", recording: "r", selection: 1600 });
    await settle();
    expect(conn.calls.length).toBe(1);
  });

  it("問い合わせ中に動き続けたら、終わってから最後の位置で 1 回だけ。カーソルが外れたら空", async () => {
    const pending: ((v: CodeAt) => void)[] = [];
    const conn = new FakeConn({ "recording.code_at": () => new Promise<CodeAt>((r) => pending.push(r)) });
    const tl = timeline();
    const link = new CodeAtLink(conn as any, tl);
    let snap!: CodeAtSnapshot;
    link.subscribe((s) => (snap = s));
    link.use();
    tl.set({ source: "recording", recording: "r", selection: 1 });
    await settle();
    tl.set({ source: "recording", recording: "r", selection: 2 });
    tl.set({ source: "recording", recording: "r", selection: 3 });
    pending.shift()!(response({ t: 1 }));
    await settle();
    expect(conn.calls.map((c) => c[1].t)).toEqual([1, 3]);
    pending.shift()!(response({ t: 3 }));
    await settle();
    expect(snap.result?.t).toBe(3);
    tl.set({ source: "capture", recording: null, selection: 3 });
    await settle();
    expect(snap).toMatchObject({ t: null, result: null });
    expect(conn.calls.length).toBe(2);
  });

  it("誤りは応答の代わりに文字で（窓は誤りを出す）", async () => {
    const conn = new FakeConn({ "recording.code_at": () => { throw new Error("読む記録が無い"); } });
    const tl = timeline();
    const link = new CodeAtLink(conn as any, tl);
    let snap!: CodeAtSnapshot;
    link.subscribe((s) => (snap = s));
    link.use();
    tl.set({ source: "recording", recording: "r", selection: 7 });
    await settle();
    expect(snap.result).toBeNull();
    expect(snap.error).toContain("読む記録が無い");
  });
});
