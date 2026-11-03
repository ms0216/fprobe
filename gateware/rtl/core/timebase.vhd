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
  -- 下位 32 ビットと上位 32 ビットに分けて数える（64 ビットの加算の 1 段は、sys を上げると遅い経路だった。2026-10-29）。
  -- 下位が 0xFFFFFFFF → 0 になる周期に上位を 1 増やす。その判定（下位 = 0xFFFFFFFF）を 1 周期前に登録しておく
  -- （wrap_next = 前の周期の下位が 0xFFFFFFFE）ので、上位の繰り上がりは遅れず、count は毎周期ちょうど 1 ずつ増える。
  signal low, upper : unsigned(31 downto 0) := (others => '0');
  signal wrap_next  : boolean := false;
  signal high  : std_logic_vector(31 downto 0) := (others => '0');
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        low       <= (others => '0');
        upper     <= (others => '0');
        wrap_next <= false;
      else
        low       <= low + 1;
        wrap_next <= low = x"FFFFFFFE";
        if wrap_next then
          upper <= upper + 1;
        else
          upper <= upper;                                -- 毎周期代入する（試験で値を強制して解除したとき、その値が残る）
        end if;
      end if;
      if hold = '1' then
        high <= std_logic_vector(upper);
      end if;
    end if;
  end process;

  count   <= upper & low;
  held_hi <= high;
end architecture;
