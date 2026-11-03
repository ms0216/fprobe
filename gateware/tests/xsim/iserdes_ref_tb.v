// ISERDESE2 の本物の模型（Vivado の UNISIM、xsim）に既知のビット列を流し、CLKDIV ごとの Q1〜Q8 を記録する。
// 記録は tests/data/iserdes_ref_w<W>.txt に置き、GHDL の模型（tests/rtl/sim_xilinx_prims.vhd の ISERDESE2）が
// 同じ入力で同じ出力を出すことを tests/test_xilinx_la_iserdes.py が確かめる（設計 2026-11-01-la-resolution-design.md §4.1）。
// 作り直し: python gateware/tests/xsim/run_iserdes_ref.py（Vivado の xvlog・xelab・xsim を使う）
`timescale 1ps/1ps
module iserdes_ref_tb;
  parameter W = 6;              // DATA_WIDTH（DDR: 4・6・8）
  localparam R = W / 2;         // CLKDIV 1 周期の CLK の周期の数
  localparam TC = 3000;         // CLK の周期（ps。比だけが意味を持つ）
  localparam TD = TC * R;       // CLKDIV の周期
  localparam SLOTS = 512;
  reg clk = 1, clkdiv = 1, rst = 1, d = 0;
  wire [8:1] q;
  // CLK と CLKDIV は時刻 0 で立ち上がりがそろう（同じ MMCM から割った BUFG の 2 本と同じ）。CLK の辺は i × TC/2
  // （i が偶数なら立ち上がり）、CLKDIV の立ち上がりは w × TD
  always #(TC / 2) clk = ~clk;
  always #(TD / 2) clkdiv = ~clkdiv;
  ISERDESE2 #(.DATA_RATE("DDR"), .DATA_WIDTH(W), .INTERFACE_TYPE("NETWORKING"), .IOBDELAY("NONE"),
              .NUM_CE(1), .OFB_USED("FALSE"), .SERDES_MODE("MASTER"),
              .DYN_CLKDIV_INV_EN("FALSE"), .DYN_CLK_INV_EN("FALSE"))
    dut (.CLK(clk), .CLKB(~clk), .CLKDIV(clkdiv), .CLKDIVP(1'b0), .OCLK(1'b0), .OCLKB(1'b0), .D(d), .DDLY(1'b0),
         .CE1(1'b1), .CE2(1'b1), .RST(rst), .BITSLIP(1'b0), .DYNCLKDIVSEL(1'b0), .DYNCLKSEL(1'b0), .OFB(1'b0),
         .SHIFTIN1(1'b0), .SHIFTIN2(1'b0),
         .Q1(q[1]), .Q2(q[2]), .Q3(q[3]), .Q4(q[4]), .Q5(q[5]), .Q6(q[6]), .Q7(q[7]), .Q8(q[8]),
         .O(), .SHIFTOUT1(), .SHIFTOUT2());
  // 区間 i（i × TC/2 の CLK の辺で取られる）: d は i × TC/2 − TC/4 から i × TC/2 + TC/4 まで b[i]
  integer i, w, seed;
  reg [SLOTS - 1:0] b;
  initial begin
    seed = 12345;
    for (i = 0; i < SLOTS; i = i + 1) b[i] = $random(seed) & 1;
    // RST は CLKDIV に同期（UG953: CLKDIV の領域の同期リセット、CLKDIV 1 周期以上）
    @(posedge clkdiv); @(posedge clkdiv); @(posedge clkdiv); @(posedge clkdiv);
    #1 rst = 0;
  end
  initial begin
    d = b[0];
    #(TC / 4);
    for (i = 1; i < SLOTS; i = i + 1) begin
      d = b[i];
      #(TC / 2);
    end
  end
  initial begin
    $display("B %b", b);
    for (w = 0; w < SLOTS / W - 2; w = w + 1) begin
      @(posedge clkdiv);
      #1;
      // w = 時刻 w × TD の CLKDIV の立ち上がりの直後の値。Q は Q8..Q1 の順
      $display("Q %0d %b%b%b%b%b%b%b%b", w, q[8], q[7], q[6], q[5], q[4], q[3], q[2], q[1]);
    end
    $finish;
  end
endmodule
