-- 共通タイムベース: clk の周期で数える 64 ビットのフリーランカウンタ（リセットで 0）。
-- 周波数はケイパビリティ TIMEBASE_HZ で申告する。64 ビットは 100 MHz で約 5800 年あふれない。
-- 32 ビットずつ読む利用者（CSR）のため、hold = '1' の周期の上位 32 ビットを held_hi に保持する。
-- 下位をレジスタに取り込むのと同じエッジで hold を出せば、2 回の読み出しで一貫した 64 ビット値になる。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity timebase is
  port (
    clk  : in  std_logic;
    rst  : in  std_logic;
    count   : out unsigned(63 downto 0);
    hold    : in  std_logic;
    held_hi : out std_logic_vector(31 downto 0)
  );
end entity;

architecture rtl of timebase is
  signal value : unsigned(63 downto 0) := (others => '0');
  signal high  : std_logic_vector(31 downto 0) := (others => '0');
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        value <= (others => '0');
      else
        value <= value + 1;
      end if;
      if hold = '1' then
        high <= std_logic_vector(value(63 downto 32));
      end if;
    end if;
  end process;

  count   <= value;
  held_hi <= high;
end architecture;
