module obs_async_fifo_Brtl_32_2
  (input  w_clk,
   input  w_rst,
   input  w_valid,
   input  [31:0] w_data,
   output [2:0] w_free,
   input  r_clk,
   input  r_pop,
   input  r_ahead,
   output [31:0] r_data,
   output [2:0] r_count);
  reg [2:0] w_ptr;
  reg [2:0] w_ptr_gray;
  reg [2:0] r_gray_w1;
  reg [2:0] r_gray_w2;
  reg [2:0] r_ptr;
  reg [2:0] r_ptr_gray;
  reg [2:0] w_gray_r1;
  reg [2:0] w_gray_r2;
  reg [31:0] r_data_q;
  wire n831;
  localparam [2:0] n832 = 3'bX;
  wire [1:0] n833;
  wire [2:0] n834;
  wire n835;
  wire n836;
  wire n837;
  wire n838;
  wire [2:0] n839;
  wire n840;
  wire n841;
  wire n842;
  wire [2:0] n843;
  wire [2:0] n844;
  wire [2:0] n846;
  wire n853;
  localparam [2:0] n854 = 3'bX;
  wire [1:0] n855;
  wire [2:0] n856;
  wire n857;
  wire n858;
  wire n859;
  wire n860;
  wire [2:0] n861;
  wire n862;
  wire n863;
  wire n864;
  wire [2:0] n865;
  wire [2:0] n866;
  wire n877;
  localparam [2:0] n878 = 3'bX;
  wire [1:0] n879;
  wire [2:0] n880;
  wire n881;
  wire n882;
  wire n883;
  wire n884;
  wire [2:0] n885;
  wire n886;
  wire n887;
  wire n888;
  wire [2:0] n889;
  wire n896;
  localparam [2:0] n897 = 3'bX;
  wire [1:0] n898;
  wire [2:0] n899;
  wire n900;
  wire n901;
  wire n902;
  wire n903;
  wire [2:0] n904;
  wire n905;
  wire n906;
  wire n907;
  wire [2:0] n908;
  wire [2:0] n909;
  wire n911;
  wire n912;
  wire [1:0] n913;
  wire [2:0] n920;
  wire [2:0] n922;
  wire [2:0] n924;
  wire [2:0] n931;
  wire [2:0] n932;
  wire [2:0] n946;
  wire [2:0] n947;
  wire [2:0] n954;
  wire [2:0] n955;
  wire [2:0] n957;
  wire [2:0] n958;
  wire [1:0] n959;
  wire n973;
  wire n974;
  reg [2:0] n977;
  reg [2:0] n978;
  reg [2:0] n979;
  reg [2:0] n980;
  reg [2:0] n981;
  reg [2:0] n982;
  reg [2:0] n983;
  reg [2:0] n984;
  reg [31:0] n985;
  wire [31:0] n986; // mem_rd
  assign w_free = n846; //(module output)
  assign r_data = r_data_q; //(module output)
  assign r_count = n866; //(module output)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:59:12 */
  always @*
    w_ptr = n977; // (isignal)
  initial
    w_ptr = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:60:12 */
  always @*
    w_ptr_gray = n978; // (isignal)
  initial
    w_ptr_gray = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:61:12 */
  always @*
    r_gray_w1 = n979; // (isignal)
  initial
    r_gray_w1 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:62:12 */
  always @*
    r_gray_w2 = n980; // (isignal)
  initial
    r_gray_w2 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:64:12 */
  always @*
    r_ptr = n981; // (isignal)
  initial
    r_ptr = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:65:12 */
  always @*
    r_ptr_gray = n982; // (isignal)
  initial
    r_ptr_gray = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:66:12 */
  always @*
    w_gray_r1 = n983; // (isignal)
  initial
    w_gray_r1 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:67:12 */
  always @*
    w_gray_r2 = n984; // (isignal)
  initial
    w_gray_r2 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:68:12 */
  always @*
    r_data_q = n985; // (isignal)
  initial
    r_data_q = 32'b00000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:49:23 */
  assign n831 = r_gray_w2[2]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n833 = n832[1:0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n834 = {n831, n833};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:22 */
  assign n835 = n834[2]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:35 */
  assign n836 = r_gray_w2[1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:30 */
  assign n837 = n835 ^ n836;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n838 = n832[0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n839 = {n831, n837, n838};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:22 */
  assign n840 = n839[1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:35 */
  assign n841 = r_gray_w2[0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:30 */
  assign n842 = n840 ^ n841;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n843 = {n831, n837, n842};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:70:69 */
  assign n844 = w_ptr - n843;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:70:60 */
  assign n846 = 3'b100 - n844;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:49:23 */
  assign n853 = w_gray_r2[2]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n855 = n854[1:0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n856 = {n853, n855};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:22 */
  assign n857 = n856[2]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:35 */
  assign n858 = w_gray_r2[1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:30 */
  assign n859 = n857 ^ n858;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n860 = n854[0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n861 = {n853, n859, n860};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:22 */
  assign n862 = n861[1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:35 */
  assign n863 = w_gray_r2[0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:30 */
  assign n864 = n862 ^ n863;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n865 = {n853, n859, n864};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:71:37 */
  assign n866 = n865 - r_ptr;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:49:23 */
  assign n877 = r_gray_w2[2]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n879 = n878[1:0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n880 = {n877, n879};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:22 */
  assign n881 = n880[2]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:35 */
  assign n882 = r_gray_w2[1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:30 */
  assign n883 = n881 ^ n882;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n884 = n878[0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n885 = {n877, n883, n884};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:22 */
  assign n886 = n885[1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:35 */
  assign n887 = r_gray_w2[0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:30 */
  assign n888 = n886 ^ n887;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n889 = {n877, n883, n888};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:49:23 */
  assign n896 = r_gray_w2[2]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n898 = n897[1:0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n899 = {n896, n898};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:22 */
  assign n900 = n899[2]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:35 */
  assign n901 = r_gray_w2[1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:30 */
  assign n902 = n900 ^ n901;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n903 = n897[0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n904 = {n896, n902, n903};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:22 */
  assign n905 = n904[1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:35 */
  assign n906 = r_gray_w2[0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:51:30 */
  assign n907 = n905 ^ n906;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:47:18 */
  assign n908 = {n896, n902, n907};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:82:43 */
  assign n909 = w_ptr - n908;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:82:66 */
  assign n911 = n909 != 3'b100;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:82:33 */
  assign n912 = n911 & w_valid;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:83:37 */
  assign n913 = w_ptr[1:0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:84:35 */
  assign n920 = w_ptr + 3'b001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:82:13 */
  assign n922 = n912 ? n920 : w_ptr;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:80:13 */
  assign n924 = w_rst ? n889 : n922;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:43:22 */
  assign n931 = n924 >> 31'b0000000000000000000000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:43:18 */
  assign n932 = n924 ^ n931;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:101:35 */
  assign n946 = r_ptr + 3'b001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:100:13 */
  assign n947 = r_pop ? n946 : r_ptr;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:43:22 */
  assign n954 = n947 >> 31'b0000000000000000000000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:43:18 */
  assign n955 = n947 ^ n954;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:108:34 */
  assign n957 = n947 + 3'b001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:107:13 */
  assign n958 = r_ahead ? n957 : n947;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:112:44 */
  assign n959 = n958[1:0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:80:13 */
  assign n973 = ~w_rst;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:80:13 */
  assign n974 = n973 & n912;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:77:9 */
  always @(posedge w_clk)
    n977 <= n924;
  initial
    n977 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:77:9 */
  always @(posedge w_clk)
    n978 <= n932;
  initial
    n978 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:77:9 */
  always @(posedge w_clk)
    n979 <= r_ptr_gray;
  initial
    n979 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:77:9 */
  always @(posedge w_clk)
    n980 <= r_gray_w1;
  initial
    n980 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:97:9 */
  always @(posedge r_clk)
    n981 <= n947;
  initial
    n981 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:97:9 */
  always @(posedge r_clk)
    n982 <= n955;
  initial
    n982 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:97:9 */
  always @(posedge r_clk)
    n983 <= w_ptr_gray;
  initial
    n983 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:97:9 */
  always @(posedge r_clk)
    n984 <= w_gray_r1;
  initial
    n984 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:97:9 */
  always @(posedge r_clk)
    n985 <= n986;
  initial
    n985 = 32'b00000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:112:29 */
  reg [31:0] mem[3:0] ; // memory
  assign n986 = mem[n959];
  always @(posedge w_clk)
    if (n974)
      mem[n913] <= w_data;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:112:29 */
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_async_fifo.vhd:83:21 */
endmodule

module obs_core_Brtl_1_4_0_256_32
  (input  clk,
   input  rst,
   input  [15:0] probes,
   input  tck,
   input  tdi,
   output tdo,
   input  sel,
   input  capture,
   input  shift,
   input  update,
   input  tlr);
  wire w_valid;
  wire [31:0] w_data;
  wire [2:0] w_free;
  wire r_pop;
  wire r_ahead;
  wire [31:0] r_data;
  wire [2:0] r_count;
  reg [31:0] t_out;
  reg [63:0] t_cmd;
  reg [6:0] t_bits;
  reg [4:0] t_pos;
  reg [2:0] t_word;
  reg t_record;
  reg [15:0] t_loaded;
  reg [15:0] t_count;
  reg [31:0] t_read;
  reg t_control;
  reg t_reg_tog;
  reg t_latch_tog;
  reg t_ovf_1;
  reg t_ovf_2;
  reg t_run_1;
  reg t_run_2;
  reg t_tlr_q;
  wire [2:0] state;
  wire [31:0] time_now;
  wire [47:0] latched;
  wire [2:0] reg_sync;
  wire [2:0] latch_sync;
  wire running;
  wire [15:0] prev;
  wire [13:0] delta;
  wire [29:0] lost;
  wire overflowed;
  wire reload;
  wire dropping;
  wire [8:0] since_anchor;
  wire n226;
  wire n227;
  wire n228;
  wire n229;
  wire n230;
  wire n232;
  wire n233;
  wire n243;
  wire n244;
  wire n245;
  wire n246;
  wire [15:0] n247;
  wire n249;
  wire [15:0] n250;
  wire [15:0] n252;
  wire [14:0] n254;
  wire [15:0] n255;
  wire [31:0] n256;
  wire n257;
  wire [62:0] n258;
  wire [63:0] n259;
  wire n261;
  wire [6:0] n263;
  wire [6:0] n264;
  wire [4:0] n266;
  wire n268;
  wire [30:0] n269;
  wire n271;
  wire [31:0] n272;
  wire n274;
  wire [15:0] n275;
  wire [31:0] n277;
  wire n279;
  wire [2:0] n280;
  reg [31:0] n282;
  wire n284;
  wire n285;
  wire [15:0] n287;
  wire n290;
  wire [15:0] n291;
  wire [31:0] n292;
  wire [2:0] n294;
  wire [2:0] n295;
  wire n297;
  wire n299;
  wire n300;
  wire [30:0] n301;
  wire [31:0] n303;
  wire [31:0] n304;
  wire [2:0] n305;
  wire n306;
  wire n307;
  wire n309;
  wire n311;
  wire [7:0] n312;
  wire [7:0] n313;
  wire [31:0] n314;
  wire n316;
  wire n318;
  wire n319;
  wire n320;
  wire n322;
  wire n324;
  wire n325;
  wire n328;
  wire n329;
  wire n330;
  wire n331;
  wire n333;
  wire [30:0] n334;
  wire n336;
  wire n338;
  wire n340;
  wire n342;
  wire n344;
  wire n346;
  wire n348;
  wire n350;
  wire [31:0] n382;
  wire n384;
  wire n386;
  wire [9:0] n387;
  reg [31:0] n398;
  wire n400;
  wire n401;
  wire n402;
  wire [31:0] n403;
  wire n404;
  wire [31:0] n405;
  wire n406;
  wire n407;
  wire n408;
  wire [31:0] n409;
  wire n410;
  wire n411;
  wire n412;
  wire n416;
  wire n417;
  wire n418;
  wire n419;
  wire [31:0] n423;
  wire [63:0] n424;
  wire n425;
  wire [4:0] n426;
  wire n427;
  wire n428;
  wire n429;
  wire [31:0] n430;
  wire n431;
  wire n432;
  wire n433;
  wire [31:0] n438;
  wire [63:0] n439;
  wire [6:0] n441;
  wire [4:0] n443;
  wire [2:0] n445;
  wire n447;
  wire [15:0] n449;
  wire [15:0] n450;
  wire [31:0] n451;
  wire n452;
  wire n453;
  wire n454;
  wire [31:0] n461;
  wire [63:0] n462;
  wire [6:0] n464;
  wire [4:0] n466;
  wire [2:0] n468;
  wire n470;
  wire [15:0] n472;
  wire [15:0] n474;
  wire [31:0] n476;
  wire n478;
  wire n479;
  wire n480;
  wire [1:0] n516;
  wire [2:0] n517;
  wire [1:0] n518;
  wire [2:0] n519;
  wire [31:0] n521;
  wire n522;
  wire n523;
  wire n524;
  wire [47:0] n525;
  wire [47:0] n526;
  wire [30:0] n527;
  wire [31:0] n528;
  wire [31:0] n530;
  wire [31:0] n531;
  wire n532;
  wire n533;
  wire n534;
  wire n535;
  wire n536;
  wire n537;
  wire n538;
  wire n540;
  wire n541;
  wire n543;
  wire [29:0] n545;
  wire n546;
  wire n548;
  wire n551;
  wire [2:0] n553;
  wire [29:0] n554;
  wire n555;
  wire n556;
  wire n558;
  wire n559;
  wire n560;
  wire [29:0] n561;
  wire n562;
  wire n563;
  wire n565;
  wire n568;
  wire n569;
  wire n571;
  wire n572;
  wire [29:0] n574;
  wire [29:0] n575;
  wire n577;
  wire [2:0] n580;
  wire [2:0] n581;
  wire n583;
  wire n585;
  wire n586;
  wire [29:0] n588;
  wire [31:0] n590;
  wire [31:0] n592;
  wire [31:0] n593;
  wire n595;
  wire n597;
  wire [29:0] n598;
  wire [29:0] n600;
  wire [31:0] n602;
  wire n604;
  wire [31:0] n606;
  wire n608;
  wire n610;
  wire [15:0] n612;
  wire [31:0] n613;
  wire [31:0] n614;
  wire n616;
  wire [31:0] n617;
  wire [31:0] n619;
  wire [8:0] n620;
  wire [8:0] n621;
  wire n624;
  wire [31:0] n626;
  wire [2:0] n628;
  wire [29:0] n630;
  wire n632;
  wire n634;
  wire [8:0] n635;
  wire n637;
  wire [31:0] n638;
  wire n640;
  wire n641;
  wire n643;
  wire n645;
  wire [13:0] n647;
  wire [2:0] n649;
  wire [13:0] n650;
  wire [29:0] n652;
  wire n654;
  wire n656;
  wire [2:0] n658;
  wire [13:0] n659;
  wire [29:0] n660;
  wire n661;
  wire n662;
  wire [13:0] n664;
  wire [2:0] n665;
  wire [13:0] n666;
  wire [29:0] n667;
  wire n668;
  wire n669;
  wire n671;
  wire [31:0] n673;
  wire [2:0] n674;
  wire [13:0] n676;
  wire [29:0] n677;
  wire n678;
  wire n679;
  wire [8:0] n680;
  wire n682;
  wire [6:0] n683;
  reg n690;
  reg [31:0] n694;
  reg [2:0] n700;
  reg [15:0] n702;
  reg [13:0] n705;
  reg [29:0] n707;
  reg n709;
  reg n711;
  reg [8:0] n714;
  wire n716;
  wire [31:0] n719;
  wire [2:0] n722;
  wire [15:0] n723;
  wire [13:0] n724;
  wire [29:0] n725;
  wire n726;
  wire n727;
  wire [8:0] n728;
  wire [2:0] n729;
  wire [2:0] n730;
  wire n732;
  wire [2:0] n734;
  wire [31:0] n736;
  wire [47:0] n738;
  wire [2:0] n739;
  wire [2:0] n740;
  wire n742;
  wire [15:0] n744;
  wire [13:0] n746;
  wire [29:0] n748;
  wire n750;
  wire n753;
  wire n756;
  wire [8:0] n758;
  reg n781;
  reg [31:0] n782;
  reg [31:0] n783;
  reg [63:0] n784;
  reg [6:0] n785;
  reg [4:0] n786;
  reg [2:0] n787;
  reg n788;
  reg [15:0] n789;
  reg [15:0] n790;
  reg [31:0] n791;
  reg n792;
  reg n793;
  reg n794;
  reg n795;
  reg n796;
  reg n797;
  reg n798;
  reg n799;
  reg [2:0] n800;
  reg [31:0] n801;
  reg [47:0] n802;
  reg [2:0] n803;
  reg [2:0] n804;
  reg n805;
  reg [15:0] n806;
  reg [13:0] n807;
  reg [29:0] n808;
  reg n809;
  reg n810;
  reg n811;
  reg [8:0] n812;
  assign tdo = n226; //(module output)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:110:12 */
  assign w_valid = n781; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:111:12 */
  assign w_data = n782; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:113:12 */
  assign r_pop = n233; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:114:12 */
  assign r_ahead = t_record; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:119:12 */
  always @*
    t_out = n783; // (isignal)
  initial
    t_out = 32'b00000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:120:12 */
  always @*
    t_cmd = n784; // (isignal)
  initial
    t_cmd = 64'b0000000000000000000000000000000000000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:121:12 */
  always @*
    t_bits = n785; // (isignal)
  initial
    t_bits = 7'b0000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:122:12 */
  always @*
    t_pos = n786; // (isignal)
  initial
    t_pos = 5'b00000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:123:12 */
  always @*
    t_word = n787; // (isignal)
  initial
    t_word = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:124:12 */
  always @*
    t_record = n788; // (isignal)
  initial
    t_record = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:125:12 */
  always @*
    t_loaded = n789; // (isignal)
  initial
    t_loaded = 16'b0000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:126:12 */
  always @*
    t_count = n790; // (isignal)
  initial
    t_count = 16'b0000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:127:12 */
  always @*
    t_read = n791; // (isignal)
  initial
    t_read = 32'b00000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:128:12 */
  always @*
    t_control = n792; // (isignal)
  initial
    t_control = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:130:12 */
  always @*
    t_reg_tog = n793; // (isignal)
  initial
    t_reg_tog = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:131:12 */
  always @*
    t_latch_tog = n794; // (isignal)
  initial
    t_latch_tog = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:132:12 */
  always @*
    t_ovf_1 = n795; // (isignal)
  initial
    t_ovf_1 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:133:12 */
  always @*
    t_ovf_2 = n796; // (isignal)
  initial
    t_ovf_2 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:134:12 */
  always @*
    t_run_1 = n797; // (isignal)
  initial
    t_run_1 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:135:12 */
  always @*
    t_run_2 = n798; // (isignal)
  initial
    t_run_2 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:136:12 */
  always @*
    t_tlr_q = n799; // (isignal)
  initial
    t_tlr_q = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:140:12 */
  assign state = n800; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:141:12 */
  assign time_now = n801; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:142:12 */
  assign latched = n802; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:143:12 */
  assign reg_sync = n803; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:144:12 */
  assign latch_sync = n804; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:145:12 */
  assign running = n805; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:147:12 */
  assign prev = n806; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:148:12 */
  assign delta = n807; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:149:12 */
  assign lost = n808; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:150:12 */
  assign overflowed = n809; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:151:12 */
  assign reload = n810; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:152:12 */
  assign dropping = n811; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:153:12 */
  assign since_anchor = n812; // (signal)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:161:5 */
  obs_async_fifo_Brtl_32_2 fifo (
    .w_clk(clk),
    .w_rst(rst),
    .w_valid(w_valid),
    .w_data(w_data),
    .r_clk(tck),
    .r_pop(r_pop),
    .r_ahead(r_ahead),
    .w_free(w_free),
    .r_data(r_data),
    .r_count(r_count));
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:167:17 */
  assign n226 = t_out[0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:170:18 */
  assign n227 = sel & shift;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:170:28 */
  assign n228 = n227 & t_record;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:170:45 */
  assign n229 = ~tlr;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:170:41 */
  assign n230 = n228 & n229;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:170:64 */
  assign n232 = t_pos == 5'b11111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:170:53 */
  assign n233 = n232 ? n230 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:195:28 */
  assign n243 = ~t_tlr_q;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:196:34 */
  assign n244 = ~t_reg_tog;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:195:17 */
  assign n245 = n243 ? n244 : t_reg_tog;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:29 */
  assign n246 = capture & sel;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:199:28 */
  assign n247 = {13'b0, r_count};  // uext
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:199:28 */
  assign n249 = $unsigned(n247) > $unsigned(16'b1111111111111111);
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:202:30 */
  assign n250 = {13'b0, r_count};  // uext
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:199:17 */
  assign n252 = n249 ? 16'b1111111111111111 : n250;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:205:43 */
  assign n254 = {14'b10100101000000, t_run_2};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:205:53 */
  assign n255 = {n254, t_ovf_2};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:205:63 */
  assign n256 = {n255, n252};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:29 */
  assign n257 = shift & sel;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:212:37 */
  assign n258 = t_cmd[63:1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:212:30 */
  assign n259 = {tdi, n258};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:213:27 */
  assign n261 = $unsigned(t_bits) < $unsigned(7'b1000000);
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:214:38 */
  assign n263 = t_bits + 7'b0000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n264 = n425 ? n263 : t_bits;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:216:32 */
  assign n266 = t_pos + 5'b00001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:217:26 */
  assign n268 = t_pos == 5'b11111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:220:26 */
  assign n269 = {28'b0, t_word};  // uext
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:221:25 */
  assign n271 = n269 == 31'b0000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:222:72 */
  assign n272 = latched[31:0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:222:25 */
  assign n274 = n269 == 31'b0000000000000000000000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:223:82 */
  assign n275 = latched[47:32]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:223:56 */
  assign n277 = {16'b0000000000000000, n275};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:223:25 */
  assign n279 = n269 == 31'b0000000000000000000000000000010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:220:21 */
  assign n280 = {n279, n274, n271};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:220:21 */
  always @*
    case (n280)
      3'b100: n282 = n277;
      3'b010: n282 = n272;
      3'b001: n282 = t_read;
      default: n282 = 32'b00000000000000000000000000000000;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:226:31 */
  assign n284 = $unsigned(t_word) >= $unsigned(3'b011);
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:227:37 */
  assign n285 = $unsigned(t_loaded) < $unsigned(t_count);
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:230:50 */
  assign n287 = t_loaded + 16'b0000000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:227:25 */
  assign n290 = n285 ? 1'b1 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n291 = n429 ? n287 : t_loaded;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:226:21 */
  assign n292 = n300 ? r_data : n282;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:233:42 */
  assign n294 = t_word + 3'b001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:226:21 */
  assign n295 = n284 ? t_word : n294;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:226:21 */
  assign n297 = n284 ? n290 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:226:21 */
  assign n299 = n285 & n284;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:226:21 */
  assign n300 = n285 & n284;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:237:41 */
  assign n301 = t_out[31:1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:237:34 */
  assign n303 = {1'b0, n301};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:217:17 */
  assign n304 = n268 ? n292 : n303;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n305 = n427 ? n295 : t_word;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n306 = n428 ? n297 : t_record;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:217:17 */
  assign n307 = n299 & n268;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:239:29 */
  assign n309 = update & sel;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:240:27 */
  assign n311 = t_bits == 7'b1000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:241:41 */
  assign n312 = t_cmd[7:0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:242:43 */
  assign n313 = t_cmd[15:8]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:243:43 */
  assign n314 = t_cmd[47:16]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:244:27 */
  assign n316 = n312 == 8'b00000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:245:33 */
  assign n318 = n313 == 8'b00010000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:246:46 */
  assign n319 = t_cmd[16]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:247:42 */
  assign n320 = ~t_reg_tog;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:248:36 */
  assign n322 = n313 == 8'b00010001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:248:58 */
  assign n324 = $unsigned(n314) < $unsigned(32'b00000000000000000000000000000001);
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:248:49 */
  assign n325 = n324 & n322;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:250:42 */
  assign n328 = ~t_reg_tog;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:248:25 */
  assign n329 = n325 ? n328 : t_reg_tog;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:239:13 */
  assign n330 = n417 ? n319 : t_control;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:245:25 */
  assign n331 = n318 ? n320 : n329;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:252:30 */
  assign n333 = n312 == 8'b00000010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:253:30 */
  assign n334 = {23'b0, n313};  // uext
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:254:29 */
  assign n336 = n334 == 31'b0000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:255:29 */
  assign n338 = n334 == 31'b0000000000000000000000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:256:29 */
  assign n340 = n334 == 31'b0000000000000000000000000000010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:257:29 */
  assign n342 = n334 == 31'b0000000000000000000000000000011;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:258:29 */
  assign n344 = n334 == 31'b0000000000000000000000000000100;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:259:29 */
  assign n346 = n334 == 31'b0000000000000000000000000000101;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:260:29 */
  assign n348 = n334 == 31'b0000000000000000000000000000110;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:261:29 */
  assign n350 = n334 == 31'b0000000000000000000000000000111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:262:54 */
  assign n382 = {1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, 1'b0, t_control};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:262:29 */
  assign n384 = n334 == 31'b0000000000000000000000000010000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:263:29 */
  assign n386 = n334 == 31'b0000000000000000000000000010001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:253:25 */
  assign n387 = {n386, n384, n350, n348, n346, n344, n342, n340, n338, n336};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:253:25 */
  always @*
    case (n387)
      10'b1000000000: n398 = 32'b00000000000000000000000000000000;
      10'b0100000000: n398 = n382;
      10'b0010000000: n398 = 32'b00000000000000000000000000000001;
      10'b0001000000: n398 = 32'b00000000000000000000000000000101;
      10'b0000100000: n398 = 32'b00000000000000000000000000000000;
      10'b0000010000: n398 = 32'b00000000000000000000000000000100;
      10'b0000001000: n398 = 32'b00000000000000000000000000000001;
      10'b0000000100: n398 = 32'b00000000000000000000000000010000;
      10'b0000000010: n398 = 32'b00000000000000000000000000000001;
      10'b0000000001: n398 = 32'b01000110010011110100001001010011;
      default: n398 = 32'b00000000000000000000000000000000;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:266:30 */
  assign n400 = n312 == 8'b00000011;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:267:40 */
  assign n401 = ~t_latch_tog;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:266:21 */
  assign n402 = n400 ? n401 : t_latch_tog;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:252:21 */
  assign n403 = n333 ? n398 : t_read;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:252:21 */
  assign n404 = n333 ? t_latch_tog : n402;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:244:21 */
  assign n405 = n316 ? t_read : n403;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:244:21 */
  assign n406 = n318 & n316;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:239:13 */
  assign n407 = n418 ? n331 : t_reg_tog;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:244:21 */
  assign n408 = n316 ? t_latch_tog : n404;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:239:13 */
  assign n409 = n416 ? n405 : t_read;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:240:17 */
  assign n410 = n406 & n311;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:240:17 */
  assign n411 = n316 & n311;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:239:13 */
  assign n412 = n419 ? n408 : t_latch_tog;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:239:13 */
  assign n416 = n311 & n309;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:239:13 */
  assign n417 = n410 & n309;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:239:13 */
  assign n418 = n411 & n309;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:239:13 */
  assign n419 = n311 & n309;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n423 = n257 ? n304 : t_out;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n424 = n257 ? n259 : t_cmd;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n425 = n261 & n257;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n426 = n257 ? n266 : t_pos;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n427 = n268 & n257;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n428 = n268 & n257;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n429 = n307 & n257;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n430 = n257 ? t_read : n409;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n431 = n257 ? t_control : n330;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n432 = n257 ? t_reg_tog : n407;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:211:13 */
  assign n433 = n257 ? t_latch_tog : n412;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n438 = n246 ? n256 : n423;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n439 = n246 ? t_cmd : n424;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n441 = n246 ? 7'b0000000 : n264;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n443 = n246 ? 5'b00000 : n426;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n445 = n246 ? 3'b000 : n305;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n447 = n246 ? 1'b0 : n306;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n449 = n246 ? 16'b0000000000000000 : n291;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n450 = n246 ? n252 : t_count;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n451 = n246 ? t_read : n430;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n452 = n246 ? t_control : n431;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n453 = n246 ? t_reg_tog : n432;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:198:13 */
  assign n454 = n246 ? t_latch_tog : n433;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n461 = tlr ? 32'b00000000000000000000000000000000 : n438;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n462 = tlr ? t_cmd : n439;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n464 = tlr ? 7'b0000000 : n441;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n466 = tlr ? 5'b00000 : n443;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n468 = tlr ? 3'b000 : n445;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n470 = tlr ? 1'b0 : n447;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n472 = tlr ? 16'b0000000000000000 : n449;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n474 = tlr ? 16'b0000000000000000 : n450;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n476 = tlr ? 32'b00000000000000000000000000000000 : n451;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n478 = tlr ? 1'b0 : n452;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n479 = tlr ? n245 : n453;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:184:13 */
  assign n480 = tlr ? t_latch_tog : n454;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:282:33 */
  assign n516 = reg_sync[1:0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:282:46 */
  assign n517 = {n516, t_reg_tog};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:283:37 */
  assign n518 = latch_sync[1:0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:283:50 */
  assign n519 = {n518, t_latch_tog};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:284:34 */
  assign n521 = time_now + 32'b00000000000000000000000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:285:26 */
  assign n522 = latch_sync[2]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:285:43 */
  assign n523 = latch_sync[1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:285:30 */
  assign n524 = n522 != n523;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:286:28 */
  assign n525 = {16'b0, time_now};  // uext
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:285:13 */
  assign n526 = n524 ? n525 : latched;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:290:21 */
  assign n527 = {28'b0, w_free};  // uext
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:290:13 */
  assign n528 = {1'b0, n527};  // uext
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:292:30 */
  assign n530 = n528 - 32'b00000000000000000000000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:291:13 */
  assign n531 = w_valid ? n530 : n528;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:294:30 */
  assign n532 = probes != prev;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:300:24 */
  assign n533 = reg_sync[2]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:300:39 */
  assign n534 = reg_sync[1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:300:28 */
  assign n535 = n533 != n534;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:300:43 */
  assign n536 = n535 | reload;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:304:30 */
  assign n537 = ~t_control;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:306:31 */
  assign n538 = ~running;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:306:37 */
  assign n540 = n538 | 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:307:32 */
  assign n541 = ~running;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:306:17 */
  assign n543 = n546 ? 1'b0 : overflowed;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:306:17 */
  assign n545 = n540 ? 30'b000000000000000000000000000000 : lost;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:306:17 */
  assign n546 = n541 & n540;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:306:17 */
  assign n548 = n540 ? 1'b0 : dropping;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:306:17 */
  assign n551 = n540 ? 1'b1 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:300:13 */
  assign n553 = n559 ? 3'b000 : state;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:304:17 */
  assign n554 = n537 ? lost : n545;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:304:17 */
  assign n555 = n537 ? overflowed : n543;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:304:17 */
  assign n556 = n537 ? dropping : n548;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:304:17 */
  assign n558 = n537 ? 1'b0 : n551;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:300:13 */
  assign n559 = n537 & n536;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:300:13 */
  assign n560 = n536 ? t_control : running;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:300:13 */
  assign n561 = n536 ? n554 : lost;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:300:13 */
  assign n562 = n536 ? n555 : overflowed;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:300:13 */
  assign n563 = n536 ? n556 : dropping;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:300:13 */
  assign n565 = n536 ? n558 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:320:21 */
  assign n568 = state == 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:324:43 */
  assign n569 = n532 & dropping;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:324:64 */
  assign n571 = lost != 30'b111111111111111111111111111111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:324:55 */
  assign n572 = n571 & n569;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:325:42 */
  assign n574 = lost + 30'b000000000000000000000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:324:25 */
  assign n575 = n572 ? n574 : n561;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:328:33 */
  assign n577 = $signed(n531) >= $signed(32'b00000000000000000000000000000100);
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:329:29 */
  assign n580 = dropping ? 3'b010 : 3'b011;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:328:25 */
  assign n581 = n577 ? n580 : n553;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:322:21 */
  assign n583 = state == 3'b001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:336:45 */
  assign n585 = lost != 30'b111111111111111111111111111111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:336:36 */
  assign n586 = n585 & n532;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:337:74 */
  assign n588 = lost + 30'b000000000000000000000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:337:50 */
  assign n590 = {2'b10, n588};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:339:50 */
  assign n592 = {2'b10, lost};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:336:25 */
  assign n593 = n586 ? n590 : n592;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:335:21 */
  assign n595 = state == 3'b010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:344:21 */
  assign n597 = state == 3'b011;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:350:68 */
  assign n598 = time_now[29:0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:350:82 */
  assign n600 = n598 + 30'b000000000000000000000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:350:41 */
  assign n602 = {2'b11, n600};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:348:21 */
  assign n604 = state == 3'b100;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:354:82 */
  assign n606 = {16'b0000000000000000, probes};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:353:21 */
  assign n608 = state == 3'b101;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:362:37 */
  assign n610 = $signed(n531) <= $signed(32'b00000000000000000000000000000000);
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:368:51 */
  assign n612 = {2'b00, delta};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:368:77 */
  assign n613 = {n612, probes};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:370:49 */
  assign n614 = {23'b0, since_anchor};  // uext
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:370:49 */
  assign n616 = $signed(n614) < $signed(32'b00000000000000000000000100000000);
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:371:66 */
  assign n617 = {23'b0, since_anchor};  // uext
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:371:66 */
  assign n619 = n617 + 32'b00000000000000000000000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:371:53 */
  assign n620 = n619[8:0];  // trunc
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:370:33 */
  assign n621 = n616 ? n620 : since_anchor;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:362:29 */
  assign n624 = n610 ? 1'b0 : 1'b1;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:362:29 */
  assign n626 = n610 ? 32'b00000000000000000000000000000000 : n613;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:362:29 */
  assign n628 = n610 ? 3'b001 : n553;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:362:29 */
  assign n630 = n610 ? 30'b000000000000000000000000000001 : n561;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:362:29 */
  assign n632 = n610 ? 1'b1 : n562;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:362:29 */
  assign n634 = n610 ? 1'b1 : n563;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:362:29 */
  assign n635 = n610 ? since_anchor : n621;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:375:37 */
  assign n637 = delta == 14'b11111111111111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:375:65 */
  assign n638 = {23'b0, since_anchor};  // uext
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:375:65 */
  assign n640 = $signed(n638) >= $signed(32'b00000000000000000000000100000000);
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:375:49 */
  assign n641 = n637 | n640;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:379:37 */
  assign n643 = $signed(n531) >= $signed(32'b00000000000000000000000000000011);
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:381:41 */
  assign n645 = delta == 14'b11111111111111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:387:48 */
  assign n647 = delta + 14'b00000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:381:29 */
  assign n649 = n645 ? 3'b001 : n553;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:381:29 */
  assign n650 = n645 ? delta : n647;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:381:29 */
  assign n652 = n645 ? 30'b000000000000000000000000000000 : n561;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:381:29 */
  assign n654 = n645 ? 1'b1 : n562;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:381:29 */
  assign n656 = n645 ? 1'b1 : n563;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:379:29 */
  assign n658 = n643 ? 3'b011 : n649;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:379:29 */
  assign n659 = n643 ? delta : n650;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:379:29 */
  assign n660 = n643 ? n561 : n652;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:379:29 */
  assign n661 = n643 ? n562 : n654;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:379:29 */
  assign n662 = n643 ? n563 : n656;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:390:44 */
  assign n664 = delta + 14'b00000000000001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:375:25 */
  assign n665 = n641 ? n658 : n553;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:375:25 */
  assign n666 = n641 ? n659 : n664;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:375:25 */
  assign n667 = n641 ? n660 : n561;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:375:25 */
  assign n668 = n641 ? n661 : n562;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:375:25 */
  assign n669 = n641 ? n662 : n563;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:361:25 */
  assign n671 = n532 ? n624 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:361:25 */
  assign n673 = n532 ? n626 : 32'b00000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:361:25 */
  assign n674 = n532 ? n628 : n665;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:361:25 */
  assign n676 = n532 ? 14'b00000000000001 : n666;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:361:25 */
  assign n677 = n532 ? n630 : n667;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:361:25 */
  assign n678 = n532 ? n632 : n668;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:361:25 */
  assign n679 = n532 ? n634 : n669;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:361:25 */
  assign n680 = n532 ? n635 : since_anchor;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:360:21 */
  assign n682 = state == 3'b110;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:319:17 */
  assign n683 = {n682, n608, n604, n597, n595, n583, n568};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:319:17 */
  always @*
    case (n683)
      7'b1000000: n690 = n671;
      7'b0100000: n690 = 1'b1;
      7'b0010000: n690 = 1'b1;
      7'b0001000: n690 = 1'b1;
      7'b0000100: n690 = 1'b1;
      7'b0000010: n690 = 1'b0;
      7'b0000001: n690 = 1'b0;
      default: n690 = 1'bX;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:319:17 */
  always @*
    case (n683)
      7'b1000000: n694 = n673;
      7'b0100000: n694 = n606;
      7'b0010000: n694 = n602;
      7'b0001000: n694 = 32'b01100000000000000000000000000000;
      7'b0000100: n694 = n593;
      7'b0000010: n694 = 32'b00000000000000000000000000000000;
      7'b0000001: n694 = 32'b00000000000000000000000000000000;
      default: n694 = 32'bX;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:319:17 */
  always @*
    case (n683)
      7'b1000000: n700 = n674;
      7'b0100000: n700 = 3'b110;
      7'b0010000: n700 = 3'b101;
      7'b0001000: n700 = 3'b100;
      7'b0000100: n700 = 3'b011;
      7'b0000010: n700 = n581;
      7'b0000001: n700 = n553;
      default: n700 = 3'bX;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:319:17 */
  always @*
    case (n683)
      7'b1000000: n702 = probes;
      7'b0100000: n702 = probes;
      7'b0010000: n702 = prev;
      7'b0001000: n702 = prev;
      7'b0000100: n702 = probes;
      7'b0000010: n702 = probes;
      7'b0000001: n702 = prev;
      default: n702 = 16'bX;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:319:17 */
  always @*
    case (n683)
      7'b1000000: n705 = n676;
      7'b0100000: n705 = 14'b00000000000001;
      7'b0010000: n705 = delta;
      7'b0001000: n705 = delta;
      7'b0000100: n705 = delta;
      7'b0000010: n705 = delta;
      7'b0000001: n705 = delta;
      default: n705 = 14'bX;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:319:17 */
  always @*
    case (n683)
      7'b1000000: n707 = n677;
      7'b0100000: n707 = n561;
      7'b0010000: n707 = n561;
      7'b0001000: n707 = n561;
      7'b0000100: n707 = n561;
      7'b0000010: n707 = n575;
      7'b0000001: n707 = n561;
      default: n707 = 30'bX;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:319:17 */
  always @*
    case (n683)
      7'b1000000: n709 = n678;
      7'b0100000: n709 = n562;
      7'b0010000: n709 = n562;
      7'b0001000: n709 = n562;
      7'b0000100: n709 = n562;
      7'b0000010: n709 = n562;
      7'b0000001: n709 = n562;
      default: n709 = 1'bX;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:319:17 */
  always @*
    case (n683)
      7'b1000000: n711 = n679;
      7'b0100000: n711 = n563;
      7'b0010000: n711 = n563;
      7'b0001000: n711 = n563;
      7'b0000100: n711 = n563;
      7'b0000010: n711 = n563;
      7'b0000001: n711 = n563;
      default: n711 = 1'bX;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:319:17 */
  always @*
    case (n683)
      7'b1000000: n714 = n680;
      7'b0100000: n714 = 9'b000000000;
      7'b0010000: n714 = since_anchor;
      7'b0001000: n714 = since_anchor;
      7'b0000100: n714 = since_anchor;
      7'b0000010: n714 = since_anchor;
      7'b0000001: n714 = since_anchor;
      default: n714 = 9'bX;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:316:13 */
  assign n716 = n565 ? 1'b0 : n690;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:316:13 */
  assign n719 = n565 ? 32'b00000000000000000000000000000000 : n694;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:316:13 */
  assign n722 = n565 ? 3'b001 : n700;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:316:13 */
  assign n723 = n565 ? prev : n702;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:316:13 */
  assign n724 = n565 ? delta : n705;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:316:13 */
  assign n725 = n565 ? n561 : n707;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:316:13 */
  assign n726 = n565 ? n562 : n709;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:316:13 */
  assign n727 = n565 ? n563 : n711;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:316:13 */
  assign n728 = n565 ? since_anchor : n714;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:409:29 */
  assign n729 = {t_reg_tog, t_reg_tog, t_reg_tog};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:411:31 */
  assign n730 = {t_latch_tog, t_latch_tog, t_latch_tog};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n732 = rst ? 1'b0 : n716;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n734 = rst ? 3'b000 : n722;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n736 = rst ? 32'b00000000000000000000000000000000 : n521;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n738 = rst ? 48'b000000000000000000000000000000000000000000000000 : n526;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n739 = rst ? n729 : n517;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n740 = rst ? n730 : n519;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n742 = rst ? 1'b0 : n560;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n744 = rst ? 16'b0000000000000000 : n723;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n746 = rst ? 14'b00000000000000 : n724;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n748 = rst ? 30'b000000000000000000000000000000 : n725;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n750 = rst ? 1'b0 : n726;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n753 = rst ? 1'b1 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n756 = rst ? 1'b0 : n727;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:396:13 */
  assign n758 = rst ? 9'b000000000 : n728;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n781 <= n732;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n782 <= n719;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n783 <= n461;
  initial
    n783 = 32'b00000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n784 <= n462;
  initial
    n784 = 64'b0000000000000000000000000000000000000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n785 <= n464;
  initial
    n785 = 7'b0000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n786 <= n466;
  initial
    n786 = 5'b00000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n787 <= n468;
  initial
    n787 = 3'b000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n788 <= n470;
  initial
    n788 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n789 <= n472;
  initial
    n789 = 16'b0000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n790 <= n474;
  initial
    n790 = 16'b0000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n791 <= n476;
  initial
    n791 = 32'b00000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n792 <= n478;
  initial
    n792 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n793 <= n479;
  initial
    n793 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n794 <= n480;
  initial
    n794 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n795 <= overflowed;
  initial
    n795 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n796 <= t_ovf_1;
  initial
    n796 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n797 <= running;
  initial
    n797 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n798 <= t_run_1;
  initial
    n798 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:178:9 */
  always @(posedge tck)
    n799 <= tlr;
  initial
    n799 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n800 <= n734;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n801 <= n736;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n802 <= n738;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n803 <= n739;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n804 <= n740;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n805 <= n742;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n806 <= n744;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n807 <= n746;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n808 <= n748;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n809 <= n750;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n810 <= n753;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n811 <= n756;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_core.vhd:281:9 */
  always @(posedge clk)
    n812 <= n758;
endmodule

module obs_soft_tap_Brtl_c98648d6e8e1bb5e8fa622c1442406b499979bb0
  (input  tck,
   input  tms,
   input  tdi,
   output tdo,
   output tdo_oe,
   output user_tdi,
   input  user_tdo,
   output user_sel,
   output user_capture,
   output user_shift,
   output user_update,
   output user_tlr);
  reg [3:0] state;
  reg [3:0] ir;
  reg [3:0] ir_shift;
  reg [31:0] id_shift;
  reg bypass;
  reg tdo_q;
  reg tdo_oe_q;
  wire n29;
  wire n30;
  wire n34;
  wire n35;
  wire n39;
  wire n40;
  wire n44;
  wire n45;
  wire n49;
  wire n50;
  wire [3:0] n57;
  wire n59;
  wire [3:0] n62;
  wire n64;
  wire [3:0] n67;
  wire n69;
  wire [3:0] n72;
  wire n74;
  wire [3:0] n77;
  wire n79;
  wire [3:0] n82;
  wire n84;
  wire [3:0] n87;
  wire n89;
  wire [3:0] n92;
  wire n94;
  wire [3:0] n97;
  wire n99;
  wire [3:0] n102;
  wire n104;
  wire [3:0] n107;
  wire n109;
  wire [3:0] n112;
  wire n114;
  wire [3:0] n117;
  wire n119;
  wire [3:0] n122;
  wire n124;
  wire [3:0] n127;
  wire n129;
  wire [3:0] n132;
  wire n134;
  wire [15:0] n135;
  reg [3:0] n137;
  wire n139;
  wire n141;
  wire [2:0] n142;
  wire [3:0] n143;
  wire n145;
  wire n147;
  wire [3:0] n148;
  reg [3:0] n150;
  reg [3:0] n152;
  wire n154;
  wire n156;
  wire [30:0] n157;
  wire [31:0] n158;
  wire [31:0] n159;
  wire n160;
  wire [31:0] n162;
  wire n164;
  wire n175;
  wire n176;
  wire n178;
  wire n180;
  wire n181;
  wire n183;
  wire n184;
  wire n185;
  wire n187;
  wire n190;
  wire n191;
  wire n193;
  reg [3:0] n197;
  reg [3:0] n198;
  reg [3:0] n199;
  reg [31:0] n200;
  reg n201;
  reg n202;
  reg n203;
  assign tdo = tdo_q; //(module output)
  assign tdo_oe = tdo_oe_q; //(module output)
  assign user_tdi = tdi; //(module output)
  assign user_sel = n30; //(module output)
  assign user_capture = n35; //(module output)
  assign user_shift = n40; //(module output)
  assign user_update = n45; //(module output)
  assign user_tlr = n50; //(module output)
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:58:12 */
  always @*
    state = n197; // (isignal)
  initial
    state = 4'b1111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:59:12 */
  always @*
    ir = n198; // (isignal)
  initial
    ir = 4'b0001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:60:12 */
  always @*
    ir_shift = n199; // (isignal)
  initial
    ir_shift = 4'b0000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:61:12 */
  always @*
    id_shift = n200; // (isignal)
  initial
    id_shift = 32'b00000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:62:12 */
  always @*
    bypass = n201; // (isignal)
  initial
    bypass = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:63:12 */
  always @*
    tdo_q = n202; // (isignal)
  initial
    tdo_q = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:64:12 */
  always @*
    tdo_oe_q = n203; // (isignal)
  initial
    tdo_oe_q = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:67:29 */
  assign n29 = ir == 4'b0010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:67:21 */
  assign n30 = n29 ? 1'b1 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:68:36 */
  assign n34 = state == 4'b0110;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:68:25 */
  assign n35 = n34 ? 1'b1 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:69:34 */
  assign n39 = state == 4'b0010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:69:23 */
  assign n40 = n39 ? 1'b1 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:70:35 */
  assign n44 = state == 4'b0101;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:70:24 */
  assign n45 = n44 ? 1'b1 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:71:32 */
  assign n49 = state == 4'b1111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:71:21 */
  assign n50 = n49 ? 1'b1 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:80:34 */
  assign n57 = tms ? 4'b1111 : 4'b1100;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:80:17 */
  assign n59 = state == 4'b1111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:81:34 */
  assign n62 = tms ? 4'b0111 : 4'b1100;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:81:17 */
  assign n64 = state == 4'b1100;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:82:34 */
  assign n67 = tms ? 4'b0100 : 4'b0110;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:82:17 */
  assign n69 = state == 4'b0111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:83:34 */
  assign n72 = tms ? 4'b0001 : 4'b0010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:83:17 */
  assign n74 = state == 4'b0110;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:84:34 */
  assign n77 = tms ? 4'b0001 : 4'b0010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:84:17 */
  assign n79 = state == 4'b0010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:85:34 */
  assign n82 = tms ? 4'b0101 : 4'b0011;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:85:17 */
  assign n84 = state == 4'b0001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:86:34 */
  assign n87 = tms ? 4'b0000 : 4'b0011;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:86:17 */
  assign n89 = state == 4'b0011;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:87:34 */
  assign n92 = tms ? 4'b0101 : 4'b0010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:87:17 */
  assign n94 = state == 4'b0000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:88:34 */
  assign n97 = tms ? 4'b0111 : 4'b1100;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:88:17 */
  assign n99 = state == 4'b0101;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:89:34 */
  assign n102 = tms ? 4'b1111 : 4'b1110;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:89:17 */
  assign n104 = state == 4'b0100;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:90:34 */
  assign n107 = tms ? 4'b1001 : 4'b1010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:90:17 */
  assign n109 = state == 4'b1110;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:91:34 */
  assign n112 = tms ? 4'b1001 : 4'b1010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:91:17 */
  assign n114 = state == 4'b1010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:92:34 */
  assign n117 = tms ? 4'b1101 : 4'b1011;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:92:17 */
  assign n119 = state == 4'b1001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:93:34 */
  assign n122 = tms ? 4'b1000 : 4'b1011;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:93:17 */
  assign n124 = state == 4'b1011;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:94:34 */
  assign n127 = tms ? 4'b1101 : 4'b1010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:94:17 */
  assign n129 = state == 4'b1000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:95:34 */
  assign n132 = tms ? 4'b0111 : 4'b1100;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:95:17 */
  assign n134 = state == 4'b1101;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:79:13 */
  assign n135 = {n134, n129, n124, n119, n114, n109, n104, n99, n94, n89, n84, n79, n74, n69, n64, n59};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:79:13 */
  always @*
    case (n135)
      16'b1000000000000000: n137 = n132;
      16'b0100000000000000: n137 = n127;
      16'b0010000000000000: n137 = n122;
      16'b0001000000000000: n137 = n117;
      16'b0000100000000000: n137 = n112;
      16'b0000010000000000: n137 = n107;
      16'b0000001000000000: n137 = n102;
      16'b0000000100000000: n137 = n97;
      16'b0000000010000000: n137 = n92;
      16'b0000000001000000: n137 = n87;
      16'b0000000000100000: n137 = n82;
      16'b0000000000010000: n137 = n77;
      16'b0000000000001000: n137 = n72;
      16'b0000000000000100: n137 = n67;
      16'b0000000000000010: n137 = n62;
      16'b0000000000000001: n137 = n57;
      default: n137 = 4'b1111;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:101:17 */
  assign n139 = state == 4'b1111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:102:17 */
  assign n141 = state == 4'b1110;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:103:60 */
  assign n142 = ir_shift[3:1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:103:50 */
  assign n143 = {tdi, n142};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:103:17 */
  assign n145 = state == 4'b1010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:104:17 */
  assign n147 = state == 4'b1101;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:100:13 */
  assign n148 = {n147, n145, n141, n139};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:100:13 */
  always @*
    case (n148)
      4'b1000: n150 = ir_shift;
      4'b0100: n150 = ir;
      4'b0010: n150 = ir;
      4'b0001: n150 = 4'b0001;
      default: n150 = ir;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:100:13 */
  always @*
    case (n148)
      4'b1000: n152 = ir_shift;
      4'b0100: n152 = n143;
      4'b0010: n152 = 4'b0001;
      4'b0001: n152 = ir_shift;
      default: n152 = ir_shift;
    endcase
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:109:22 */
  assign n154 = state == 4'b0110;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:112:25 */
  assign n156 = state == 4'b0010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:113:43 */
  assign n157 = id_shift[31:1]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:113:33 */
  assign n158 = {tdi, n157};
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:112:13 */
  assign n159 = n156 ? n158 : id_shift;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:112:13 */
  assign n160 = n156 ? tdi : bypass;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:109:13 */
  assign n162 = n154 ? 32'b00001111000010110101000000000001 : n159;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:109:13 */
  assign n164 = n154 ? 1'b0 : n160;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:122:22 */
  assign n175 = state == 4'b1010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:123:34 */
  assign n176 = ir_shift[0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:125:25 */
  assign n178 = state == 4'b0010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:126:23 */
  assign n180 = ir == 4'b0001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:127:38 */
  assign n181 = id_shift[0]; // extract
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:128:26 */
  assign n183 = ir == 4'b0010;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:128:17 */
  assign n184 = n183 ? user_tdo : bypass;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:126:17 */
  assign n185 = n180 ? n181 : n184;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:125:13 */
  assign n187 = n178 ? n185 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:125:13 */
  assign n190 = n178 ? 1'b1 : 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:122:13 */
  assign n191 = n175 ? n176 : n187;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:122:13 */
  assign n193 = n175 ? 1'b1 : n190;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:77:9 */
  always @(posedge tck)
    n197 <= n137;
  initial
    n197 = 4'b1111;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:77:9 */
  always @(posedge tck)
    n198 <= n150;
  initial
    n198 = 4'b0001;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:77:9 */
  always @(posedge tck)
    n199 <= n152;
  initial
    n199 = 4'b0000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:77:9 */
  always @(posedge tck)
    n200 <= n162;
  initial
    n200 = 32'b00000000000000000000000000000000;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:77:9 */
  always @(posedge tck)
    n201 <= n164;
  initial
    n201 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:121:9 */
  always @(negedge tck)
    n202 <= n191;
  initial
    n202 = 1'b0;
  /*# C:/Users/M/Desktop/DebugProbe-with-FPGA/target_ip/fprobe_obs/rtl/obs_soft_tap.vhd:121:9 */
  always @(negedge tck)
    n203 <= n193;
  initial
    n203 = 1'b0;
endmodule

module obs_check_core
  (input  tck,
   input  tms,
   input  tdi,
   output tdo,
   output tdo_oe);
  wire u_tdi;
  wire u_tdo;
  wire u_sel;
  wire u_capture;
  wire u_shift;
  wire u_update;
  wire u_tlr;
  wire \tap.tdo ;
  wire \tap.tdo_oe ;
  localparam [15:0] n10 = 16'b0000000000000000;
  assign tdo = \tap.tdo ; //(module output)
  assign tdo_oe = \tap.tdo_oe ; //(module output)
  /*# obs_check_core.vhd:10:3 */
  obs_soft_tap_Brtl_c98648d6e8e1bb5e8fa622c1442406b499979bb0 tap (
    .tck(tck),
    .tms(tms),
    .tdi(tdi),
    .user_tdo(u_tdo),
    .tdo(\tap.tdo ),
    .tdo_oe(\tap.tdo_oe ),
    .user_tdi(u_tdi),
    .user_sel(u_sel),
    .user_capture(u_capture),
    .user_shift(u_shift),
    .user_update(u_update),
    .user_tlr(u_tlr));
  /*# obs_check_core.vhd:14:3 */
  obs_core_Brtl_1_4_0_256_32 core (
    .clk(tck),
    .rst(u_tlr),
    .probes(16'b0000000000000000),
    .tck(tck),
    .tdi(u_tdi),
    .sel(u_sel),
    .capture(u_capture),
    .shift(u_shift),
    .update(u_update),
    .tlr(u_tlr),
    .tdo(u_tdo));
endmodule

