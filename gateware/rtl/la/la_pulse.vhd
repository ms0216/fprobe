-- ロジアナのパルス幅のトリガ（ボード非依存、clk のドメイン。U21）。
--
-- probes は同期済み（la_filter の出力）。ch channel の level の脈（level に変わってから戻るまで）の幅を周期で数え:
--   mode 1（幅）: 脈が終わったとき、幅が min_cycles 以上 max_cycles 以下なら event を 1 周期出す。
--                 「より長い」は max_cycles を全ビット 1、「より短い」は min_cycles を 0 にする。
--                 始まりを見ていない脈（有効にした時点で既に level だった）は幅が分からないので数えない。
--   mode 2（一定時間変化しない）: level が min_cycles 周期を超えて続いたら、その時点で（脈の途中で）event を
--                 1 周期出す（脈ごとに 1 回）。有効にした時点で既に level なら、そこから数える。
--   mode 0・3: 何もしない（状態を消す）。
-- 幅の数えは COUNT_BITS ビットで折り返す（32 ビットなら 100 MHz で約 43 秒より長い脈は正しく数えない）。
-- event は脈の終わり（mode 1）・超えた時点（mode 2）から 2 周期後。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity la_pulse is
  generic (
    WIDTH      : positive := 16;
    COUNT_BITS : positive := 32
  );
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    probes     : in  std_logic_vector(WIDTH - 1 downto 0);
    mode       : in  std_logic_vector(1 downto 0);
    channel    : in  unsigned(3 downto 0);                       -- WIDTH 以上は ch 0
    level      : in  std_logic;
    min_cycles : in  unsigned(COUNT_BITS - 1 downto 0);
    max_cycles : in  unsigned(COUNT_BITS - 1 downto 0);
    event      : out std_logic
  );
end entity;

architecture rtl of la_pulse is
  constant MODE_RANGE   : std_logic_vector(1 downto 0) := "01";
  constant MODE_TIMEOUT : std_logic_vector(1 downto 0) := "10";
  signal x, prev   : std_logic := '0';
  signal inside    : std_logic := '0';     -- 脈を数えている（level が続いている）
  signal edged     : std_logic := '0';     -- その脈の始まり（変化）を見た
  signal count     : unsigned(COUNT_BITS - 1 downto 0) := (others => '0');   -- 脈の今までの周期数
  signal reached   : std_logic := '0';     -- 幅が min_cycles を超えた
  signal over      : std_logic := '0';     -- 幅が max_cycles を超えた
  signal event_r   : std_logic := '0';
begin
  process (clk)
    variable ch : natural range 0 to WIDTH - 1;
  begin
    if rising_edge(clk) then
      ch := 0;
      if to_integer(channel) < WIDTH then
        ch := to_integer(channel);
      end if;
      x       <= probes(ch);
      prev    <= x;
      event_r <= '0';
      if rst = '1' or (mode /= MODE_RANGE and mode /= MODE_TIMEOUT) then
        inside  <= '0';
        edged   <= '0';
        count   <= (others => '0');
        reached <= '0';
        over    <= '0';
      elsif x = level then
        if inside = '0' then                   -- 脈の始まり（または有効にした時点で既に level）
          inside  <= '1';
          edged   <= prev xor level;
          count   <= to_unsigned(1, COUNT_BITS);
          reached <= '0';
          over    <= '0';
        else
          count <= count + 1;
          if count = min_cycles then           -- この周期で幅が min_cycles を超えた
            reached <= '1';
            if mode = MODE_TIMEOUT and reached = '0' then
              event_r <= '1';
            end if;
          end if;
          if count = max_cycles then
            over <= '1';
          end if;
        end if;
      elsif inside = '1' then                  -- 脈の終わり: 幅 = count
        inside <= '0';
        if mode = MODE_RANGE and edged = '1' and over = '0'
           and (reached = '1' or count = min_cycles or min_cycles = 0) then
          event_r <= '1';
        end if;
      end if;
    end if;
  end process;

  event <= event_r;
end architecture;
