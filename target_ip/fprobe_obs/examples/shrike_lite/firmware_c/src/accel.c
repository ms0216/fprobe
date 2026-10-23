/* SPDX-License-Identifier: Apache-2.0 */
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/spi.h>

#include "accel.h"
#include "demo.h"

static const struct device *const gpio = DEVICE_DT_GET(DT_NODELABEL(gpio0));
static const struct device *const spi = DEVICE_DT_GET(DT_NODELABEL(spi0));

static const struct spi_config cfg = {
	.frequency = 1000000,
	.operation = SPI_WORD_SET(8) | SPI_TRANSFER_MSB | SPI_OP_MODE_MASTER,
};

int accel_init(void)
{
	return gpio_pin_configure(gpio, PIN_SPI_CS, GPIO_OUTPUT_HIGH);
}

static void frame(const uint8_t tx[3], uint8_t rx[3])
{
	const struct spi_buf tb = {.buf = (void *)tx, .len = 3};
	const struct spi_buf rb = {.buf = rx, .len = 3};
	const struct spi_buf_set txs = {.buffers = &tb, .count = 1};
	const struct spi_buf_set rxs = {.buffers = &rb, .count = 1};

	gpio_pin_set(gpio, PIN_SPI_CS, 0);
	spi_transceive(spi, &cfg, &txs, &rxs);
	gpio_pin_set(gpio, PIN_SPI_CS, 1);
}

void accel_write(uint8_t addr, uint16_t v)
{
	const uint8_t tx[3] = {addr, v >> 8, v & 0xFF};
	uint8_t rx[3];

	frame(tx, rx);
}

uint16_t accel_read(uint8_t addr)
{
	const uint8_t tx[3] = {0x80 | addr, 0, 0};
	uint8_t rx[3] = {0, 0, 0};                      /* SPI が失敗しても初期化されないスタックの値を返さない */

	frame(tx, rx);
	return (uint16_t)(rx[1] << 8 | rx[2]);
}
