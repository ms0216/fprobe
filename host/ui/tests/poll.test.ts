// パネルの読み続け（lib/poll.ts）: 要求を積もらせない・かかった時間に合わせて間をあける・読み込み中と誤りを出す
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RpcError, E_NO_DATA, isNoData } from "../src/lib/api";
import { Poller, type PollState } from "../src/lib/poll";

/** 手で終わらせる読み（resolve / reject を順に覚える） */
function manual() {
  const calls: { resolve: () => void; reject: (e: Error) => void }[] = [];
  const read = () => new Promise<void>((resolve, reject) => calls.push({ resolve, reject }));
  return { read, calls };
}

describe("Poller", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("読んでいる最中に頼まれても重ねて出さない（終わった後に 1 回だけ読み直す）", async () => {
    const { read, calls } = manual();
    const p = new Poller(read);
    const first = p.refresh();
    p.refresh();
    p.refresh();
    expect(calls).toHaveLength(1);
    calls[0].resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toHaveLength(2);                      // 3 回頼まれても、後の読みは 1 回
    calls[1].resolve();
    await first;                                        // 最初の約束は後の読みが終わるまで待つ
    expect(calls).toHaveLength(2);
    p.dispose();
  });

  it("周期の読みは前の読みが終わってから、かかった時間に合わせて間をあける", async () => {
    const { read, calls } = manual();
    const p = new Poller(read, { minMs: 1000, factor: 2, maxMs: 10000 });
    p.poll(true);
    expect(calls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(3000);           // 読みに 3 秒かかった: その間は次を出さない
    expect(calls).toHaveLength(1);
    calls[0].resolve();
    await vi.advanceTimersByTimeAsync(5999);           // 次は 2 × 3 秒 = 6 秒後
    expect(calls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(calls).toHaveLength(2);
    calls[1].resolve();                                 // 速い読み: 最短の 1 秒
    await vi.advanceTimersByTimeAsync(1000);
    expect(calls).toHaveLength(3);
    p.poll(false);
    calls[2].resolve();
    await vi.advanceTimersByTimeAsync(20000);
    expect(calls).toHaveLength(3);                      // 止めたら次を出さない
    p.dispose();
  });

  it("読み込み中・読めた・誤りを状態に出す（誤りを黙って捨てない。次に読めたら消える）", async () => {
    const { read, calls } = manual();
    const p = new Poller(read);
    const seen: PollState[] = [];
    p.subscribe((s) => seen.push(s));
    expect(seen.at(-1)).toEqual({ loading: false, loaded: false, error: null });
    void p.refresh();
    expect(seen.at(-1)).toEqual({ loading: true, loaded: false, error: null });
    calls[0].reject(new RpcError(-32020, "RuntimeError: 読めない", { exception: "RuntimeError", detail: "読めない" }));
    await vi.advanceTimersByTimeAsync(0);
    expect(seen.at(-1)).toEqual({ loading: false, loaded: false, error: "読めない" });
    void p.refresh();
    calls[1].resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(seen.at(-1)).toEqual({ loading: false, loaded: true, error: null });
    p.dispose();
  });

  it("捨てた後は読み直さない", async () => {
    const { read, calls } = manual();
    const p = new Poller(read);
    p.poll(true);
    p.refresh();
    p.dispose();
    calls[0].resolve();
    await vi.advanceTimersByTimeAsync(10000);
    expect(calls).toHaveLength(1);
  });
});

describe("まだ無い（E_NO_DATA）", () => {
  it("取っていない・開いていないは、読めなかった誤りと分ける", () => {
    expect(isNoData(new RpcError(E_NO_DATA, "NoData: プロファイルが無い"))).toBe(true);
    expect(isNoData(new RpcError(-32020, "RuntimeError: 読めない"))).toBe(false);
    expect(isNoData(new Error("x"))).toBe(false);
  });
});
