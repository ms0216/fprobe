/* Zynq DUT 回路（gateware/rtl/designs/dut_stream_core.vhd）のレジスタ表。語番地は PL の表と同じ（host/tests/test_dut_app.py が照合する）。
 * PS から 0x40000000 に写る（AXI-Lite、4 KB）。 */
#ifndef DUT_REGS_H
#define DUT_REGS_H

#define DUT_BASE_ADDR       0x40000000u
#define DUT_MAP_BYTES       4096u

#define DUT_REG_CTRL        0   /* bit0 = 生成の有効 */
#define DUT_REG_PERIOD      1   /* 生成の間隔（クロック数、100 MHz。既定 1000） */
#define DUT_REG_DATA        2   /* 読むと FIFO の先頭を 1 語取り出す。空なら 0xFFFFFFFF */
#define DUT_REG_LEVEL       3   /* FIFO の語数 */
#define DUT_REG_OVERFLOWS   4   /* 満杯で捨てた数（クリアしない） */
#define DUT_REG_GENERATED   5   /* 生成した数（不具合で捨てた分も含む） */
#define DUT_REG_FAULT       6   /* bit0 = 有効、bits[31:8] = N（N 個に 1 個を黙って捨てる） */
#define DUT_REG_ID          7

#define DUT_ID_VALUE        0x44555401u
#define DUT_EMPTY           0xFFFFFFFFu

#endif
