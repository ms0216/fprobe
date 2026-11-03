-- テスト専用: la_testgen の clk と cap_clk に同じクロックを渡す（fprobe_pynq_deep の形。取り込みが sys と同じ）。
-- 同一ネットなので、シミュレーションでも delta の競合が出ない。
library ieee;
use ieee.std_logic_1164.all;

entity la_testgen_same_clk_harness is
  port (
    clk      : in  std_logic;
    rst      : in  std_logic;
    enable   : in  std_logic;
    edge_sel : in  std_logic;
    period   : in  std_logic_vector(15 downto 0);
    high     : in  std_logic_vector(15 downto 0);
    out_q    : out std_logic;
    out_oe   : out std_logic
  );
end entity;

architecture rtl of la_testgen_same_clk_harness is
begin
  dut_i : entity work.la_testgen
    port map (clk => clk, rst => rst, cap_clk => clk, enable => enable, edge_sel => edge_sel, period => period, high => high,
              out_q => out_q, out_oe => out_oe);
end architecture;
