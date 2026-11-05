-- ロジアナの前段の N 並列版（ボード非依存、clk = sys のドメイン）: 1 周期に LANES 個のサンプル（レーン）を受け、トリガを判定する。
-- 仕様: docs/superpowers/specs/2026-11-01-la-resolution-design.md §3.3（段 1）
--
-- lane_data: レーン k（その周期の k 番目のサンプル、k = 0 が最も古い）の ch c = lane_data(k × WIDTH + c)。clk に同期した値
-- （ISERDES の出力・sys の信号。非同期の入力は前で同期化しておく）。
-- run = '1' の間、毎周期 1 語（LANES サンプル）を sample_valid とともに出す（間引きは無い。la の rate はこの構成では使わない）。
-- トリガ: レーンごとに 値一致 ((s and mask) = (value and mask)) かつ エッジ条件（rise / fall で指定した ch のどれかが、
--   1 つ前のサンプルからその向きに変化。レーン 0 の 1 つ前は前の周期のレーン LANES − 1。指定なしなら真）。
--   成立したレーンのうち最も古いものを sample_lane に出す。捕捉の最初の語のレーン 0 はエッジを見ない（捕捉の前のサンプルとの
--   変化で成立させない。la_front の「前の捕捉の値を持ち越さない」と同じ）。
--   外部トリガ（ext_trigger、sys の 1 周期のパルスでよい）は、EXT_DELAY 周期遅らせて、判定の段にある語のレーン EXT_LANE の成立に加える
--   （la_lane_capture が IN_AGE から決める: 外部トリガが 1 になった sys の周期 X の頭 = 細かい辺 N × X の直後のサンプルに付ける。
--   時刻の正しさ S1b の N3。それまでは報告した語のレーン 0 で、外部トリガより約 3 sys 周期 + IN_AGE 前のサンプルになっていた）。
--   トリガは記録の側（la_record）がトリガ待ちの間だけ受けるので、プリトリガを溜める間に来たものは使われない。
-- パイプライン: 入力 → w1（取り込み）→ w2・判定（hv_r）→ 出力（sample・sample_hit・sample_lane が揃う）の 3 段。
--   入力の語が出力に出るのは 3 周期後（PIPE_CYCLES）。
-- グリッチフィルタ・パルス幅のトリガは無い（ケイパビリティ la_filter・la_pulse を偽にする構成で使う）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.la_pkg.all;

entity la_lane_front is
  generic (
    WIDTH : positive := 16;
    LANES : positive := 6;
    EXT_DELAY : positive := 2;               -- 外部トリガを判定の段に加えるまでの周期（上の注）
    EXT_LANE  : natural := 0                 -- 外部トリガを付けるレーン
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    lane_data        : in  std_logic_vector(LANES * WIDTH - 1 downto 0);
    ext_trigger  : in  std_logic;
    run          : in  std_logic;
    trig_mask    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_value   : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_rise    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_fall    : in  std_logic_vector(WIDTH - 1 downto 0);
    sample_valid : out std_logic;
    sample       : out std_logic_vector(LANES * WIDTH - 1 downto 0);
    sample_hit   : out std_logic;
    sample_lane  : out unsigned(3 downto 0)
  );
end entity;

architecture rtl of la_lane_front is
  subtype word_t is std_logic_vector(LANES * WIDTH - 1 downto 0);
  subtype ch_t is std_logic_vector(WIDTH - 1 downto 0);
  signal w1, w2, w3 : word_t := (others => '0');
  signal p1 : ch_t := (others => '0');                       -- w1 の 1 つ前の語の最も新しいレーン
  signal mask_r, vm_r, rise_r, fall_r : ch_t := (others => '0');
  signal no_edges_r : boolean := true;
  signal hv, hv_r : std_logic_vector(LANES - 1 downto 0) := (others => '0');
  signal hv0_plain, hv0_plain_r : std_logic := '0';          -- レーン 0 のエッジを見ない判定（捕捉の最初の語に使う）
  signal run_prev : std_logic := '0';
  signal now : boolean;
  signal valid_r, hit_r : boolean := false;
  signal lane_r : unsigned(3 downto 0) := (others => '0');
  type ext_line_t is array (1 to EXT_DELAY) of std_logic;
  signal ext_line : ext_line_t := (others => '0');            -- ext_line(k) = ext_trigger を k 周期遅らせたもの
  signal hx_r : std_logic := '0';                            -- 判定の段の語のレーン EXT_LANE に外部トリガ（hv_r と同じ段）

  function lane(w : word_t; k : natural) return ch_t is
  begin
    return lane_slice(w, WIDTH, k);
  end function;
begin
  assert LANES <= 16 report "la_lane_front: LANES must be at most 16" severity failure;
  assert EXT_LANE < LANES report "la_lane_front: EXT_LANE must be below LANES" severity failure;

  now <= run = '1' and run_prev = '1';

  -- レーンごとの判定（w1 と、その 1 つ前のサンプル）
  process (all)
    variable s, p : ch_t;
    variable edge, match : boolean;
  begin
    for k in 0 to LANES - 1 loop
      s := lane(w1, k);
      if k = 0 then
        p := p1;
      else
        p := lane(w1, k - 1);
      end if;
      match := (s and mask_r) = vm_r;
      edge := no_edges_r or trig_edge(rise_r, fall_r, s, p);
      hv(k) <= '1' when match and edge else '0';
      if k = 0 then
        hv0_plain <= '1' when match and no_edges_r else '0';
      end if;
    end loop;
  end process;

  process (clk)
    variable use_hv : std_logic_vector(LANES - 1 downto 0);
    variable found : boolean;
  begin
    if rising_edge(clk) then
      mask_r   <= trig_mask;
      vm_r     <= trig_value and trig_mask;
      rise_r   <= trig_rise;
      fall_r   <= trig_fall;
      no_edges_r <= (trig_rise or trig_fall) = (trig_rise'range => '0');
      -- 段 1
      w1 <= lane_data;
      p1 <= lane(w1, LANES - 1);
      ext_line(1) <= ext_trigger;
      for k in 2 to EXT_DELAY loop
        ext_line(k) <= ext_line(k - 1);
      end loop;
      -- 段 2
      w2 <= w1;
      hv_r <= hv;
      hv0_plain_r <= hv0_plain;
      hx_r <= ext_line(EXT_DELAY);
      -- 段 3（出力）: 最初の語（この周期に初めて valid になる）のレーン 0 はエッジを見ない。外部トリガはレーン EXT_LANE に加える
      use_hv := hv_r;
      if now and not valid_r then
        use_hv(0) := hv0_plain_r;
      end if;
      use_hv(EXT_LANE) := use_hv(EXT_LANE) or hx_r;
      found := false;
      lane_r <= (others => '0');
      for k in 0 to LANES - 1 loop
        if use_hv(k) = '1' and not found then
          lane_r <= to_unsigned(k, 4);
          found := true;
        end if;
      end loop;
      hit_r   <= now and found;
      w3      <= w2;
      valid_r <= now;
      run_prev <= run;
      if rst = '1' or run = '0' then
        valid_r  <= false;
        hit_r    <= false;
      end if;
    end if;
  end process;

  sample_valid <= '1' when valid_r else '0';
  sample       <= w3;
  sample_hit   <= '1' when valid_r and hit_r else '0';
  sample_lane  <= lane_r;
end architecture;
