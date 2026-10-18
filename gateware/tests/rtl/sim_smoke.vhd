-- シミュレーション基盤の疎通確認用カウンタ
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity sim_smoke is
  generic (STEP : positive := 1);
  port (
    clk : in  std_logic;
    rst : in  std_logic;
    q   : out unsigned(7 downto 0)
  );
end entity;

architecture rtl of sim_smoke is
  signal count : unsigned(7 downto 0) := (others => '0');
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        count <= (others => '0');
      else
        count <= count + STEP;
      end if;
    end if;
  end process;
  q <= count;
end architecture;
