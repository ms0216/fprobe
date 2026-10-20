-- テスト専用: zynq_frame_link（AXI4-Lite）→ frame_mux の DAP チャンネルに dap_harness（DAP 処理 + SWD、ターゲットモデル用の
-- 線）をつなぐ。CONTROL は読み捨てて応答しない。PS の中継と同じ手順の AXI の読み書きで、DAP の端から端までを確かめる。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity zynq_dap_harness is
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    s_axi_awaddr  : in  std_logic_vector(11 downto 0);
    s_axi_awvalid : in  std_logic;
    s_axi_awready : out std_logic;
    s_axi_wdata   : in  std_logic_vector(31 downto 0);
    s_axi_wstrb   : in  std_logic_vector(3 downto 0);
    s_axi_wvalid  : in  std_logic;
    s_axi_wready  : out std_logic;
    s_axi_bresp   : out std_logic_vector(1 downto 0);
    s_axi_bvalid  : out std_logic;
    s_axi_bready  : in  std_logic;
    s_axi_araddr  : in  std_logic_vector(11 downto 0);
    s_axi_arvalid : in  std_logic;
    s_axi_arready : out std_logic;
    s_axi_rdata   : out std_logic_vector(31 downto 0);
    s_axi_rresp   : out std_logic_vector(1 downto 0);
    s_axi_rvalid  : out std_logic;
    s_axi_rready  : in  std_logic;
    swclk         : out std_logic;
    swdio         : out std_logic;
    target_swdio  : in  std_logic;
    nreset_ext    : in  std_logic
  );
end entity;

architecture rtl of zynq_dap_harness is
  signal m_we, m_commit, m_busy, m_tx_valid, m_tx_done : std_logic;
  signal m_waddr, m_raddr : unsigned(9 downto 0);
  signal m_wdata, m_rdata : std_logic_vector(7 downto 0);
  signal m_rx_len : natural range 0 to 513;
  signal m_tx_len : natural range 1 to 513;
  signal req_valid, req_ready, req_last, rsp_valid, rsp_ready, rsp_last : std_logic;
  signal req_data, rsp_data : std_logic_vector(7 downto 0);
begin
  link_i : entity work.zynq_frame_link
    port map (clk => clk, rst => rst,
              s_axi_awaddr => s_axi_awaddr, s_axi_awvalid => s_axi_awvalid, s_axi_awready => s_axi_awready,
              s_axi_wdata => s_axi_wdata, s_axi_wstrb => s_axi_wstrb, s_axi_wvalid => s_axi_wvalid,
              s_axi_wready => s_axi_wready, s_axi_bresp => s_axi_bresp, s_axi_bvalid => s_axi_bvalid,
              s_axi_bready => s_axi_bready, s_axi_araddr => s_axi_araddr, s_axi_arvalid => s_axi_arvalid,
              s_axi_arready => s_axi_arready, s_axi_rdata => s_axi_rdata, s_axi_rresp => s_axi_rresp,
              s_axi_rvalid => s_axi_rvalid, s_axi_rready => s_axi_rready,
              rx_we => m_we, rx_waddr => m_waddr, rx_wdata => m_wdata, rx_commit => m_commit, rx_len => m_rx_len,
              rx_busy => m_busy, tx_valid => m_tx_valid, tx_len => m_tx_len, tx_raddr => m_raddr,
              tx_rdata => m_rdata, tx_done => m_tx_done, mem_base => open);

  mux_i : entity work.frame_mux
    port map (clk => clk, rst => rst,
              rx_we => m_we, rx_waddr => m_waddr, rx_wdata => m_wdata, rx_commit => m_commit, rx_len => m_rx_len,
              rx_busy => m_busy, tx_valid => m_tx_valid, tx_len => m_tx_len, tx_raddr => m_raddr,
              tx_rdata => m_rdata, tx_done => m_tx_done,
              ctrl_out_valid => open, ctrl_out_ready => '1', ctrl_out_data => open, ctrl_out_last => open,
              ctrl_in_valid => '0', ctrl_in_ready => open, ctrl_in_data => x"00", ctrl_in_last => '0',
              dap_out_valid => req_valid, dap_out_ready => req_ready, dap_out_data => req_data, dap_out_last => req_last,
              dap_in_valid => rsp_valid, dap_in_ready => rsp_ready, dap_in_data => rsp_data, dap_in_last => rsp_last,
              swo_in_valid => '0', swo_in_ready => open, swo_in_data => x"00",
              stream_in_valid => '0', stream_in_ready => open, stream_in_data => x"00",
              uart_out_valid => open, uart_out_ready => '1', uart_out_data => open,
              uart_in_valid => '0', uart_in_ready => open, uart_in_data => x"00",
              line_rate => open, line_changed => open, diag => open);

  dap_i : entity work.dap_harness
    port map (clk => clk, rst => rst, packet_size => 512,
              in_valid => req_valid, in_ready => req_ready, in_data => req_data, in_last => req_last,
              out_valid => rsp_valid, out_ready => rsp_ready, out_data => rsp_data, out_last => rsp_last,
              swclk => swclk, swdio => swdio, tdi => open, target_swdio => target_swdio, nreset => open,
              nreset_ext => nreset_ext);
end architecture;
