// 止めると壊れるターゲットの安全: 止める前の警告（debug.halt_risk）と、走らせたままの致命的エラー（target.state の fatal）
import { describe, expect, it } from "vitest";
import { t } from "../src/lib/i18n";
import {
  Safety, clueText, conditionLines, confirmText, fatalTitle, haltRiskForm, haltRiskParams, isFatal, linkText, livenessForm,
  livenessParams, needsWarning, reasonText, type HaltRisk,
} from "../src/lib/safety";
import { AppState } from "../src/lib/state";
import { FakeConn } from "./fake-conn";

// 探す名前・正規表現は使う側が debug.halt_risk.set で渡したもの。画面は種類（無線・RTOS 等）を決めず、合った事実を描く
const RISK: HaltRisk = {
  elf: "/x/zmk.elf", names: ["_kernel"], patterns: ["^ll_"], matched: true, monitor_in_elf: false,
  matches: [{ query: "_kernel", by: "name", count: 1, symbols: [{ name: "_kernel", kind: "object", address: 0x20000000 }] },
            { query: "^ll_", by: "pattern", count: 12,
              symbols: [{ name: "ll_adv_enable", kind: "function", address: 0x1000 },
                        { name: "ll_conn_update", kind: "function", address: 0x1100 }] }],
};

const tick = () => new Promise((r) => setTimeout(r, 0));

function setup(risk: HaltRisk | null, answer = true) {
  const conn = new FakeConn({ "debug.halt_risk": () => { if (!risk) throw new Error("no elf"); return risk; } });
  const app = new AppState(conn as any);
  const asked: string[] = [];
  const safety = new Safety(conn as any, app, (text) => { asked.push(text); return answer; });
  return { conn, app, safety, asked };
}

describe("止める前の警告", () => {
  it("渡した名前が ELF に合い、モニタ方式でなければ警告する", () => {
    expect(needsWarning(RISK, { state: "RUNNING" })).toBe(true);
    expect(needsWarning(RISK, { state: "HALTED", debug_mode: "monitor" })).toBe(false);
    expect(needsWarning({ ...RISK, matched: false, matches: [] }, { state: "RUNNING" })).toBe(false);
    expect(needsWarning(null, { state: "RUNNING" })).toBe(false);
  });

  it("理由は渡した条件と合ったシンボルと数（種類の名前は付けない）で、止めずに調べる方法へ案内する", () => {
    expect(reasonText(RISK.matches[0], t)).toBe("_kernel");
    expect(reasonText(RISK.matches[1], t)).toContain("^ll_");
    expect(reasonText(RISK.matches[1], t)).toContain("ll_adv_enable");
    expect(reasonText(RISK.matches[1], t)).toContain("11");
    expect(reasonText(RISK.matches[1], t)).not.toMatch(/radio|無線/i);
    const text = confirmText(RISK, "halt", t);
    expect(text).toContain(t("safety.action.halt"));
    expect(text).toContain(t("safety.addMonitor"));
    expect(confirmText({ ...RISK, monitor_in_elf: true }, "step", t)).toContain(t("safety.useMonitor"));
  });

  it("ELF を読んだら見分け、止める操作の前に 1 度だけ確かめる（ELF ごと）", async () => {
    const { conn, app, safety, asked } = setup(RISK);
    conn.link!("open");
    await tick();
    expect(safety.snapshot.risk?.matched).toBe(true);
    expect(conn.quiet).toContain("debug.halt_risk");
    conn.emit("target.state", { state: "RUNNING" });
    expect(await safety.confirmHalt("halt", t)).toBe(true);
    expect(await safety.confirmHalt("breakpoint", t)).toBe(true);
    expect(asked.length).toBe(1);
    void app;
  });

  it("「いいえ」なら止めない。モニタ方式なら聞かない", async () => {
    const { conn, safety, asked } = setup(RISK, false);
    conn.link!("open");
    await tick();
    conn.emit("target.state", { state: "RUNNING" });
    expect(await safety.confirmHalt("halt", t)).toBe(false);
    conn.emit("target.state", { state: "RUNNING", debug_mode: "monitor" });
    expect(await safety.confirmHalt("halt", t)).toBe(true);
    expect(asked.length).toBe(1);
  });

  it("ELF が無ければ聞かない。ELF を読み直したら見分け直す", async () => {
    const { conn, safety, asked } = setup(null);
    conn.link!("open");
    await tick();
    expect(safety.snapshot.risk).toBeNull();
    expect(await safety.confirmHalt("step", t)).toBe(true);
    expect(asked).toEqual([]);
    conn.responders["debug.halt_risk"] = RISK;
    conn.emit("activity", { method: "debug.elf.load" });
    await tick();
    expect(safety.snapshot.risk?.elf).toBe("/x/zmk.elf");
  });

  it("探す条件を変えたら（debug.halt_risk.set）読み直す", async () => {
    const { conn, safety } = setup({ ...RISK, matched: false, matches: [] });
    conn.link!("open");
    await tick();
    expect(safety.snapshot.risk?.matched).toBe(false);
    conn.responders["debug.halt_risk"] = RISK;
    conn.emit("activity", { method: "debug.halt_risk.set" });
    await tick();
    expect(safety.snapshot.risk?.matched).toBe(true);
  });
});

