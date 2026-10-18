-- Tang Primer 20K Dock 用 ULPI 疎通確認トップ。
--
--   ulpi_clk（USB3317 の 60 MHz CLKOUT）で全体を動かす（I/O タイミングは ulpi_link が担う）。
--   起動時に RESETB を一度だけパルスし、解除後に usb ドメインのリセットを外す。
--   結果は BL702 の仮想 COM（115200 baud）へ 1 行で報告する。'?' で再実行。
--   LED（アクティブロー）: led(0)=PHY 準備完了, led(1)=常時点灯, その他は消灯
library ieee;
use ieee.std_logic_1164.all;

entity primer20k_ulpi_probe_top is
  port (
    clk27     : in    std_logic;
    ulpi_clk  : in    std_logic;
    ulpi_dir  : in    std_logic;
    ulpi_nxt  : in    std_logic;
    ulpi_stp  : out   std_logic;
    ulpi_data : inout std_logic_vector(7 downto 0);
    ulpi_rst  : out   std_logic;
    uart_rx   : in    std_logic;
    uart_tx   : out   std_logic;
    led       : out   std_logic_vector(5 downto 0)
  );
end entity;

architecture rtl of primer20k_ulpi_probe_top is
  signal usb_clk       : std_logic;
  signal usb_rst       : std_logic;
  signal phy_released  : std_logic;
  signal data_o        : std_logic_vector(7 downto 0);
  signal data_oe       : std_logic;
  signal phy_ready     : std_logic;
begin
  usb_clk <= ulpi_clk;

  phy_reset_i : entity work.primer20k_ulpi_reset
    port map (clk27 => clk27, resetb => ulpi_rst, released => phy_released);

  usb_reset_i : entity work.reset_sync
    port map (clk => usb_clk, arst_in => not phy_released, rst => usb_rst);

  ulpi_data <= data_o when data_oe = '1' else (others => 'Z');

  core_i : entity work.ulpi_probe_core
    generic map (CLK_HZ => 60_000_000, BAUD => 115_200)
    port map (clk => usb_clk, rst => usb_rst,
              dir => ulpi_dir, nxt => ulpi_nxt, data_i => ulpi_data,
              data_o => data_o, data_oe => data_oe, stp => ulpi_stp,
              uart_rx => uart_rx, uart_tx => uart_tx, phy_ready => phy_ready);

  led(0) <= not phy_ready;
  led(1) <= '0';
  led(5 downto 2) <= (others => '1');
end architecture;
