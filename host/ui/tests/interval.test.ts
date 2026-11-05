// components/common/interval.ts: $effect の後始末として返す周期の読み
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { interval, intervalNow } from "../src/components/common/interval";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test("interval はすぐには呼ばず、周期で呼び、後始末で止まる", () => {
  const fn = vi.fn();
  const stop = interval(fn, 100);
  expect(fn).not.toHaveBeenCalled();
  vi.advanceTimersByTime(250);
  expect(fn).toHaveBeenCalledTimes(2);
  stop();
  vi.advanceTimersByTime(500);
  expect(fn).toHaveBeenCalledTimes(2);
});

test("intervalNow は先に 1 回呼んでから周期で呼び、後始末で止まる", () => {
  const fn = vi.fn();
  const stop = intervalNow(fn, 100);
  expect(fn).toHaveBeenCalledTimes(1);
  vi.advanceTimersByTime(200);
  expect(fn).toHaveBeenCalledTimes(3);
  stop();
  vi.advanceTimersByTime(500);
  expect(fn).toHaveBeenCalledTimes(3);
});
