// 人間の操作の前の確認: AI の作業中に操作すると AI の操作と混ざる。一時停止中は確認しない。
// 確認は画面の中の窓（lib/confirm.ts）なので非同期: 呼ぶ側は `if (!(await allowed(app))) return;`
import { confirmer, type Confirmer } from "./confirm";
import { t } from "./i18n";
import type { AppState } from "./state";

export async function allowed(app: Pick<AppState, "aiWorking">, ask: Confirmer = confirmer): Promise<boolean> {
  return !app.aiWorking || ask.ask(t("confirm.aiWorking"), { ok: t("confirm.continue") });
}
