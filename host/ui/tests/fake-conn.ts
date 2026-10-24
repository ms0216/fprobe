// 偽の接続: call は responders で答える（関数なら引数を渡して呼ぶ）。試験から通知と接続の状態を起こす
import { RpcError } from "../src/lib/api";

/** デーモンが返す機器の誤り（E_DEVICE。data に例外の型と文） */
export function deviceError(detail: string, exception = "RuntimeError"): RpcError {
  return new RpcError(-32020, `${exception}: ${detail}`, { exception, detail });
}

export class FakeConn {
  topics = new Map<string, Set<(d: any) => void>>();
  link: ((s: any, h?: any) => void) | null = null;
  calls: [string, any][] = [];
  quiet: string[] = [];                    // quiet で呼ばれたメソッド
  constructor(public responders: Record<string, any>) {}
  call(method: string, params: any = {}, opts?: { quiet?: boolean }) {
    this.calls.push([method, params]);
    if (opts?.quiet) this.quiet.push(method);
    const r = this.responders[method];
    try {
      return Promise.resolve(typeof r === "function" ? r(params) : r);
    } catch (e) {
      return Promise.reject(e);
    }
  }
  on(topic: string, fn: (d: any) => void) {
    const set = this.topics.get(topic) ?? new Set();
    set.add(fn);
    this.topics.set(topic, set);
    return () => set.delete(fn);
  }
  private linkFns: ((s: any, h?: any) => void)[] = [];
  onLink(fn: (s: any, h?: any) => void) {
    this.linkFns.push(fn);                 // 受け手はいくつでも（状態・時間軸・画面の同期が同じ接続を見る）
    this.link = (s, h) => this.linkFns.forEach((f) => f(s, h));
    return () => {};
  }
  emit(topic: string, data: any) {
    this.topics.get(topic)?.forEach((fn) => fn(data));
  }
}
