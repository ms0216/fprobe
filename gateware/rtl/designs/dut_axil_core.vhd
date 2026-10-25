-- DUT の AXI-Lite 付きコア: dut_axil_slave + dut_stream_core（PS の 0x40000000 から 4 KB に見える）。
-- トップ pynq_z2_dut_top は、これに PS7・MMCM・端子をつなぐだけ（GHDL で試験できる範囲をここに集める）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity dut_axil_core is
  generic (
    FIFO_DEPTH : positive := 256;
    PIN_COUNT  : positive := 16                -- ピン診断ブロック（語番地 8〜11）の端子の数
  );
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    s_axi_awaddr  : in  std_logic_vector(31 downto 0);
    s_axi_awvalid : in  std_logic;
    s_axi_awready : out std_logic;
    s_axi_wdata   : in  std_logic_vector(31 downto 0);
    s_axi_wstrb   : in  std_logic_vector(3 downto 0);
    s_axi_wvalid  : in  std_logic;
    s_axi_wready  : out std_logic;
    s_axi_bresp   : out std_logic_vector(1 downto 0);
    s_axi_bvalid  : out std_logic;
    s_axi_bready  : in  std_logic;
    s_axi_araddr  : in  std_logic_vector(31 downto 0);
    s_axi_arvalid : in  std_logic;
    s_axi_arready : out std_logic;
    s_axi_rdata   : out std_logic_vector(31 downto 0);
    s_axi_rresp   : out std_logic_vector(1 downto 0);
    s_axi_rvalid  : out std_logic;
    s_axi_rready  : in  std_logic;
    dbg           : out std_logic_vector(5 downto 0);
    pins          : inout std_logic_vector(PIN_COUNT - 1 downto 0)
  );
end entity;

architecture rtl of dut_axil_core is
  signal reg_wr_en, reg_rd_en : std_logic;
  signal reg_addr : unsigned(3 downto 0);
  signal reg_wdata, reg_rdata : std_logic_vector(31 downto 0);
  signal core_rdata, pins_rdata : std_logic_vector(31 downto 0);
  signal core_wr, core_rd, pins_wr : std_logic;
begin
  -- 語番地 0〜7 = ストリームのコア、8〜11 = ピン診断ブロック（reg_addr(3) で分ける）
  core_wr <= reg_wr_en and not reg_addr(3);
  core_rd <= reg_rd_en and not reg_addr(3);
  pins_wr <= reg_wr_en and reg_addr(3);
  reg_rdata <= pins_rdata when reg_addr(3) = '1' else core_rdata;

  slave_i : entity work.dut_axil_slave
    port map (clk => clk, rst => rst,
              s_axi_awaddr => s_axi_awaddr, s_axi_awvalid => s_axi_awvalid, s_axi_awready => s_axi_awready,
              s_axi_wdata => s_axi_wdata, s_axi_wstrb => s_axi_wstrb, s_axi_wvalid => s_axi_wvalid,
              s_axi_wready => s_axi_wready, s_axi_bresp => s_axi_bresp, s_axi_bvalid => s_axi_bvalid,
              s_axi_bready => s_axi_bready, s_axi_araddr => s_axi_araddr, s_axi_arvalid => s_axi_arvalid,
              s_axi_arready => s_axi_arready, s_axi_rdata => s_axi_rdata, s_axi_rresp => s_axi_rresp,
              s_axi_rvalid => s_axi_rvalid, s_axi_rready => s_axi_rready,
              reg_wr_en => reg_wr_en, reg_addr => reg_addr, reg_wdata => reg_wdata, reg_rd_en => reg_rd_en,
              reg_rdata => reg_rdata);

  core_i : entity work.dut_stream_core
    generic map (FIFO_DEPTH => FIFO_DEPTH)
    port map (clk => clk, rst => rst, reg_wr_en => core_wr, reg_addr => reg_addr, reg_wdata => reg_wdata,
              reg_rd_en => core_rd, reg_rdata => core_rdata, dbg => dbg);

  pins_i : entity work.dut_pins
    generic map (N => PIN_COUNT)
    port map (clk => clk, rst => rst, wr_en => pins_wr, addr => reg_addr(1 downto 0), wdata => reg_wdata,
              rdata => pins_rdata, pin => pins);
end architecture;
