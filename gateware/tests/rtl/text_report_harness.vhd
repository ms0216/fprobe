-- テスト専用: 固定テンプレートの text_report
library ieee;
use ieee.std_logic_1164.all;
use work.text_report_pkg.all;

entity text_report_harness is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    start     : in  std_logic;
    field0    : in  std_logic_vector(7 downto 0);
    field1    : in  std_logic_vector(7 downto 0);
    busy      : out std_logic;
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of text_report_harness is
  constant MSG : template_t := lit("a=") & hex(0) & lit(" b=") & hex(1) & lit(CR & LF);
  signal fields : fields_t := (others => (others => '0'));
begin
  fields(0) <= field0;
  fields(1) <= field1;
  report_i : entity work.text_report
    generic map (TEMPLATE => MSG)
    port map (clk => clk, rst => rst, start => start, fields => fields, busy => busy,
              out_valid => out_valid, out_ready => out_ready, out_data => out_data);
end architecture;
