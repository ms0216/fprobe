-- テスト専用: uart_tx の出力を uart_rx に直結する
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity uart_loopback is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    divisor   : in  unsigned(15 downto 0);
    in_valid  : in  std_logic;
    in_ready  : out std_logic;
    in_data   : in  std_logic_vector(7 downto 0);
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of uart_loopback is
  signal line : std_logic;
begin
  tx_i : entity work.uart_tx
    port map (clk => clk, rst => rst, divisor => divisor,
              in_valid => in_valid, in_ready => in_ready, in_data => in_data,
              tx => line, busy => open);
  rx_i : entity work.uart_rx
    port map (clk => clk, rst => rst, divisor => divisor, rx => line,
              out_valid => out_valid, out_ready => out_ready, out_data => out_data,
              frame_error => open, overrun => open);
end architecture;
