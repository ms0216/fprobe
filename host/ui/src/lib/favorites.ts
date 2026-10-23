// お気に入り（段階 B。docs/superpowers/specs/2026-10-22-ui-stage-b-design.md §2）: よく使う呼び出し（メソッドと引数）を
// 人が自分で並べる。その人の画面のブラウザに覚える（人によって違うので、デーモンで全員と共有しない）。Svelte に依らない
import { buildParams, fieldsOf, valuesOf, type ApiMethod } from "./apiform";
import { load, save } from "./prefs";

export interface Favorite {
  id: string;
  label: string;
  method: string;
  params: Record<string, unknown>;
}

interface Store {
  load(): string | null;
  save(value: string): void;
}

const KEY = "favorites";
const browser: Store = { load: () => load(KEY, ""), save: (v) => save(KEY, v) };

function parse(text: string | null): Favorite[] {
  try {
    const v = JSON.parse(text || "[]");
    return Array.isArray(v) ? v.filter((f) => f && typeof f.id === "string" && typeof f.label === "string"
      && typeof f.method === "string" && f.params && typeof f.params === "object") : [];
  } catch {
    return [];
  }
}

export class Favorites {
  private items: Favorite[];
  private listeners = new Set<(list: Favorite[]) => void>();

  constructor(private store: Store = browser) {
    this.items = parse(store.load());
  }

  subscribe(fn: (list: Favorite[]) => void): () => void {
    this.listeners.add(fn);
    fn(this.items);
    return () => this.listeners.delete(fn);
  }

  get list(): Favorite[] {
    return this.items;
  }

  add(method: string, params: Record<string, unknown>, label = method): string {
    const id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    this.set([...this.items, { id, label, method, params }]);
    return id;
  }

  /** API パネルで直した呼び出しで置き換える（名前と位置は保つ） */
  update(id: string, method: string, params: Record<string, unknown>): void {
    this.set(this.items.map((f) => (f.id === id ? { ...f, method, params } : f)));
  }

  rename(id: string, label: string): void {
    this.set(this.items.map((f) => (f.id === id ? { ...f, label } : f)));
  }

  /** delta = -1 で上へ、+1 で下へ */
  move(id: string, delta: number): void {
    const i = this.items.findIndex((f) => f.id === id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= this.items.length) return;
    const next = [...this.items];
    [next[i], next[j]] = [next[j], next[i]];
    this.set(next);
  }

  remove(id: string): void {
    this.set(this.items.filter((f) => f.id !== id));
  }

  private set(items: Favorite[]): void {
    this.items = items;
    this.store.save(JSON.stringify(items));
    this.listeners.forEach((fn) => fn(items));
  }
}

/** 呼ぶ前の確かめ: 仕様に無いメソッドは missing、引数が今の仕様に合わなければ params（API パネルと同じ組み立てで） */
export function checkFavorite(f: Favorite, methods: ApiMethod[]):
  { method: ApiMethod; params: Record<string, unknown> } | { problem: "missing" | "params" } {
  const method = methods.find((m) => m.name === f.method);
  if (!method) return { problem: "missing" };
  const fields = fieldsOf(method.params);
  const unknown = Object.keys(f.params).some((k) => !fields.some((x) => x.name === k));
  const restored = valuesOf(fields, f.params);
  const built = buildParams(fields, restored.values);
  if (unknown || restored.unrestored.length || "errors" in built) return { problem: "params" };
  return { method, params: built.params };
}

export const favorites = new Favorites();
