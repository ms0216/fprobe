/* SPDX-License-Identifier: Apache-2.0
 * Shrike-Lite の C のデモ（MCU 側）: FPGA に回路を送り、演算器に A × B を頼んで結果を確かめ続ける。
 * 要求のスレッド req: A・B を書いて開始 → STATUS を g_poll_limit 回まで読む → 足りなければリトライ（待ち回数を倍に）
 * → RESULT を A × B と比べる。GPIO26 = 要求中 1（リトライのたびに一度 0 に戻す）。g_log_every 回ごとに RTT へ 1 行。 */
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include "accel.h"
#include "demo.h"
#include "fpga_load.h"
#include "scenario.h"

#define USED __attribute__((used))

USED volatile uint32_t g_requests;
USED volatile uint32_t g_errors;
USED volatile uint32_t g_retries;
USED volatile uint32_t g_last_a;
USED volatile uint32_t g_last_b;
USED volatile uint32_t g_last_result;
USED volatile uint32_t g_fpga_id;
USED volatile uint32_t g_scenario;
USED volatile uint32_t g_poll_limit = 16;
USED volatile uint32_t g_s2_rate = 10;
USED volatile uint32_t g_log_every = 100;
USED volatile uint32_t g_reset;
USED volatile uint32_t g_s3_period_us = 99991;

static const struct device *const gpio = DEVICE_DT_GET(DT_NODELABEL(gpio0));
static uint32_t seed = 12345;

static uint8_t rand8(void)
{
	seed = seed * 1103515245u + 12345u;
	return (seed >> 16) & 0xFF;
}

/* FPGA に回路を送って ID を確かめる。合わなければ false（要求しない） */
static bool start_fpga(void)
{
	scenario_quiesce();                         /* 題材の割り込みが送り込みの CS を乱さないように */
	int err = fpga_load(fpga_image, fpga_image_len);

	accel_init();
	g_fpga_id = accel_read(REG_ID);
	if (err || g_fpga_id != FPGA_ID) {
		printk("fpga: load error %d, id 0x%04x (expected 0x%04x)\n", err, g_fpga_id, FPGA_ID);
		return false;
	}
	printk("fpga: loaded %u bytes, id 0x%04x\n", (unsigned)fpga_image_len, g_fpga_id);
	return true;
}

#define POLLS_MAX (1u << 20)

/* 1 回の要求。結果を *result に入れて true。PC から題材の切り替え・送り直しを頼まれたら途中でやめて false
 * （S4 で FPGA が固まると、ここから抜けられなくなるため） */
static bool request(uint8_t a, uint8_t b, uint32_t scenario, uint16_t *result)
{
	uint32_t polls = g_poll_limit;

	gpio_pin_set(gpio, PIN_MARK, 1);
	accel_write(REG_A, a);
	accel_write(REG_B, b);
	accel_write(REG_CTRL, 1);
	for (;;) {
		uint32_t k;

		for (k = 0; k < polls; k++) {
			if (accel_read(REG_STATUS) & STATUS_DONE) {
				break;
			}
			/* 長い待ち（最大 2^20 回 ≒ 25 秒）の途中でも PC の切り替え・送り直しに応える */
			if ((k & 0xFFu) == 0xFFu && (g_reset || g_scenario != scenario)) {
				gpio_pin_set(gpio, PIN_MARK, 0);
				return false;
			}
		}
		if (k < polls) {
			break;
		}
		if (g_reset || g_scenario != scenario) {
			gpio_pin_set(gpio, PIN_MARK, 0);
			return false;
		}
		g_retries++;                  /* タイムアウト: リトライ（次は倍まで待つ） */
		if (scenario == 4) {
			scenario_cue();       /* S4: タイムアウトを検知した瞬間の印 */
		}
		if (polls < POLLS_MAX) {
			polls *= 2;
		}
		gpio_pin_set(gpio, PIN_MARK, 0);
		gpio_pin_set(gpio, PIN_MARK, 1);
		accel_write(REG_CTRL, 1);
	}
	*result = accel_read(REG_RESULT);
	gpio_pin_set(gpio, PIN_MARK, 0);
	return true;
}

static void req_main(void *p1, void *p2, void *p3)
{
	uint32_t applied = UINT32_MAX;

	gpio_pin_configure(gpio, PIN_MARK, GPIO_OUTPUT_LOW);
	gpio_pin_configure(gpio, PIN_CUE, GPIO_OUTPUT_LOW);
	bool loaded = start_fpga();

	seed ^= k_cycle_get_32();
	for (;;) {
		if (g_reset || !loaded) {                 /* PC から: FPGA を送り直す（固まりから戻す等） */
			while (!loaded && !g_reset) {     /* 送れなかった・ID が違う: 要求を止めて送り直しを待つ */
				k_msleep(100);
			}
			g_reset = 0;
			loaded = start_fpga();
			applied = UINT32_MAX;
			if (!loaded) {
				continue;
			}
		}
		if (g_scenario != applied) {
			applied = g_scenario;
			scenario_apply(applied);
		}
		uint8_t a = rand8(), b = rand8();
		uint16_t result;

		if (!request(a, b, applied, &result)) {
			continue;
		}
		bool ok = result == (uint16_t)(a * b);   /* 結果の比較（ソース行のブレークポイントの例） */

		g_requests++;
		if (!ok) {
			g_errors++;
		}
		g_last_a = a;
		g_last_b = b;
		g_last_result = result;
		uint32_t every = g_log_every;            /* PC が途中で 0 を書いても 0 で割らない */

		if (every && g_requests % every == 0) {
			printk("req %u a %u b %u result %u %s errors %u retries %u\n", g_requests, a, b, result,
			       ok ? "ok" : "NG", g_errors, g_retries);
		}
		k_msleep(5);
	}
}

K_THREAD_DEFINE(req, 2048, req_main, NULL, NULL, NULL, 5, 0, 0);
