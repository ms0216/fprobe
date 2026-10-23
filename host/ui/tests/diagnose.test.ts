// 接続の診断（lib/diagnose.ts）: target.diagnose と probe.pins.read の事実を、接続のダイアログに並べる形に
import { describe, expect, it } from "vitest";
import { diagnose, pinsText } from "../src/lib/diagnose";
import { FakeConn } from "./fake-conn";

const REPORT = {
  ok: false, verdict: "DP が応答しない", frequency_hz: 1000000, suggested_connect: null,
  steps: [{ name: "power", status: "ok", message: "3.3 V" }, { name: "swd", status: "fail", message: "ACK が無い" },
          { name: "dp", status: "skip", message: "" }],
};

describe("diagnose", () => {
  it("診断とピンを読み、手順をそのまま返す（並べ替えない）", async () => {
    const conn = new FakeConn({ "target.diagnose": REPORT, "probe.pins.read": { swclk: 1, swdio: 0, nreset: 1, not_reported: ["tdi"] } });
    const r = await diagnose(conn, 4_000_000);
    expect(conn.calls).toEqual([["target.diagnose", { frequency_hz: 4_000_000 }], ["probe.pins.read", {}]]);
    expect(r.report?.steps.map((s) => s.status)).toEqual(["ok", "fail", "skip"]);
    expect(r.pins).toEqual({ swclk: 1, swdio: 0, nreset: 1, not_reported: ["tdi"] });
    expect(r.error).toBeNull();
  });

  it("診断が失敗したら誤りの文。ピンだけでも読めれば返す", async () => {
    const conn = new FakeConn({ "target.diagnose": () => { throw new Error("RuntimeError: 接続中は診断しない"); },
                                "probe.pins.read": { swclk: 0, swdio: 0, nreset: 0 } });
    const r = await diagnose(conn, 1_000_000);
    expect(r.report).toBeNull();
    expect(r.error).toBe("接続中は診断しない");
    expect(r.pins?.nreset).toBe(0);
  });
});

describe("pinsText", () => {
  it("ピンの値を名前つきで並べる", () => {
    expect(pinsText({ swclk: 1, swdio: 0, nreset: 1 })).toBe("SWCLK 1 · SWDIO 0 · nRESET 1");
  });
});

describe("mhzToHz（接続の周波数の欄）", async () => {
  const { mhzToHz } = await import("../src/lib/diagnose");
  it("正の数だけ Hz に。数でない・0 以下は null（デーモンに送らない）", () => {
    expect(mhzToHz("4")).toBe(4_000_000);
    expect(mhzToHz(" 0.5 ")).toBe(500_000);
    expect(mhzToHz("x")).toBeNull();
    expect(mhzToHz("0")).toBeNull();
    expect(mhzToHz("")).toBeNull();
  });
});
