-- 内蔵ロジアナの N 並列版（ボード非依存、clk = sys のドメイン）: 1 周期に LANES サンプルを受け、トリガを判定し、
-- 1 語 = LANES × WIDTH ビットで BRAM に記録する（la_lane_front + la_record）。
-- 仕様: docs/superpowers/specs/2026-11-01-la-resolution-design.md §3.3・§3.5（段 1）
--
-- 数える単位は語（sys の 1 周期）: pretrigger・start_index・trigger_index・DEPTH は語。trigger_lane はトリガの語の中のレーン。
-- 時刻: 入力の語（clk の立ち上がり w の後に lane_data に出た語）のレーン k が、細かいサンプルの辺 LANES × w − (LANES − 1 − k) − IN_AGE
--   で取ったサンプルであるとき（IN_AGE = ボードの取り込みの、最も新しいレーンの遅れ。PYNQ の ISERDES は pynq_z2_pkg.iserdes_newest_age）、
--   ピンが変わった細かいサンプルの番号 n（辺 n の後に変わり、辺 n + 1 で取られた）は
--     n = trigger_time × LANES + trigger_lane − latency
--   （trigger_time = トリガの語を記録した周期のタイムベース）。latency は定数で、出力 latency に出す
--   （= LANES × (PIPE_CYCLES + 1) + IN_AGE。PIPE_CYCLES = la_lane_front の 3 段 = timing_pkg の LA_LANE_PIPE_CYCLES。tests/tb/la_lane_e2e_tb.py が
--   ISERDES の模型を通して測り、一致を確かめる）。
-- 外部トリガ: ext_trigger が 1 になった sys の周期 X の頭（細かい辺 N × X）の直後のサンプル（辺 N × X + 1）にトリガを付ける（n = N × X。
--   時刻の正しさ S1b の N3）。そのサンプルは入力の語 X + 1 + IN_AGE / N のレーン IN_AGE mod N で、その語が la_lane_front の判定の段に
--   ある周期まで ext_trigger を遅らせる（la_lane_front の EXT_DELAY・EXT_LANE）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.timing_pkg.all;

entity la_lane_capture is
  generic (
    WIDTH  : positive := 16;
    LANES  : positive := 6;
    DEPTH  : positive := 16384;                -- 語。2 の冪
    IN_AGE : natural := 3
  );
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    lane_data         : in  std_logic_vector(LANES * WIDTH - 1 downto 0);
    time          : in  unsigned(63 downto 0);
    ext_trigger   : in  std_logic;
    run           : in  std_logic;
    pretrigger    : in  unsigned(31 downto 0);
    trig_mask     : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_value    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_rise     : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_fall     : in  std_logic_vector(WIDTH - 1 downto 0);
    capturing     : out std_logic;
    triggered     : out std_logic;
    done          : out std_logic;
    start_index   : out unsigned(31 downto 0);
    trigger_index : out unsigned(31 downto 0);
    trigger_time  : out unsigned(63 downto 0);
    trigger_lane  : out unsigned(3 downto 0);
    latency       : out unsigned(7 downto 0);
    rd_addr       : in  unsigned(clog2(DEPTH) - 1 downto 0);
    rd_data       : out std_logic_vector(LANES * WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of la_lane_capture is
  constant PIPE_CYCLES : natural := LA_LANE_PIPE_CYCLES;   -- la_lane_front: 入力の語が sample に出るまで
  constant LATENCY_FINE : natural := LANES * (PIPE_CYCLES + 1) + IN_AGE;
  -- 外部トリガ（上の注）: 入力の語 w は la_lane_front の w1（判定の段）に周期 w + 1 にある。ext_trigger が 1 の周期 X → 語 X + 1 + IN_AGE / LANES
  constant EXT_DELAY : positive := 2 + IN_AGE / LANES;
  constant EXT_LANE  : natural := IN_AGE mod LANES;
  signal arm, valid, hit : std_logic;
  signal data : std_logic_vector(LANES * WIDTH - 1 downto 0);
  signal lane : unsigned(3 downto 0);
begin
  assert LATENCY_FINE < 256 report "la_lane_capture: latency does not fit in 8 bits" severity failure;
  latency <= to_unsigned(LATENCY_FINE, 8);

  front_i : entity work.la_lane_front
    generic map (WIDTH => WIDTH, LANES => LANES, EXT_DELAY => EXT_DELAY, EXT_LANE => EXT_LANE)
    port map (clk => clk, rst => rst, lane_data => lane_data, ext_trigger => ext_trigger, run => run,
              trig_mask => trig_mask, trig_value => trig_value, trig_rise => trig_rise, trig_fall => trig_fall,
              sample_valid => valid, sample => data, sample_hit => hit, sample_lane => lane);

  record_i : entity work.la_record
    generic map (WIDTH => LANES * WIDTH, DEPTH => DEPTH, RD_REG => false, FAST => false)
    port map (clk => clk, rst => rst, run => run, pretrigger => pretrigger, valid => valid, data => data, hit => hit,
              time => time, phase => lane, arm => arm, capturing => capturing, triggered => triggered, done => done,
              start_index => start_index, trigger_index => trigger_index, trigger_time => trigger_time,
              trigger_phase => trigger_lane, rd_addr => rd_addr, rd_data => rd_data);
end architecture;
