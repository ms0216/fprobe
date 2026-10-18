-- 単純 2 ポート RAM（書き込み 1 口・同期読み出し 1 口、1 クロック）。ベンダーのブロック RAM に推論される定型の書き方。
-- rdata は raddr を与えた次の周期に出る。同じ番地への同時の読み書きでは古い値が読める（read-first）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity sdp_ram is
  generic (
    WIDTH : positive := 8;
    DEPTH : positive := 1024                -- 2 の冪
  );
  port (
    clk   : in  std_logic;
    we    : in  std_logic;
    waddr : in  unsigned(clog2(DEPTH) - 1 downto 0);
    wdata : in  std_logic_vector(WIDTH - 1 downto 0);
    raddr : in  unsigned(clog2(DEPTH) - 1 downto 0);
    rdata : out std_logic_vector(WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of sdp_ram is
  type memory_t is array (0 to DEPTH - 1) of std_logic_vector(WIDTH - 1 downto 0);
  signal memory : memory_t;
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if we = '1' then
        memory(to_integer(waddr)) <= wdata;
      end if;
      rdata <= memory(to_integer(raddr));
    end if;
  end process;
end architecture;
