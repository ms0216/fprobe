/* SPDX-License-Identifier: Apache-2.0
 * FPGA の演算器（rtl/demo_accel.vhd）のレジスタを SPI で読み書きする（3 バイトのフレーム: 書き = addr・上位・下位、
 * 読み = 0x80|addr・0・0 で返りの 2・3 バイト目）。 */
#ifndef ACCEL_H
#define ACCEL_H

#include <stdint.h>

enum accel_reg {
	REG_ID = 0x00, REG_CTRL = 0x01, REG_A = 0x02, REG_B = 0x03, REG_RESULT = 0x04,
	REG_STATUS = 0x06, REG_LEFT = 0x07, REG_MODE = 0x08, REG_S2_MASK = 0x09, REG_EVENTS = 0x0A,
};

#define STATUS_BUSY 0x1u
#define STATUS_DONE 0x2u

int accel_init(void);
void accel_write(uint8_t addr, uint16_t v);
uint16_t accel_read(uint8_t addr);

#endif
