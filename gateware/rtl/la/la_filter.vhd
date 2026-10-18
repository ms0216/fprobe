-- ロジアナのグリッチフィルタ（ボード非依存、clk のドメイン。U21）。
--
-- d は非同期でよい（2 段で同期化する）。mask の ch は、同期化した値の変化が cycles 周期続いたときだけ q に通す
-- （cycles 周期より短い脈・ひげを捨てる）。それ以外の ch と cycles <= 1 は同期化した値をそのまま 1 周期遅れで通す。
-- フィルタした ch の変化は、通す ch より cycles - 1 周期遅れて q に出る（脈の幅は保たれる。ホストが申告に使う）。
-- 設定は使う前に決めること（動作中に変えると、その時の数えかけの変化が新旧どちらで判定されるかは決めない）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity la_filter is
  generic (
    WIDTH    : positive := 8;
    LEN_BITS : positive := 8
  );
  port (
    clk    : in  std_logic;
    rst    : in  std_logic;
    d      : in  std_logic_vector(WIDTH - 1 downto 0);
    mask   : in  std_logic_vector(WIDTH - 1 downto 0);   -- フィルタする ch
    cycles : in  unsigned(LEN_BITS - 1 downto 0);         -- 通す最短の脈（周期）
    q      : out std_logic_vector(WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of la_filter is
  type count_array_t is array (0 to WIDTH - 1) of unsigned(LEN_BITS - 1 downto 0);
  signal s1, s : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal q_r   : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal left  : count_array_t := (others => (others => '0'));   -- 通すまでに残る周期 - 1
  signal span  : unsigned(LEN_BITS - 1 downto 0) := (others => '0');   -- cycles - 1（cycles <= 1 なら 0）

  -- 同期化の FF: 最適化で消さず、シフトレジスタとして記憶素子にまとめない（sync_bit と同じ）
  attribute syn_preserve : integer;
  attribute syn_srlstyle : string;
  attribute syn_preserve of s1, s : signal is 1;
  attribute syn_srlstyle of s1, s : signal is "registers";
begin
  process (clk)
  begin
    if rising_edge(clk) then
      s1 <= d;
      s  <= s1;
      if cycles > 1 then
        span <= cycles - 1;
      else
        span <= (others => '0');
      end if;
      for i in 0 to WIDTH - 1 loop
        if rst = '1' then
          q_r(i)  <= s(i);
          left(i) <= (others => '0');
        elsif s(i) = q_r(i) then
          if mask(i) = '1' then                 -- 変わっていない: 次の変化に備えて数え直す
            left(i) <= span;
          else
            left(i) <= (others => '0');
          end if;
        elsif left(i) = 0 then                  -- 変化が続いた（フィルタしない ch は最初の周期）
          q_r(i) <= s(i);
          if mask(i) = '1' then
            left(i) <= span;
          end if;
        else
          left(i) <= left(i) - 1;
        end if;
      end loop;
    end if;
  end process;

  q <= q_r;
end architecture;