describe("走らせたままの致命的エラー", () => {
  it("ティックが変わっていないだけ（tick_stopped）は致命的エラーと言わない。ロックアップと、使う側が渡した関数の中（pc_in）は言う", () => {
    expect(isFatal({ kind: "tick_stopped", since: 1, evidence: [] } as any)).toBe(false);
    for (const kind of ["lockup", "pc_in"]) expect(isFatal({ kind, since: 1, evidence: [] } as any)).toBe(true);
  });

  it("target.state の fatal を受け取り、見出しと手がかりを言葉にする", () => {
    const { conn, safety } = setup(null);
    const fatal = { kind: "pc_in", function: "arch_system_halt", since: 1700000000,
                    evidence: [{ clue: "pc_in", function: "arch_system_halt", valid: 4, pc_samples: 4 },
                               { clue: "tick_stopped", tick: 1234 }] };
    conn.emit("target.state", { state: "RUNNING", fatal });
    expect(safety.snapshot.fatal).toEqual(fatal);
    expect(fatalTitle(safety.snapshot.fatal!, t)).toContain("arch_system_halt");
    expect(clueText(fatal.evidence[1] as any, t)).toContain("1234");
    expect(clueText(fatal.evidence[0] as any, t)).toContain("4/4");
    conn.emit("target.state", { state: "HALTED", pc: 0 });
    expect(safety.snapshot.fatal).toBeNull();
  });
});

describe("プローブの USB のつなぎ直し（B28: target.state の link_lost）", () => {
  it("機能が変わっていれば、変わったものを前 → 後で並べる", () => {
    expect(linkText({ at: 1, changed: {} }, t)).toBe(t("safety.linkLost"));
    const text = linkText({ at: 1, changed: { jtag: [false, true], sampler: [true, false] } }, t);
    expect(text).toContain(t("safety.linkLost"));
    expect(text).toContain("jtag: false → true, sampler: true → false");
  });

  it("版が分かれば、版が変わったかを確実に言う（U49）", () => {
    const version = (changed: boolean | null, latest_build: boolean | null) =>
      ({ before: "aaaa", after: "bbbb", changed, latest_build });
    const changedFeatures = { jtag: [false, true] as [unknown, unknown] };
    expect(linkText({ at: 1, changed: changedFeatures, version: version(false, null) }, t)).toBe(t("safety.linkLost"));
    const flashed = linkText({ at: 1, changed: {}, version: version(true, false) }, t);
    expect(flashed).toContain(t("safety.linkVersion", { before: "aaaa", after: "bbbb" }));
    const latest = linkText({ at: 1, changed: {}, version: version(true, true) }, t);
    expect(latest).toContain(t("safety.linkVersionLatest", { before: "aaaa", after: "bbbb" }));
    const unknown = linkText({ at: 1, changed: changedFeatures, version: version(null, null) }, t);
    expect(unknown).toContain("jtag: false → true");
  });
});

