/* SPDX-License-Identifier: Apache-2.0 */
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>

#include "accel.h"
#include "demo.h"
#include "scenario.h"

/* RP2040 の SIO（GPIO の出力を 1 命令で読み書きする）。S3 の割り込みが直接書く */
#define SIO_GPIO_OUT (*(volatile uint32_t *)0xD0000010u)

static const struct device *const gpio = DEVICE_DT_GET(DT_NODELABEL(gpio0));

/* S3: 割り込みの中で「題材の印を反転する」つもりの処理。出力の写し out_copy は、割り込みを始めたときの出力
 * （CS = 1・マーカー = 0 の空き時間）のまま更新しないので、転送の途中に当たると CS を 1 に戻してしまう
 * （GPIO の出力の読み・変更・書きの競合。SET/CLR の 1 ビットの書きを使えば起きない） */
static uint32_t out_copy;

static void s3_tick(struct k_timer *t)
{
	ARG_UNUSED(t);
	out_copy ^= BIT(PIN_CUE);
	SIO_GPIO_OUT = out_copy;
}

K_TIMER_DEFINE(s3_timer, s3_tick, NULL);

void scenario_quiesce(void)
{
	k_timer_stop(&s3_timer);
	gpio_pin_set(gpio, PIN_CUE, 0);
}

void scenario_apply(uint32_t s)
{
	static const uint16_t mode[] = {0, 0x1, 0x2, 0x0, 0x5};
	uint16_t m = s < ARRAY_SIZE(mode) ? mode[s] : 0;

	scenario_quiesce();
	accel_write(REG_S2_MASK, s == 2 ? g_s2_rate : 0);
	accel_write(REG_MODE, m);                     /* MODE を書くと S4 の固まりも解ける */
	g_poll_limit = s == 2 ? 128 : 16;             /* S2 はリトライさせない（取りこぼしだけを見る） */
	if (s == 3) {
		out_copy = SIO_GPIO_OUT;
		k_timer_start(&s3_timer, K_USEC(g_s3_period_us), K_USEC(g_s3_period_us));
	}
}

void scenario_cue(void)
{
	gpio_pin_set(gpio, PIN_CUE, 1);
	gpio_pin_set(gpio, PIN_CUE, 0);
}
