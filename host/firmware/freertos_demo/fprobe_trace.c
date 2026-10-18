// SPDX-License-Identifier: Apache-2.0
/* fprobe_trace.h のカーネルの出来事の記録を ITM に書く（見出しをポート 30、中身をポート 29）。
 * 割り込みを禁止して書く: 途中で ISR のフックの記録が割り込まない（ホストは見出しの後に中身が並ぶと仮定する）。
 * ITM（TCR.ITMENA と TER のポート 29・30）が無効なら何もしない（FIFO を待たない）。
 */
#include "fprobe_trace.h"

static inline void fprobe_itm_put(uint32_t port, uint32_t word) {
    while (FPROBE_ITM_PORT(port) == 0u) {
        /* FIFO が空くのを待つ（読むと 1 = 書ける） */
    }
    FPROBE_ITM_PORT(port) = word;
}

void fprobe_trace_record(uint32_t code_count, uint32_t arg, uint32_t w0, uint32_t w1) {
    const uint32_t ports = (1u << FPROBE_KERNEL_PORT) | (1u << FPROBE_PAYLOAD_PORT);
    if (!(FPROBE_ITM_TCR & 1u) || (FPROBE_ITM_TER & ports) != ports) {
        return;
    }
    uint32_t ipsr, primask;
    __asm volatile("mrs %0, ipsr" : "=r"(ipsr));
    __asm volatile("mrs %0, primask\n cpsid i" : "=r"(primask) : : "memory");
    if (arg > FPROBE_ARG_MAX) {
        arg = FPROBE_ARG_MAX;
    }
    fprobe_itm_put(FPROBE_KERNEL_PORT, (code_count & 0x3FFu) | ((ipsr & 0x1FFu) << 10) | (arg << 19));
    uint32_t count = (code_count >> 8) & 3u;
    if (count > 0u) {
        fprobe_itm_put(FPROBE_PAYLOAD_PORT, w0);
    }
    if (count > 1u) {
        fprobe_itm_put(FPROBE_PAYLOAD_PORT, w1);
    }
    __asm volatile("msr primask, %0" : : "r"(primask) : "memory");
}
