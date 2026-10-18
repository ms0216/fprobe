// 偽の接続: call は responders で答える（関数なら引数を渡して呼ぶ）。試験から通知と接続の状態を起こす
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
  onLink(fn: (s: any, h?: any) => void) {
    this.link = fn;
    return () => {};
  }
  emit(topic: string, data: any) {
    this.topics.get(topic)?.forEach((fn) => fn(data));
  }
}
