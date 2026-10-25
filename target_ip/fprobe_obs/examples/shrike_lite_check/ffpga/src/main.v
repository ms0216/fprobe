// Shrike-Lite（ForgeFPGA SLG47910）: 観測の JTAG の配線の確かめ。
// PYNQ の fprobe が観測の回路の ID "FOBS" を読めれば、TCK・TMS・TDI・TDO の 4 本とも正しい。
// ピン: F_GPIO2 = TCK、F_GPIO0 = TMS、F_GPIO1 = TDI、F_GPIO7 = TDO。
// TCK はピンから入れて「ロジックをクロックに」（LaC0）でクロックの網に戻す（見本 AN-FG-019 と同じ形）。
// obs_check_core は target_ip/fprobe_obs/rtl を GHDL で Verilog にしたもの（obs_soft_tap + obs_core、組 1、FIFO 4、clk = TCK）。
(* top *) module main (
  (* iopad_external_pin, clkbuf_inhibit *) input tck,   // LaC0 から戻るクロック
  (* iopad_external_pin *) input  tck_pin,
  (* iopad_external_pin *) input  tms,
  (* iopad_external_pin *) input  tdi,
  (* iopad_external_pin *) output tdo,
  (* iopad_external_pin *) output tdo_oe,
  (* iopad_external_pin *) output lac0_clk,              // LaC0 の源
  (* iopad_external_pin *) output lac0_en
);
  assign lac0_clk = tck_pin;
  assign lac0_en  = 1'b1;
  obs_check_core core (.tck(tck), .tms(tms), .tdi(tdi), .tdo(tdo), .tdo_oe(tdo_oe));
endmodule
