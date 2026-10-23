/* SPDX-License-Identifier: Apache-2.0
 * Shrike-Lite の C のデモ: ピンと、fprobe が名前で読み書きする大域変数。 */
#ifndef DEMO_H
#define DEMO_H

#include <stdint.h>

/* RP2040 の GPIO */
#define PIN_SPI_CS   1    /* FPGA の SPI の CS（回路の送り込みと演算器で共通） */
#define PIN_FPGA_PWR 12
#define PIN_FPGA_EN  13
#define PIN_MARK     26   /* 要求中 1（ロジアナ ch 10） */
#define PIN_CUE      27   /* 題材の印（PMOD JB 7 = ロジアナ ch 14） */

#define FPGA_ID 0x5D01u

/* fprobe が名前で読む（g_scenario 等は書いて題材を切り替える）。volatile: 外から読み書きされる */
extern volatile uint32_t g_requests;
extern volatile uint32_t g_errors;
extern volatile uint32_t g_retries;
extern volatile uint32_t g_last_a;
extern volatile uint32_t g_last_b;
extern volatile uint32_t g_last_result;
extern volatile uint32_t g_fpga_id;
extern volatile uint32_t g_scenario;
extern volatile uint32_t g_poll_limit;
extern volatile uint32_t g_s2_rate;
extern volatile uint32_t g_log_every;
extern volatile uint32_t g_reset;
extern volatile uint32_t g_s3_period_us;

#endif
