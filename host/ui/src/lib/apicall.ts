// API パネルとお気に入りが共有する呼び出し: 呼ぶ前の確かめ（read は無し、control は引数を見せて確かめ、元に戻せない
// 操作は赤。AI の作業中は先に聞く）と、結果・誤りの文字列。Svelte に依らない
import { errorText, type DaemonConnection } from "./api";
import { confirmLevel, type ApiMethod } from "./apiform";
import { confirmer, type Confirmer } from "./confirm";
import { allowed } from "./guard";
import { t } from "./i18n";
import type { AppState } from "./state";

export interface CallDeps {
  conn: Pick<DaemonConnection, "call">;
  app: Pick<AppState, "aiWorking">;
  ask?: Confirmer;
}

/** 呼んだ結果（ok = false は誤りの文）。確かめでやめたら null */
export async function callApi(m: Pick<ApiMethod, "name" | "kind" | "irreversible">, params: Record<string, unknown>,
                              { conn, app, ask = confirmer }: CallDeps): Promise<{ ok: boolean; text: string } | null> {
  const level = confirmLevel(m);
  if (level !== "none") {
    if (!(await allowed(app, ask))) return null;
    const message = t(level === "danger" ? "api.confirmDanger" : "api.confirm",
                      { method: m.name, params: JSON.stringify(params, null, 2) });
    if (!(await ask.ask(message, { ok: t("api.call"), danger: level === "danger" }))) return null;
  }
  try {
    const r = await conn.call(m.name as never, params as never);
    return { ok: true, text: JSON.stringify(r, null, 2) ?? "null" };
  } catch (e) {
    return { ok: false, text: errorText(e) };
  }
}
