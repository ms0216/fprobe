-- 深いバッファの符号器の N 並列版（ボード非依存、clk = sys のドメイン）: 1 周期に LANES サンプルの語を、RAW（la_lane_raw: サンプルと
-- ch を選んで詰める）か変化点（la_lane_rle）で 128 ビットの拍にする。la_deep_buffer の LANES > 1 の構成が la_encoder の代わりに使う。
-- 仕様: docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §2〜§4
--
-- run の 0→1 で設定（rle・step・pack・rate）を取り込んで始め、その次の周期の語から符号にする。1→0 で止める（作りかけは捨てる）。
-- 起点の時刻: start_time = 最初に符号にした語のタイムベース（入力の lane_data に出ていた周期の time）。その語のレーン k は、細かい
--   サンプルの辺 LANES × start_time − (LANES − 1 − k) − IN_AGE で取ったサンプル（IN_AGE = ボードの取り込みの最も新しいレーンの遅れ。
--   la_lane_capture と同じ約束）。流れの細かいサンプルの番号 s（最初の語のレーン 0 = 0）の値に変わったピンは、辺
--     n = start_time × LANES + s − latency（latency = LANES + IN_AGE）
--   の後に変わり、辺 n + 1 で取られた（RAW で 2^step > 1 なら、s = サンプルの番号 × 2^step で、変化は辺 n − 2^step + 1〜n の後のどこか）。
-- 出力は 1 拍の保持レジスタ（out_valid / out_ready）。保持中に次の拍が出た（RAW）・RLE の溜めが溢れた周期に overrun を立て、
--   以後は新しい拍を作らない（start まで。出た拍は欠けの無い先頭の部分）。RLE の溢れる前の溜めは出し切る。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity la_lane_encoder is
  generic (
    WIDTH  : positive := 16;
    LANES  : positive := 8;
    IN_AGE : natural := 5
  );
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    run        : in  std_logic;
    rle        : in  std_logic;
    step       : in  unsigned(1 downto 0);
    pack       : in  unsigned(2 downto 0);
    first_ch   : in  unsigned(3 downto 0);                    -- RAW: 記録する最初の ch（la_lane_raw）
    rate       : in  unsigned(15 downto 0);
    lane_data      : in  std_logic_vector(LANES * WIDTH - 1 downto 0);
    time       : in  unsigned(63 downto 0);
    out_valid  : out std_logic;
    out_ready  : in  std_logic;
    out_data   : out std_logic_vector(127 downto 0);
    overrun    : out std_logic;
    start_time : out unsigned(63 downto 0);
    started    : out std_logic;                               -- start_time が今の流れのもの
    latency    : out unsigned(7 downto 0)
  );
end entity;

architecture rtl of la_lane_encoder is
  constant LATENCY_FINE : natural := LANES + IN_AGE;
  signal lanes_r : std_logic_vector(LANES * WIDTH - 1 downto 0) := (others => '0');
  signal time_r : unsigned(63 downto 0) := (others => '0');   -- lanes_r の語の時刻
  signal run_prev, start, rle_r : std_logic := '0';
  signal active, dead, first : boolean := false;
  signal in_valid : std_logic;
  signal raw_valid, rle_valid, rle_ready, rle_ovf : std_logic;
  signal raw_data, rle_data : std_logic_vector(127 downto 0);
  signal hold_valid : std_logic := '0';
  signal hold_data : std_logic_vector(127 downto 0) := (others => '0');
  signal ovr : std_logic := '0';
  signal t0 : unsigned(63 downto 0) := (others => '0');
  signal t0_ok : std_logic := '0';
begin
  assert LATENCY_FINE < 256 report "la_lane_encoder: latency does not fit in 8 bits" severity failure;
  latency <= to_unsigned(LATENCY_FINE, 8);

  start    <= run and not run_prev;
  in_valid <= '1' when active and not dead else '0';

  raw_i : entity work.la_lane_raw
    generic map (WIDTH => WIDTH, LANES => LANES)
    port map (clk => clk, rst => rst, start => start, in_valid => in_valid and not rle_r, lane_data => lanes_r,
              step => step, pack => pack, first_ch => first_ch, rate => rate, beat_valid => raw_valid, beat_data => raw_data);

  rle_i : entity work.la_lane_rle
    generic map (WIDTH => WIDTH, LANES => LANES)
    port map (clk => clk, rst => rst, start => start, in_valid => in_valid and rle_r, lane_data => lanes_r,
              beat_valid => rle_valid, beat_ready => rle_ready, beat_data => rle_data, overflow => rle_ovf);
  rle_ready <= not hold_valid or out_ready;

  process (clk)
  begin
    if rising_edge(clk) then
      lanes_r  <= lane_data;
      time_r   <= time;
      run_prev <= run;
      if hold_valid = '1' and out_ready = '1' then
        hold_valid <= '0';
      end if;
      if rle_r = '1' then
        if rle_valid = '1' and rle_ready = '1' then
          hold_valid <= '1';
          hold_data  <= rle_data;
        end if;
        if rle_ovf = '1' then
          ovr  <= '1';
          dead <= true;
        end if;
      elsif raw_valid = '1' then
        if hold_valid = '1' and out_ready = '0' then
          ovr  <= '1';                                         -- 前の拍がまだ出ていない: この拍を捨てて以後を止める
          dead <= true;
        elsif not dead then
          hold_valid <= '1';
          hold_data  <= raw_data;
        end if;
      end if;
      if active and first then
        t0    <= time_r;                                       -- 最初に符号にした語の時刻
        t0_ok <= '1';
        first <= false;
      end if;
      if start = '1' then
        rle_r  <= rle;
        active <= true;
        first  <= true;
        dead   <= false;
        ovr    <= '0';
        t0_ok  <= '0';
        hold_valid <= '0';
      end if;
      if run = '0' then
        active <= false;
        first  <= false;
      end if;
      if rst = '1' then
        hold_valid <= '0';
        active <= false;
        first  <= false;
        dead   <= false;
        ovr    <= '0';
        t0_ok  <= '0';
      end if;
    end if;
  end process;

  out_valid  <= hold_valid;
  out_data   <= hold_data;
  overrun    <= ovr;
  start_time <= t0;
  started    <= t0_ok;
end architecture;
