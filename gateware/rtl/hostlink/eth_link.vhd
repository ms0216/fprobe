-- EthernetLink: 論理チャンネルを UDP で運ぶホストリンク（ボード非依存。clk = sys、ref_clk = RMII の 50 MHz）。
-- 仕様: docs/superpowers/specs/2026-10-17-m11-ethernet-link-design.md
--
-- 構成: frame_mux（チャンネルの多重化、SerialLink と共通）↔ udp_stack（ARP・ICMP echo・UDP）↔ async FIFO ↔
--   rmii_rx / rmii_tx（ref_clk のドメイン）。コア側の I/F は serial_link と同じ。
-- 送信は、フレームの全バイトを FIFO に入れてから送り出しの 1 件を別の FIFO で渡す（rmii_tx は途切れずに送る）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity eth_link is
  generic (
    MAC         : std_logic_vector(47 downto 0);
    IP          : std_logic_vector(31 downto 0);
    PORT_NUM    : natural := 63920;
    MAX_PAYLOAD : positive := 512;
    IDLE_CYCLES : positive := 64;
    UART_DEPTH  : positive := 512
  );
  port (
    clk             : in  std_logic;
    rst             : in  std_logic;
    -- RMII（ref_clk は PHY が出す 50 MHz）
    ref_clk         : in  std_logic;
    crs_dv          : in  std_logic;
    rxd             : in  std_logic_vector(1 downto 0);
    txd             : out std_logic_vector(1 downto 0);
    tx_en           : out std_logic;
    -- 状態
    peer_known      : out std_logic;
    diag            : out std_logic_vector(15 downto 0);   -- [15:8] udp_stack、[7:0] frame_mux（診断用）
    -- CONTROL（ctrl_out: PC からの要求、ctrl_in: 応答）
    ctrl_out_valid  : out std_logic;
    ctrl_out_ready  : in  std_logic;
    ctrl_out_data   : out std_logic_vector(7 downto 0);
    ctrl_out_last   : out std_logic;
    ctrl_in_valid   : in  std_logic;
    ctrl_in_ready   : out std_logic;
    ctrl_in_data    : in  std_logic_vector(7 downto 0);
    ctrl_in_last    : in  std_logic;
    -- CMSIS-DAP
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
    -- ストリーム
    stream_in_valid : in  std_logic;
    stream_in_ready : out std_logic;
    stream_in_data  : in  std_logic_vector(7 downto 0);
    -- UART0（ターゲットの UART）
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

architecture rtl of eth_link is
  constant RAM_BITS : positive := clog2(MAX_PAYLOAD + 2);
  signal ref_rst : std_logic;
  -- frame_mux ↔ udp_stack
  signal m_we, m_commit, m_busy, m_tx_valid, m_tx_done : std_logic;
  signal m_waddr, m_raddr : unsigned(RAM_BITS - 1 downto 0);
  signal m_wdata, m_rdata : std_logic_vector(7 downto 0);
  signal m_rx_len : natural range 0 to MAX_PAYLOAD + 1;
  signal m_tx_len : natural range 1 to MAX_PAYLOAD + 1;
  -- 受信（ref → sys）
  signal rr_valid, rr_ready, rs_valid, rs_ready : std_logic;
  signal rr_word, rs_word : std_logic_vector(9 downto 0);   -- good | last | data
  -- 送信（sys → ref）
  signal ts_valid, ts_ready, ts_last, tr_valid, tr_ready : std_logic;
  signal ts_data : std_logic_vector(7 downto 0);
  signal tr_word : std_logic_vector(8 downto 0);             -- last | data
  signal ks_valid, ks_ready, kr_valid, kr_ready : std_logic;
  signal kr_word : std_logic_vector(0 downto 0);
begin
  ref_reset_i : entity work.reset_sync
    port map (clk => ref_clk, arst_in => rst, rst => ref_rst);

  mux_i : entity work.frame_mux
    generic map (MAX_PAYLOAD => MAX_PAYLOAD, RAM_BITS => RAM_BITS, IDLE_CYCLES => IDLE_CYCLES,
                 UART_DEPTH => UART_DEPTH)
    port map (clk => clk, rst => rst,
              rx_we => m_we, rx_waddr => m_waddr, rx_wdata => m_wdata, rx_commit => m_commit, rx_len => m_rx_len,
              rx_busy => m_busy,
              tx_valid => m_tx_valid, tx_len => m_tx_len, tx_raddr => m_raddr, tx_rdata => m_rdata,
              tx_done => m_tx_done,
              ctrl_out_valid => ctrl_out_valid, ctrl_out_ready => ctrl_out_ready, ctrl_out_data => ctrl_out_data,
              ctrl_out_last => ctrl_out_last,
              ctrl_in_valid => ctrl_in_valid, ctrl_in_ready => ctrl_in_ready, ctrl_in_data => ctrl_in_data,
              ctrl_in_last => ctrl_in_last,
              dap_out_valid => dap_out_valid, dap_out_ready => dap_out_ready, dap_out_data => dap_out_data,
              dap_out_last => dap_out_last,
              dap_in_valid => dap_in_valid, dap_in_ready => dap_in_ready, dap_in_data => dap_in_data,
              dap_in_last => dap_in_last,
              swo_in_valid => swo_in_valid, swo_in_ready => swo_in_ready, swo_in_data => swo_in_data,
              stream_in_valid => stream_in_valid, stream_in_ready => stream_in_ready, stream_in_data => stream_in_data,
              uart_out_valid => uart_out_valid, uart_out_ready => uart_out_ready, uart_out_data => uart_out_data,
              uart_in_valid => uart_in_valid, uart_in_ready => uart_in_ready, uart_in_data => uart_in_data,
              line_rate => line_rate, line_changed => line_changed, diag => diag(7 downto 0));

  stack_i : entity work.udp_stack
    generic map (MAC => MAC, IP => IP, PORT_NUM => PORT_NUM, MAX_PAYLOAD => MAX_PAYLOAD, RAM_BITS => RAM_BITS)
    port map (clk => clk, rst => rst,
              in_valid => rs_valid, in_ready => rs_ready, in_data => rs_word(7 downto 0), in_last => rs_word(8),
              in_good => rs_word(9),
              out_valid => ts_valid, out_ready => ts_ready, out_data => ts_data, out_last => ts_last,
              start_valid => ks_valid, start_ready => ks_ready,
              rx_we => m_we, rx_waddr => m_waddr, rx_wdata => m_wdata, rx_commit => m_commit, rx_len => m_rx_len,
              rx_busy => m_busy,
              tx_valid => m_tx_valid, tx_len => m_tx_len, tx_raddr => m_raddr, tx_rdata => m_rdata,
              tx_done => m_tx_done, peer_known => peer_known, diag => diag(15 downto 8));

  rx_i : entity work.rmii_rx
    port map (clk => ref_clk, rst => ref_rst, crs_dv => crs_dv, rxd => rxd,
              out_valid => rr_valid, out_ready => rr_ready, out_data => rr_word(7 downto 0), out_last => rr_word(8),
              out_good => rr_word(9));
  rx_fifo_i : entity work.async_fifo
    generic map (WIDTH => 10, DEPTH => 2048)
    port map (in_clk => ref_clk, in_rst => ref_rst, in_valid => rr_valid, in_ready => rr_ready, in_data => rr_word,
              out_clk => clk, out_rst => rst, out_valid => rs_valid, out_ready => rs_ready, out_data => rs_word);

  tx_fifo_i : entity work.async_fifo
    generic map (WIDTH => 9, DEPTH => 1024)
    port map (in_clk => clk, in_rst => rst, in_valid => ts_valid, in_ready => ts_ready, in_data => ts_last & ts_data,
              out_clk => ref_clk, out_rst => ref_rst, out_valid => tr_valid, out_ready => tr_ready, out_data => tr_word);
  start_fifo_i : entity work.async_fifo
    generic map (WIDTH => 1, DEPTH => 4)
    port map (in_clk => clk, in_rst => rst, in_valid => ks_valid, in_ready => ks_ready, in_data => "1",
              out_clk => ref_clk, out_rst => ref_rst, out_valid => kr_valid, out_ready => kr_ready, out_data => kr_word);
  tx_i : entity work.rmii_tx
    port map (clk => ref_clk, rst => ref_rst, start_valid => kr_valid, start_ready => kr_ready,
              in_valid => tr_valid, in_ready => tr_ready, in_data => tr_word(7 downto 0), in_last => tr_word(8),
              txd => txd, tx_en => tx_en);
end architecture;
