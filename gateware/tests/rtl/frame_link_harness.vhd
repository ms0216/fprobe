-- テスト専用: zynq_frame_link + frame_mux（PYNQ-Z2 のトップと同じつなぎ）。PS（AXI4-Lite）から入れたフレームが各チャンネルへ出て、
-- 各チャンネルの入力が PS から読めるフレームになるかを試す。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity frame_link_harness is
  generic (
    MAX_PAYLOAD : positive := 512;
    RAM_BITS    : positive := 10
  );
  port (
    clk, rst        : in  std_logic;
    s_axi_awaddr    : in  std_logic_vector(11 downto 0);
    s_axi_awvalid   : in  std_logic;
    s_axi_awready   : out std_logic;
    s_axi_wdata     : in  std_logic_vector(31 downto 0);
    s_axi_wstrb     : in  std_logic_vector(3 downto 0);
    s_axi_wvalid    : in  std_logic;
    s_axi_wready    : out std_logic;
    s_axi_bresp     : out std_logic_vector(1 downto 0);
    s_axi_bvalid    : out std_logic;
    s_axi_bready    : in  std_logic;
    s_axi_araddr    : in  std_logic_vector(11 downto 0);
    s_axi_arvalid   : in  std_logic;
    s_axi_arready   : out std_logic;
    s_axi_rdata     : out std_logic_vector(31 downto 0);
    s_axi_rresp     : out std_logic_vector(1 downto 0);
    s_axi_rvalid    : out std_logic;
    s_axi_rready    : in  std_logic;
    ctrl_out_valid  : out std_logic;
    ctrl_out_ready  : in  std_logic;
    ctrl_out_data   : out std_logic_vector(7 downto 0);
    ctrl_out_last   : out std_logic;
    ctrl_in_valid   : in  std_logic;
    ctrl_in_ready   : out std_logic;
    ctrl_in_data    : in  std_logic_vector(7 downto 0);
    ctrl_in_last    : in  std_logic;
    dap_out_valid   : out std_logic;
    dap_out_ready   : in  std_logic;
    dap_out_data    : out std_logic_vector(7 downto 0);
    dap_out_last    : out std_logic;
    dap_in_valid    : in  std_logic;
    dap_in_ready    : out std_logic;
    dap_in_data     : in  std_logic_vector(7 downto 0);
    dap_in_last     : in  std_logic;
    swo_in_valid    : in  std_logic;
    swo_in_ready    : out std_logic;
    swo_in_data     : in  std_logic_vector(7 downto 0);
    stream_in_valid : in  std_logic;
    stream_in_ready : out std_logic;
    stream_in_data  : in  std_logic_vector(7 downto 0);
    uart_out_valid  : out std_logic;
    uart_out_ready  : in  std_logic;
    uart_out_data   : out std_logic_vector(7 downto 0);
    uart_in_valid   : in  std_logic;
    uart_in_ready   : out std_logic;
    uart_in_data    : in  std_logic_vector(7 downto 0);
    line_rate       : out unsigned(31 downto 0);
    line_changed    : out std_logic
  );
end entity;

architecture rtl of frame_link_harness is
  signal m_we, m_commit, m_busy, m_tx_valid, m_tx_done : std_logic;
  signal m_waddr, m_raddr : unsigned(RAM_BITS - 1 downto 0);
  signal m_wdata, m_rdata : std_logic_vector(7 downto 0);
  signal m_rx_len : natural range 0 to MAX_PAYLOAD + 1;
  signal m_tx_len : natural range 1 to MAX_PAYLOAD + 1;
  signal mem_base : unsigned(31 downto 0);
  signal diag : std_logic_vector(7 downto 0);
begin
  link_i : entity work.zynq_frame_link
    generic map (MAX_PAYLOAD => MAX_PAYLOAD, RAM_BITS => RAM_BITS, MEM_BYTES => 0)
    port map (clk => clk, rst => rst,
              s_axi_awaddr => s_axi_awaddr, s_axi_awvalid => s_axi_awvalid, s_axi_awready => s_axi_awready,
              s_axi_wdata => s_axi_wdata, s_axi_wstrb => s_axi_wstrb, s_axi_wvalid => s_axi_wvalid,
              s_axi_wready => s_axi_wready, s_axi_bresp => s_axi_bresp, s_axi_bvalid => s_axi_bvalid,
              s_axi_bready => s_axi_bready, s_axi_araddr => s_axi_araddr, s_axi_arvalid => s_axi_arvalid,
              s_axi_arready => s_axi_arready, s_axi_rdata => s_axi_rdata, s_axi_rresp => s_axi_rresp,
              s_axi_rvalid => s_axi_rvalid, s_axi_rready => s_axi_rready,
              rx_we => m_we, rx_waddr => m_waddr, rx_wdata => m_wdata, rx_commit => m_commit, rx_len => m_rx_len,
              rx_busy => m_busy, tx_valid => m_tx_valid, tx_len => m_tx_len, tx_raddr => m_raddr,
              tx_rdata => m_rdata, tx_done => m_tx_done, mem_base => mem_base);

  mux_i : entity work.frame_mux
    generic map (MAX_PAYLOAD => MAX_PAYLOAD, RAM_BITS => RAM_BITS)
    port map (clk => clk, rst => rst,
              rx_we => m_we, rx_waddr => m_waddr, rx_wdata => m_wdata, rx_commit => m_commit, rx_len => m_rx_len,
              rx_busy => m_busy, tx_valid => m_tx_valid, tx_len => m_tx_len, tx_raddr => m_raddr,
              tx_rdata => m_rdata, tx_done => m_tx_done,
              ctrl_out_valid => ctrl_out_valid, ctrl_out_ready => ctrl_out_ready, ctrl_out_data => ctrl_out_data,
              ctrl_out_last => ctrl_out_last, ctrl_in_valid => ctrl_in_valid, ctrl_in_ready => ctrl_in_ready,
              ctrl_in_data => ctrl_in_data, ctrl_in_last => ctrl_in_last,
              dap_out_valid => dap_out_valid, dap_out_ready => dap_out_ready, dap_out_data => dap_out_data,
              dap_out_last => dap_out_last, dap_in_valid => dap_in_valid, dap_in_ready => dap_in_ready,
              dap_in_data => dap_in_data, dap_in_last => dap_in_last,
              swo_in_valid => swo_in_valid, swo_in_ready => swo_in_ready, swo_in_data => swo_in_data,
              stream_in_valid => stream_in_valid, stream_in_ready => stream_in_ready,
              stream_in_data => stream_in_data,
              uart_out_valid => uart_out_valid, uart_out_ready => uart_out_ready, uart_out_data => uart_out_data,
              uart_in_valid => uart_in_valid, uart_in_ready => uart_in_ready, uart_in_data => uart_in_data,
              line_rate => line_rate, line_changed => line_changed, diag => diag);
end architecture;
