-- 符号なしの割り算（復元型、1 周期に 1 ビット。WIDTH + 1 周期で完了）。
-- start で dividend / divisor を取り込み、done（1 周期）で quotient と remainder が有効になる。
-- divisor = 0 のときは quotient = 全 1、remainder = dividend（呼び出し側で避けること）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity udiv is
  generic (
    WIDTH : positive := 32
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    start     : in  std_logic;
    dividend  : in  unsigned(WIDTH - 1 downto 0);
    divisor   : in  unsigned(WIDTH - 1 downto 0);
    busy      : out std_logic;
    done      : out std_logic;
    quotient  : out unsigned(WIDTH - 1 downto 0);
    remainder : out unsigned(WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of udiv is
  signal num   : unsigned(WIDTH - 1 downto 0) := (others => '0');
  signal den   : unsigned(WIDTH - 1 downto 0) := (others => '0');
  signal quot  : unsigned(WIDTH - 1 downto 0) := (others => '0');
  signal rem_r : unsigned(WIDTH downto 0) := (others => '0');
  signal left  : natural range 0 to WIDTH := 0;
  signal fin   : std_logic := '0';
begin
  process (clk)
    variable trial : unsigned(WIDTH downto 0);
  begin
    if rising_edge(clk) then
      fin <= '0';
      if rst = '1' then
        left <= 0;
      elsif left = 0 then
        if start = '1' then
          num   <= dividend;
          den   <= divisor;
          quot  <= (others => '0');
          rem_r <= (others => '0');
          left  <= WIDTH;
        end if;
      else
        trial := rem_r(WIDTH - 1 downto 0) & num(WIDTH - 1);
        if trial >= ('0' & den) then
          rem_r <= trial - ('0' & den);
          quot  <= quot(WIDTH - 2 downto 0) & '1';
        else
          rem_r <= trial;
          quot  <= quot(WIDTH - 2 downto 0) & '0';
        end if;
        num  <= num(WIDTH - 2 downto 0) & '0';
        left <= left - 1;
        if left = 1 then
          fin <= '1';
        end if;
      end if;
    end if;
  end process;

  busy      <= '1' when left /= 0 else '0';
  done      <= fin;
  quotient  <= quot;
  remainder <= rem_r(WIDTH - 1 downto 0);
end architecture;
