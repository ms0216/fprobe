/* SPDX-License-Identifier: Apache-2.0 */
#ifndef FPGA_LOAD_H
#define FPGA_LOAD_H

#include <stddef.h>
#include <stdint.h>

/* 埋め込んだ FPGA の回路（build.py が fpga_mcu.bin から作る） */
extern const uint8_t fpga_image[];
extern const size_t fpga_image_len;

/* Shrike の手順で FPGA に回路を送る（vicharak-in/shrike_flash と同じ）。0 = 成功、負 = SPI・GPIO の誤り。 */
int fpga_load(const uint8_t *image, size_t len);

#endif
