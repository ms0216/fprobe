"""STM32: 眠り・停止・待機（WFI / STOP / STANDBY）の間もデバッグをつなぎ続ける（DBGMCU_CR の DBG_SLEEP / DBG_STOP / DBG_STANDBY）。

DBGMCU が 0xE0042000 にある系列（F1 / F2 / F3 / F4 / F7 / L1 / L4 / G4 / WB）。接続の後とリセットの後に書く。
ほかの番地の系列（F0 / G0 / L0 / H7）では何もせず、警告を残す。pyOCD のユーザスクリプト（target_connect の script）。
"""

DBGMCU_IDCODE = 0xE0042000
DBGMCU_CR = 0xE0042004
LOW_POWER_BITS = 0x7                    # DBG_SLEEP | DBG_STOP | DBG_STANDBY


def _enable(memory):
    if memory.read32(DBGMCU_IDCODE) & 0xFFF == 0:
        LOG.warning("DBGMCU is not at 0xE0042000: low-power debug is not enabled")  # noqa: F821（pyOCD の名前空間）
        return
    memory.write32(DBGMCU_CR, memory.read32(DBGMCU_CR) | LOW_POWER_BITS)


def did_init_target(target):
    _enable(target)


def did_reset(core, reset_type):
    _enable(core)
