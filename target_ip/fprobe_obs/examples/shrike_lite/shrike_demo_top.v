// SPDX-License-Identifier: Apache-2.0
// Shrike-Lite（ForgeFPGA SLG47910）の最上位: ピン・クロックだけを扱い、中身は shrike_demo_core（VHDL を GHDL で
// Verilog にしたもの）。ピンの割り当ては build.py（ForgeFPGA の I/O Planner の記録）。
//
//   osc_clk  ← 内蔵の発振器 50 MHz（osc_en = 1）
//   clk      ← osc_clk を 2 分周して「ロジックをクロックに」（LaC0）で戻した 25 MHz（観測の回路の見積もりが 50 MHz に
//              届かないため。lac0_clk = 源、lac0_en = 1）
//   nreset   ← 電源投入のリセット
//   tck・tms・tdi ← F_GPIO0・F_GPIO18・F_GPIO1、tdo → F_GPIO7（fprobe の観測の JTAG。観測の回路は 1 クロック版で、
//              TCK は clk で標本化する。TCK ≤ 25 MHz / 8）
//   spi_*    ↔ F_GPIO3〜6（FPGA を書く SPI。書いた後は RP2040 の SPI0 と話す）
(* top *) module shrike_demo_top (
    (* iopad_external_pin, clkbuf_inhibit *) input osc_clk,
    (* iopad_external_pin *) output osc_en,
    (* iopad_external_pin, clkbuf_inhibit *) input clk,
    (* iopad_external_pin *) output lac0_clk,
    (* iopad_external_pin *) output lac0_en,
    (* iopad_external_pin *) input  nreset,
    (* iopad_external_pin *) input  tck,
    (* iopad_external_pin *) input  tms,
    (* iopad_external_pin *) input  tdi,
    (* iopad_external_pin *) output tdo,
    (* iopad_external_pin *) output tdo_oe,
    (* iopad_external_pin *) input  spi_sck,
    (* iopad_external_pin *) input  spi_cs_n,
    (* iopad_external_pin *) input  spi_mosi,
    (* iopad_external_pin *) output spi_miso,
    (* iopad_external_pin *) output spi_miso_oe
);
    reg half = 1'b0;
    always @(posedge osc_clk) half <= ~half;

    assign osc_en = 1'b1;
    assign lac0_clk = half;
    assign lac0_en = 1'b1;
    assign spi_miso_oe = 1'b1;

    shrike_demo_core core (
        .clk(clk), .nreset(nreset),
        .spi_sck(spi_sck), .spi_cs_n(spi_cs_n), .spi_mosi(spi_mosi), .spi_miso(spi_miso),
        .tck(tck), .tms(tms), .tdi(tdi), .tdo(tdo), .tdo_oe(tdo_oe)
    );
endmodule
