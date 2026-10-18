"""リセットを nRESET の線だけで行う（SYSRESETREQ を使わない: SYSRESETREQ が効かない・外部の回路ごとリセットしたいターゲット）。

halt 付きのリセット（ベクタキャッチ）は pyOCD が前後で設定するので、そのまま使える。pyOCD のユーザスクリプト（target_connect の script）。
"""

import time

PULSE_S = 0.01


def will_reset(core, reset_type):
    probe = core.session.probe
    probe.assert_reset(True)
    time.sleep(PULSE_S)
    probe.assert_reset(False)
    time.sleep(PULSE_S)
    return True                         # 既定のリセットはしない
