// 人間の操作の前の確認: AI がつながっていて一時停止していなければ、AI が操作すると人の操作と混ざりうる。事実（接続・
// 一時停止）だけで聞き、AI が作業中かは判定しない。一時停止中・AI の接続が無ければ確認しない。
// 確認は画面の中の窓（lib/confirm.ts）なので非同期: 呼ぶ側は `if (!(await allowed(app))) return;`
import { confirmer, type Confirmer } from "./confirm";
import { t } from "./i18n";
import type { AppState } from "./state";

export async function allowed(app: Pick<AppState, "aiConnectedUnpaused">, ask: Confirmer = confirmer): Promise<boolean> {
  return !app.aiConnectedUnpaused || ask.ask(t("confirm.aiConnected"), { ok: t("confirm.continue") });
}
