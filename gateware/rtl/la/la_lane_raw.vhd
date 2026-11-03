-- ロジアナの RAW の N 並列の詰め（ボード非依存、clk = sys のドメイン）: 1 周期に LANES サンプル（レーン）の語を受け、
-- 選んだサンプル・選んだ ch のビットだけを時間の順に並べて 128 ビットの拍にする（深いバッファ・垂れ流し用）。
-- 仕様: docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §3
--
--   サンプルの選び: 語ごとに、レーン 0・2^step・2 × 2^step … の M = LANES / 2^step 個（細かいサンプルの 2^step 個ごとに 1 つ）。
--     語は rate + 1 語ごとに 1 つ使う（rate > 0 は M = 1 のときに使う想定。そうでなくても回路は同じ式で動く）。
--   ch の選び: 1 サンプル c = WIDTH / 2^pack ビット（ch first〜first + c − 1。WIDTH を超える ch は 0。ほかの ch は記録しない）。
--   拍: 1 語の M × c ビット（2 の累乗）を、時間の古いものが下位になるように 128 ビットに詰める（128 / (M × c) 語で 1 拍）。
--     c = 16 なら 1 拍 = 8 サンプル（la_encoder の RAW と同じ形）、c < 16 なら la_pack の詰めた語を 8 つ並べたものと同じ形
--     （ホストの解読 fprobe.la は同じ）。
-- start（1 周期）で作りかけの拍と語の数えを捨てて始め直す。in_valid の周期の lane_data を 1 語として受ける。
-- 拍が揃った周期の次に beat_valid を 1 周期出す（止められない。受け手が受けられなければ、受け手が溢れを記録する）。
-- パイプライン: 入力 → ch の寄せ（sh。各レーンを first だけ右へ）→ 選び（nb・lg）→ 詰め（acc）の 3 段。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity la_lane_raw is
  generic (
    WIDTH : positive := 16;                 -- 2 の累乗
    LANES : positive := 8                   -- 2 の累乗。LANES × WIDTH <= 128
  );
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    start      : in  std_logic;
    in_valid   : in  std_logic;
    lane_data      : in  std_logic_vector(LANES * WIDTH - 1 downto 0);
    step       : in  unsigned(1 downto 0);   -- 2^step <= LANES（start の周期の値を使う）
    pack       : in  unsigned(2 downto 0);   -- 2^pack <= WIDTH（同）
    first_ch   : in  unsigned(3 downto 0);   -- 記録する最初の ch（同）
    rate       : in  unsigned(15 downto 0);  -- 語の間隔 − 1（同）
    beat_valid : out std_logic;
    beat_data  : out std_logic_vector(127 downto 0)
  );
end entity;

architecture rtl of la_lane_raw is
  constant LOG_LANES : natural := clog2(LANES);
  constant LOG_WIDTH : natural := clog2(WIDTH);
  constant LOG_BITS  : natural := LOG_LANES + LOG_WIDTH;     -- 1 語の最大のビット数の log2（<= 7）
  signal step_r : unsigned(1 downto 0) := (others => '0');
  signal pack_r : unsigned(2 downto 0) := (others => '0');
  signal rate_r : unsigned(15 downto 0) := (others => '0');
  signal first_r : natural range 0 to 15 := 0;
  signal sh : std_logic_vector(LANES * WIDTH - 1 downto 0) := (others => '0');   -- 各レーンを first だけ右へ寄せた語
  signal v0 : std_logic := '0';
  signal cnt : unsigned(15 downto 0) := (others => '0');      -- 次に使う語までの残り
  signal v1 : std_logic := '0';
  signal nb : std_logic_vector(127 downto 0) := (others => '0');   -- 選んだビット（下位 2^lg ビット）
  signal lg_r : natural range 0 to 7 := 0;
  signal nb_next : std_logic_vector(127 downto 0);
  signal lg_next : natural range 0 to 7;
  signal acc : std_logic_vector(127 downto 0) := (others => '0');
  signal words : unsigned(7 downto 0) := (others => '0');          -- 拍に入った語の数
  signal bv : std_logic := '0';
  signal bd : std_logic_vector(127 downto 0) := (others => '0');
begin
  assert 2 ** LOG_LANES = LANES and 2 ** LOG_WIDTH = WIDTH and LOG_BITS <= 7
    report "la_lane_raw: LANES and WIDTH must be powers of two with LANES * WIDTH <= 128" severity failure;

  -- 選び: 設定（step・pack）の組ごとに、定数の添字で並べる
  process (all)
    variable b : std_logic_vector(127 downto 0);
    variable l : integer;
  begin
    b := (others => '0');
    l := LOG_BITS - to_integer(step_r) - to_integer(pack_r);
    for s in 0 to 3 loop
      for p in 0 to 4 loop
        if s <= LOG_LANES and p <= LOG_WIDTH and to_integer(step_r) = s and to_integer(pack_r) = p then
          for j in 0 to (LANES / 2 ** s) - 1 loop
            for q in 0 to (WIDTH / 2 ** p) - 1 loop
              b(j * (WIDTH / 2 ** p) + q) := sh(j * 2 ** s * WIDTH + q);
            end loop;
          end loop;
        end if;
      end loop;
    end loop;
    nb_next <= b;
    if l < 0 then
      lg_next <= 0;
    else
      lg_next <= l;
    end if;
  end process;

  process (clk)
    variable a : std_logic_vector(127 downto 0);
  begin
    if rising_edge(clk) then
      bv <= '0';
      -- 段 0: 語の選び（rate + 1 語ごと）と ch の寄せ
      v0 <= '0';
      if in_valid = '1' then
        if cnt = 0 then
          v0  <= '1';
          cnt <= rate_r;
        else
          cnt <= cnt - 1;
        end if;
      end if;
      for k in 0 to LANES - 1 loop
        sh(k * WIDTH + WIDTH - 1 downto k * WIDTH) <=
          std_logic_vector(shift_right(unsigned(lane_data(k * WIDTH + WIDTH - 1 downto k * WIDTH)), first_r));
      end loop;
      -- 段 1: サンプルと ch の選び
      v1   <= v0;
      nb   <= nb_next;
      lg_r <= lg_next;
      -- 段 2: 古いものが下位になるよう、新しいビットを上に入れて右へ送る
      if v1 = '1' then
        a := acc;
        for l in 0 to 7 loop
          if lg_r = l then
            if l = 7 then
              a := nb;
            else
              a := nb(2 ** l - 1 downto 0) & acc(127 downto 2 ** l);
            end if;
          end if;
        end loop;
        acc <= a;
        if words = shift_left(to_unsigned(1, 8), 7 - lg_r) - 1 then
          bv    <= '1';
          bd    <= a;
          words <= (others => '0');
        else
          words <= words + 1;
        end if;
      end if;
      if rst = '1' or start = '1' then
        step_r <= step;
        pack_r <= pack;
        rate_r <= rate;
        first_r <= to_integer(first_ch);
        cnt    <= (others => '0');
        v0     <= '0';
        v1     <= '0';
        words  <= (others => '0');
        acc    <= (others => '0');
        bv     <= '0';
      end if;
    end if;
  end process;

  beat_valid <= bv;
  beat_data  <= bd;
end architecture;
