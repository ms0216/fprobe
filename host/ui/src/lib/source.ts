// 使われ方の記録の「どこから」（docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §3.4）: 窓（panel:<id>）・
// 画面の枠（shell）・共有の部品（debugger・timeline 等）ごとに、要求に source を付けて送る。デーモンは申告をそのまま
// 操作の記録に残す（推定しない）。共有の部品の要求は、どの窓から使われても部品の名前になる（呼び出しの経路の事実）
import type { DaemonConnection } from "./api";

type Conn = Pick<DaemonConnection, "call" | "on" | "onLink">;
type Opts = { quiet?: boolean; source?: string } | undefined;

/** 要求に source を付ける接続（呼び手が source を渡せばそれを使う） */
export function sourcedConn<C extends Conn>(conn: C, source: string): C {
  return {
    ...conn,
    call: (method: any, params?: any, opts?: Opts) => conn.call(method, params, { source, ...opts } as never),
    on: conn.on.bind(conn),
    onLink: conn.onLink.bind(conn),
  } as C;
}

/** act だけ source を付け、ほか（状態・購読・メソッド）は元のものを指す */
export function sourcedApp<A extends { act: (...args: any[]) => any }>(app: A, source: string): A {
  return new Proxy(app, {
    get(target, key) {
      if (key === "act") return (method: any, params?: any, opts?: Opts) => target.act(method, params, { source, ...opts });
      const value = Reflect.get(target, key, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}
