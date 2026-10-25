-- 観測の命令の待ち行列（obs_jtag_engine から切り出し）: 深さ DEPTH・幅 WIDTH の FIFO。
--   push: push_data を積む。満杯なら捨てて dropped を立てる。dropped は最後の push の結果（積めたら 0 に戻る）。
--   pop : 先頭を捨てる。nonempty のときだけ意味がある（使う側が front を読んだ周期に pop する）。
--   flush: 開き直す。積んでいた命令を全部捨てる（同じ周期の push は 1 つだけ残す）。dropped も 0 に戻す。
--          flush と pop は同じ周期に立てない（使う側が排他にする）。
--   front / nonempty は組み合わせ出力（レジスタの値だけで決まる）。
library ieee;
use ieee.std_logic_1164.all;

entity obs_cmd_queue is
  generic (
    DEPTH : positive := 4;
    WIDTH : positive := 16
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    push      : in  std_logic;
    push_data : in  std_logic_vector(WIDTH - 1 downto 0);
    pop       : in  std_logic;
    flush     : in  std_logic;
    front     : out std_logic_vector(WIDTH - 1 downto 0);
    nonempty  : out std_logic;
    dropped   : out std_logic
  );
end entity;

architecture rtl of obs_cmd_queue is
  type mem_t is array (0 to DEPTH - 1) of std_logic_vector(WIDTH - 1 downto 0);
  signal mem       : mem_t;
  signal head      : natural range 0 to DEPTH - 1;
  signal count     : natural range 0 to DEPTH;
  signal dropped_r : std_logic;
begin
  front    <= mem(head);
  nonempty <= '1' when count /= 0 else '0';
  dropped  <= dropped_r;

  process (clk)
    variable push_ok : boolean;
  begin
    if rising_edge(clk) then
      push_ok := push = '1' and count < DEPTH;
      if push_ok then
        mem((head + count) mod DEPTH) <= push_data;
        dropped_r <= '0';                                -- dropped は最後の push の結果（積めた命令で消える）
      elsif push = '1' then
        dropped_r <= '1';
      end if;

      if flush = '1' then
        head <= (head + count) mod DEPTH;
        if push_ok then
          count <= 1;
        else
          count <= 0;
        end if;
        dropped_r <= '0';
      else
        if pop = '1' and count /= 0 then
          head <= (head + 1) mod DEPTH;
        end if;
        if push_ok and not (pop = '1' and count /= 0) then
          count <= count + 1;
        elsif pop = '1' and count /= 0 and not push_ok then
          count <= count - 1;
        end if;
      end if;

      if rst = '1' then
        head <= 0;
        count <= 0;
        dropped_r <= '0';
      end if;
    end if;
  end process;
end architecture;
