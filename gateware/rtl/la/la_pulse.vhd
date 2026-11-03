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
-- 幅の比較（count = min_cycles・count = max_cycles）は、1 周期前の count が「設定 − 1」だったかを毎周期求めて旗に持つ
-- （min_f・max_f。広い比較を毎周期の経路・途中の条件の先に置かない。300 MHz 級の取り込み領域のため）。count が 1 になった最初の周期は
-- 旗が古い count のものなので、設定が 1 かの旗を使う。設定は止めている間に変える約束なので、設定 − 1 はレジスタに置いてよい。
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
  constant HALF : natural := COUNT_BITS / 2;
  signal min_m1, max_m1 : unsigned(COUNT_BITS - 1 downto 0) := (others => '0');
  signal min_lo, max_lo : unsigned(HALF downto 0) := (others => '0');       -- 下半分 − 1（最上位は借り）
  signal min_hi, max_hi : unsigned(COUNT_BITS - HALF - 1 downto 0) := (others => '0');
  signal min_zero, min_one, max_one : std_logic := '0';
  signal first_c   : std_logic := '0';     -- count に 1 を入れた周期（旗はまだ古い count のもの）
  signal min_f, max_f : std_logic := '0';  -- 1 周期前の count が設定 − 1 だった（毎周期求める）
  signal at_min, at_max : std_logic;       -- count = min_cycles・count = max_cycles（今の count について）
begin
  -- 設定 − 1 は 2 段で求める（桁上がりの鎖を半分にする）。設定は止めている間に変える約束
  process (clk)
  begin
    if rising_edge(clk) then
      min_lo <= resize(min_cycles(HALF - 1 downto 0), HALF + 1) - 1;
      min_hi <= min_cycles(COUNT_BITS - 1 downto HALF);
      max_lo <= resize(max_cycles(HALF - 1 downto 0), HALF + 1) - 1;
      max_hi <= max_cycles(COUNT_BITS - 1 downto HALF);
      min_m1 <= (min_hi - ("" & min_lo(HALF))) & min_lo(HALF - 1 downto 0);
      max_m1 <= (max_hi - ("" & max_lo(HALF))) & max_lo(HALF - 1 downto 0);
      min_zero <= '1' when min_cycles = 0 else '0';
      min_one  <= '1' when min_cycles = 1 else '0';
      max_one  <= '1' when max_cycles = 1 else '0';
      -- 毎周期求める（途中の条件に左右されない）。count が 1 になった最初の周期だけ、旗は古い count のものなので min_one・max_one を使う
      min_f <= '1' when count = min_m1 else '0';
      max_f <= '1' when count = max_m1 else '0';
    end if;
  end process;
  at_min <= min_one when first_c = '1' else min_f;
  at_max <= max_one when first_c = '1' else max_f;

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
      first_c <= '0';
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
          first_c <= '1';
        else
          count <= count + 1;
          if at_min = '1' then                 -- この周期で幅が min_cycles を超えた
            reached <= '1';
            if mode = MODE_TIMEOUT and reached = '0' then
              event_r <= '1';
            end if;
          end if;
          if at_max = '1' then
            over <= '1';
          end if;
        end if;
      elsif inside = '1' then                  -- 脈の終わり: 幅 = count
        inside <= '0';
        if mode = MODE_RANGE and edged = '1' and over = '0'
           and (reached = '1' or at_min = '1' or min_zero = '1') then
          event_r <= '1';
        end if;
      end if;
    end if;
  end process;

  event <= event_r;
end architecture;
