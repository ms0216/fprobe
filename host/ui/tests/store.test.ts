import { describe, expect, it } from "vitest";
import { addListener } from "../src/lib/store";

describe("addListener", () => {
  it("足すとすぐ今の値を 1 回渡し、やめる関数で外れる", () => {
    const listeners = new Set<(v: number) => void>();
    const seen: number[] = [];
    const stop = addListener(listeners, (v) => seen.push(v), 1);
    expect(seen).toEqual([1]);
    expect(listeners.size).toBe(1);
    listeners.forEach((fn) => fn(2));
    stop();
    expect(listeners.size).toBe(0);
    listeners.forEach((fn) => fn(3));
    expect(seen).toEqual([1, 2]);
  });
});
