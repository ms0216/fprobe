-- 1 周期に複数のサンプルのロジアナの入力を、レーンの並び（lane_data）に組み立てる（ボード非依存、clk = sys のドメイン）。
-- 仕様: docs/superpowers/specs/2026-11-01-la-resolution-design.md §3.3、2026-11-01-time-accuracy-design.md §9.5 の N2（段階 S1b）
--
-- lane_data のレーン k（その周期の k 番目のサンプル、k = 0 が最も古い）の ch c = lane_data(k × WIDTH + c)。ch の並びは la_probe_inputs と
-- 同じ（0..LA_PIN_COUNT − 1 = ピン、続いて内部の 2 本、続いて専用の入力、残りは 0）。
-- 時間軸: ボードの直並列変換（xilinx_la_iserdes）が出す細かい ch（専用の入力と、FINE_PINS ならピン）は、立ち上がり w の後に出た語の
--   レーン k が細かい辺 N × w − (N − 1 − k) − IN_AGE のサンプル（N = LANES。sys の辺 T は細かい辺 N × T）。la_lane_capture・
--   la_lane_encoder の時刻の式はこの並びを前提にする。
-- sys の信号（細かくない ch）は、レーンごとに、そのレーンの辺の前の sys の周期の値を入れて同じ時間軸に載せる:
--   - 内部の 2 本（clk に同期した FF の出力。辺 T で変わる = 細かい辺 N × T の直後に変わった）: レーン k に、周期
--     floor((N × w − N + k − IN_AGE) / N) の値（w から D_INT(k) 周期前）を入れる。辺 N × T の直後に変わった細かい ch と同じレーンに来る
--     （LA_CH_TIMING のずれ 0・位相の幅 0。揃える前は全レーンに今の値を並べ、N + IN_AGE 細かいサンプル早く見えた）。
--   - FINE_PINS = false のピン（非同期。sys で 2 段の同期化）: 同じ選び方で 1 周期新しい値（D_INT(k) − 1 周期前）。sys の周期 [T, T + 1) の
--     中で変わったピンは、時間軸の T + 1 の辺の直後に来る（ずれ −1 刻み・位相の幅 1 刻み）。これより早くは並べられない（未来の値になる）。
-- tests/tb/la_lane_inputs_tb.py が細かい辺の番号で確かめる。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity la_lane_inputs is
  generic (
    WIDTH          : positive := 16;
    LANES          : positive := 8;
    LA_PIN_COUNT   : natural := 8;
    LA_INPUT_COUNT : natural := 0;
    FINE_PINS      : boolean := false;     -- ピンもボードの直並列変換（pin_lanes）から取る
    IN_AGE         : natural := 5          -- 直並列変換の最も新しいレーンの遅れ（細かいサンプル）
  );
  port (
    clk         : in  std_logic;
    pins        : in  std_logic_vector(LA_PIN_COUNT - 1 downto 0);                        -- 非同期（FINE_PINS = false で使う）
    pin_lanes   : in  std_logic_vector(LANES * LA_PIN_COUNT - 1 downto 0);                -- FINE_PINS = true で使う
    internal    : in  std_logic_vector(1 downto 0);                                       -- clk に同期した FF の出力
    input_lanes : in  std_logic_vector(maximum(LANES * LA_INPUT_COUNT, 1) - 1 downto 0);
    lane_data   : out std_logic_vector(LANES * WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of la_lane_inputs is
  -- レーン k の内部の ch の遅れ（周期）: 1 − floor((k − IN_AGE) / N)
  function d_int(k : natural) return natural is
  begin
    if k >= IN_AGE then
      return 1;
    end if;
    return 1 + (IN_AGE - k + LANES - 1) / LANES;
  end function;
  constant DEPTH : positive := d_int(0);                     -- 最も古いレーンが最も遅い

  type int_line_t is array (1 to DEPTH) of std_logic_vector(1 downto 0);
  type pin_line_t is array (0 to DEPTH) of std_logic_vector(LA_PIN_COUNT - 1 downto 0);
  signal int_line : int_line_t := (others => (others => '0'));     -- int_line(d) = internal の d 周期前（d >= 1）
  signal pin_line : pin_line_t := (others => (others => '0'));     -- pin_line(d) = 同期化したピンの d 周期前
  signal pins_s1 : std_logic_vector(LA_PIN_COUNT - 1 downto 0) := (others => '0');
begin
  assert LA_PIN_COUNT + 2 + LA_INPUT_COUNT <= WIDTH
    report "la_lane_inputs: LA_PIN_COUNT + 2 + LA_INPUT_COUNT exceeds WIDTH" severity failure;

  process (clk)
  begin
    if rising_edge(clk) then
      pins_s1 <= pins;                                       -- 非同期のピンは 2 段で同期化（pin_line(0) = 2 段目）
      pin_line(0) <= pins_s1;
      int_line(1) <= internal;
      for d in 1 to DEPTH loop
        pin_line(d) <= pin_line(d - 1);
      end loop;
      for d in 2 to DEPTH loop
        int_line(d) <= int_line(d - 1);
      end loop;
    end if;
  end process;

  process (all)
  begin
    lane_data <= (others => '0');
    for k in 0 to LANES - 1 loop
      if FINE_PINS then
        lane_data(k * WIDTH + LA_PIN_COUNT - 1 downto k * WIDTH) <= pin_lanes((k + 1) * LA_PIN_COUNT - 1 downto k * LA_PIN_COUNT);
      else
        lane_data(k * WIDTH + LA_PIN_COUNT - 1 downto k * WIDTH) <= pin_line(d_int(k) - 1);
      end if;
      lane_data(k * WIDTH + LA_PIN_COUNT + 1 downto k * WIDTH + LA_PIN_COUNT) <= int_line(d_int(k));
      if LA_INPUT_COUNT > 0 then
        lane_data(k * WIDTH + LA_PIN_COUNT + 2 + LA_INPUT_COUNT - 1 downto k * WIDTH + LA_PIN_COUNT + 2) <=
          input_lanes((k + 1) * LA_INPUT_COUNT - 1 downto k * LA_INPUT_COUNT);
      end if;
    end loop;
  end process;
end architecture;
