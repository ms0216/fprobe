/* SPDX-License-Identifier: Apache-2.0
 * FPGA（ForgeFPGA SLG47910）に回路を送る。手順は Shrike の公式のライブラリ（vicharak-in/shrike_flash の
 * ShrikeFlash.cpp）と同じ: CS・EN・PWR を下げて 100 ms → EN・PWR を上げて 100 ms → 500 ms → CS を上げて 2 ms →
 * CS を下げる → SPI モード 0・MSB 先頭で回路のデータをそのまま送る → CS を上げて 100 ms。 */
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/kernel.h>

#include "demo.h"
#include "fpga_load.h"

const uint8_t fpga_image[] = {
#include "fpga_image.inc"
};
const size_t fpga_image_len = sizeof(fpga_image);

static const struct device *const gpio = DEVICE_DT_GET(DT_NODELABEL(gpio0));
static const struct device *const spi = DEVICE_DT_GET(DT_NODELABEL(spi0));

static const struct spi_config load_cfg = {
	.frequency = 1600000,   /* 公式のライブラリの既定 */
	.operation = SPI_WORD_SET(8) | SPI_TRANSFER_MSB | SPI_OP_MODE_MASTER,
};

int fpga_load(const uint8_t *image, size_t len)
{
	int err = gpio_pin_configure(gpio, PIN_SPI_CS, GPIO_OUTPUT_LOW) |
		  gpio_pin_configure(gpio, PIN_FPGA_EN, GPIO_OUTPUT_LOW) |
		  gpio_pin_configure(gpio, PIN_FPGA_PWR, GPIO_OUTPUT_LOW);
	if (err) {
		return -EIO;
	}
	k_msleep(100);
	gpio_pin_set(gpio, PIN_FPGA_EN, 1);
	gpio_pin_set(gpio, PIN_FPGA_PWR, 1);
	k_msleep(100);
	k_msleep(500);
	gpio_pin_set(gpio, PIN_SPI_CS, 1);
	k_usleep(2000);
	gpio_pin_set(gpio, PIN_SPI_CS, 0);

	const struct spi_buf buf = {.buf = (void *)image, .len = len};
	const struct spi_buf_set tx = {.buffers = &buf, .count = 1};
	err = spi_write(spi, &load_cfg, &tx);

	gpio_pin_set(gpio, PIN_SPI_CS, 1);
	k_msleep(100);
	return err;
}
