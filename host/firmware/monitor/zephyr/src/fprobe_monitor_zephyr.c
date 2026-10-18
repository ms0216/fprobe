// SPDX-License-Identifier: Apache-2.0
// fprobe のモニタ（../../fprobe_monitor.c）を Zephyr で起動する。
//
// ベクタ 12: Zephyr のベクタ表（arch/arm/core/cortex_m/vector_table.S）は DebugMonitor に z_arm_debug_monitor を
//   置き、fault_s.S がそれを弱い定義（故障の処理）で持つ。CONFIG_CORTEX_M_DEBUG_MONITOR_HOOK はこれを上書きするための
//   Zephyr の決まった口（z_arm_exc_setup が DebugMon を最も低い優先度にする）。fprobe_monitor.c は CMake の
//   FPROBE_MONITOR_HANDLER=z_arm_debug_monitor でその名前のハンドラを作る（頭の印 "FPMN" と箱の番地はそのまま）。
// 優先度: SYS_INIT（PRE_KERNEL_1。z_arm_exc_setup の後）で fprobe_monitor_init(CONFIG_FPROBE_MONITOR_PRIORITY)。
#include <zephyr/init.h>
#include <zephyr/kernel.h>

#include "fprobe_monitor.h"

BUILD_ASSERT(CONFIG_FPROBE_MONITOR_STACK_SIZE % 8 == 0, "monitor stack must be a multiple of 8");

static int fprobe_monitor_zephyr_init(void)
{
	fprobe_monitor_init((uint8_t)CONFIG_FPROBE_MONITOR_PRIORITY);
	return 0;
}

SYS_INIT(fprobe_monitor_zephyr_init, PRE_KERNEL_1, CONFIG_FPROBE_MONITOR_INIT_PRIORITY);
