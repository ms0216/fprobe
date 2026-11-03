// 人間の操作の前の確認: AI がつながっていて一時停止していなければ、AI が操作すると人の操作と混ざりうる。事実（接続・
// 一時停止）だけで聞き、AI が作業中かは判定しない。一時停止中・AI の接続が無ければ確認しない。
// 確認は画面の中の窓（lib/confirm.ts）なので非同期: 呼ぶ側は `if (!(await allowed(app))) return;`
import { confirmer, type Confirmer } from "./confirm";
import { t } from "./i18n";
import type { AppState } from "./state";

/** what = 今押した操作の名前（確認の文に載せる）。AI の名前は API の clients（事実）。どちらも無ければ従来の文 */
export async function allowed(app: Pick<AppState, "aiConnectedUnpaused"> & Partial<Pick<AppState, "aiClientNames">>,
                              ask: Confirmer = confirmer, what?: string): Promise<boolean> {
  if (!app.aiConnectedUnpaused) return true;
  const who = (app.aiClientNames ?? []).join(", ");
  const message = who || what ? t("confirm.aiConnectedWho", { who: who || "-", what: what ?? "-" }) : t("confirm.aiConnected");
  return ask.ask(message, { ok: t("confirm.continue") });
}