describe("止める前の警告・走らせたままの見張りの条件の欄（人も AI と同じ条件を set できる）", () => {
  it("欄の 1 行が 1 つの条件（前後の空白と空の行は外す。正規表現の , は区切りにしない）", () => {
    expect(conditionLines(" _kernel \n\n^ll_(a|b),x\r\n")).toEqual(["_kernel", "^ll_(a|b),x"]);
    expect(conditionLines("")).toEqual([]);
  });

  it("今の条件（debug.halt_risk の names・patterns）を欄に描き、欄から debug.halt_risk.set の引数を組む", () => {
    expect(haltRiskForm(RISK)).toEqual({ names: "_kernel", patterns: "^ll_" });
    expect(haltRiskForm(null)).toBeNull();                       // 読めない（ELF が無い）: 画面に既定の名前を持たない
    expect(haltRiskParams({ names: "a\nb", patterns: "" })).toEqual({ names: ["a", "b"], patterns: [] });
  });

  it("今の見張りの条件（target.liveness.get）を欄に描く。PC の数が無ければ空（既定の数を画面に持たない）", () => {
    expect(livenessForm({ functions: ["arch_system_halt"], patterns: ["Fault"], pc_samples: 4 }))
      .toEqual({ functions: "arch_system_halt", patterns: "Fault", pcSamples: "4" });
    expect(livenessForm({ functions: [], patterns: [], pc_samples: null }))
      .toEqual({ functions: "", patterns: "", pcSamples: "" });
    expect(livenessForm(null)).toBeNull();
  });

  it("欄から target.liveness.set の引数: 条件があれば PC の数は必須（1 以上の整数）。条件が無ければ null", () => {
    expect(livenessParams({ functions: "arch_system_halt", patterns: "", pcSamples: "" })).toEqual({ error: "pc_samples" });
    expect(livenessParams({ functions: "", patterns: "Fault$", pcSamples: "0" })).toEqual({ error: "pc_samples" });
    expect(livenessParams({ functions: "", patterns: "Fault$", pcSamples: "1.5" })).toEqual({ error: "pc_samples" });
    expect(livenessParams({ functions: "arch_system_halt", patterns: "Fault$", pcSamples: " 8 " }))
      .toEqual({ params: { functions: ["arch_system_halt"], patterns: ["Fault$"], pc_samples: 8 } });
    expect(livenessParams({ functions: "", patterns: "", pcSamples: "8" }))
      .toEqual({ params: { functions: [], patterns: [], pc_samples: null } });
  });

  it("見張りの条件は接続したとき・誰か（AI も）が target.liveness.set したときに API から読み直す", async () => {
    const { conn, safety } = setup(null);
    conn.responders["target.liveness.get"] = { functions: [], patterns: [], pc_samples: null };
    conn.link!("open");
    await tick();
    expect(safety.snapshot.liveness).toEqual({ functions: [], patterns: [], pc_samples: null });
    expect(conn.quiet).toContain("target.liveness.get");
    conn.responders["target.liveness.get"] = { functions: ["arch_system_halt"], patterns: [], pc_samples: 4 };
    conn.emit("activity", { method: "target.liveness.set" });
    await tick();
    expect(safety.snapshot.liveness?.functions).toEqual(["arch_system_halt"]);
  });

  it("見張りの条件が読めなければ（古いデーモン）null", async () => {
    const { conn, safety } = setup(null);
    conn.responders["target.liveness.get"] = () => { throw new Error("unknown method"); };
    conn.link!("open");
    await tick();
    expect(safety.snapshot.liveness).toBeNull();
  });
});

describe("止める前の警告の条件（ELF に依らない）", () => {
  it("debug.halt_risk.get で今の条件を読む。ELF が無くても条件は描ける", async () => {
    const conn = new FakeConn({ "debug.halt_risk": () => { throw new Error("no elf"); },
                                "debug.halt_risk.get": { names: ["RADIO_IRQHandler"], patterns: [] } });
    const app = new AppState(conn as any);
    const safety = new Safety(conn as any, app, () => true);
    let snap: any = null;
    safety.subscribe((x: any) => (snap = x));
    await safety.reload();
    expect(snap.risk).toBeNull();
    expect(snap.haltConditions).toEqual({ names: ["RADIO_IRQHandler"], patterns: [] });
  });
});
