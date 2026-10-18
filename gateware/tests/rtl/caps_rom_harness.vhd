-- テスト専用: caps_rom に試験用の生成パッケージ（test_caps_pkg、テストが一時ディレクトリに生成）をつなぐ。
library ieee;
use ieee.std_logic_1164.all;
use work.csr_bus_pkg.all;

entity caps_rom_harness is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    bus_req   : in  std_logic;
    bus_we    : in  std_logic;
    bus_addr  : in  csr_addr_t;
    bus_wdata : in  csr_word_t;
    bus_ack   : out std_logic;
    bus_err   : out std_logic;
    bus_rdata : out csr_word_t
  );
end entity;

architecture rtl of caps_rom_harness is
begin
  rom_i : entity work.caps_rom
    generic map (CONTENT => work.test_caps_pkg.CAPS_ROM, WINDOW_SIZE => 1024)
    port map (clk => clk, rst => rst, bus_req => bus_req, bus_addr => bus_addr,
              bus_ack => bus_ack, bus_rdata => bus_rdata);
  bus_err <= '0';
end architecture;
