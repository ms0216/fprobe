-- ロジアナの入力を揃える（ボード非依存、clk のドメイン）: 外の信号（非同期）のグリッチフィルタ、内部の ch の段数合わせ、
-- ロジアナの ch の並び（ch 0〜LA_PIN_COUNT − 1 = ピン、続いて内部の 2 本、続いて専用の入力、残りは 0）への組み立て。
-- 仕様: docs/superpowers/specs/2026-11-01-time-accuracy-design.md §4.3 の R2
--
-- FILTER = true なら外の信号は la_filter（2 段の同期化 + 1 = timing_pkg の LA_FILTER_DELAY 段）を通る。内部の ch（clk に同期した
-- FF の出力。ここに来るまでに INTERNAL_AGE 段のレジスタを通ったもの）は LA_FILTER_DELAY − INTERNAL_AGE 段のレジスタで遅らせ、
-- 「時刻が T になる辺の直後に変わった外の信号」と「辺 T で変わった内部の FF」が同じ周期に probes に出るようにする
-- （後段の la_front の同期化・サンプル・トリガは全 ch に同じ。よって内部の ch の時刻はピンの時間軸そのもの = LA_CH_TIMING の 0）。
-- FILTER = false なら外の信号はそのまま通し（後段の la_front が同期化する）、内部の ch も INTERNAL_AGE = 0 なら遅らせない
-- （それで揃う）。FILTER = false で INTERNAL_AGE > 0 は揃えられないので elaboration で止める。
-- filter_channels はロジアナの ch の番号の並び（CSR la.filter）。外の信号の並び（ピン、専用の入力）に直して la_filter に渡す。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.timing_pkg.all;

entity la_probe_inputs is
  generic (
    WIDTH          : positive := 16;
    LA_PIN_COUNT   : natural := 8;
    LA_INPUT_COUNT : natural := 0;
    FILTER         : boolean := true;          -- 外の信号に la_filter を通す
    INTERNAL_AGE   : natural := 0              -- 内部の ch がここまでに通ったレジスタの段数
  );
  port (
    clk             : in  std_logic;
    rst             : in  std_logic;
    ext_raw         : in  std_logic_vector(LA_PIN_COUNT + LA_INPUT_COUNT - 1 downto 0);   -- 非同期の外の信号
    internal        : in  std_logic_vector(1 downto 0);                                   -- clk に同期した内部の信号
    filter_channels : in  std_logic_vector(WIDTH - 1 downto 0);
    filter_cycles   : in  unsigned(7 downto 0);
    probes          : out std_logic_vector(WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of la_probe_inputs is
  constant EXT_COUNT : positive := LA_PIN_COUNT + LA_INPUT_COUNT;

  function align_stages return natural is
  begin
    if FILTER then
      return LA_FILTER_DELAY - INTERNAL_AGE;
    end if;
    return 0;
  end function;
  constant ALIGN : natural := align_stages;

  type delay_t is array (0 to ALIGN) of std_logic_vector(1 downto 0);
  signal stage  : delay_t := (others => (others => '0'));
  signal ext_f, ext_mask : std_logic_vector(EXT_COUNT - 1 downto 0);
begin
  assert LA_PIN_COUNT + 2 + LA_INPUT_COUNT <= WIDTH
    report "la_probe_inputs: LA_PIN_COUNT + 2 + LA_INPUT_COUNT exceeds WIDTH" severity failure;
  assert INTERNAL_AGE <= LA_FILTER_DELAY and (FILTER or INTERNAL_AGE = 0)
    report "la_probe_inputs: the internal channels cannot be aligned with the pins (INTERNAL_AGE too large)" severity failure;

  ext_mask(LA_PIN_COUNT - 1 downto 0) <= filter_channels(LA_PIN_COUNT - 1 downto 0);
  inputs_mask_g : if LA_INPUT_COUNT > 0 generate
    ext_mask(EXT_COUNT - 1 downto LA_PIN_COUNT) <= filter_channels(LA_PIN_COUNT + 2 + LA_INPUT_COUNT - 1 downto LA_PIN_COUNT + 2);
  end generate;

  filter_g : if FILTER generate
    filter_i : entity work.la_filter
      generic map (WIDTH => EXT_COUNT, LEN_BITS => 8)
      port map (clk => clk, rst => rst, d => ext_raw, mask => ext_mask, cycles => filter_cycles, q => ext_f);
  end generate;
  no_filter_g : if not FILTER generate
    ext_f <= ext_raw;
  end generate;

  -- 内部の ch の遅れ（stage(0) = 入力、stage(ALIGN) = ALIGN 段の後）
  stage(0) <= internal;
  delay_g : for k in 1 to ALIGN generate
    process (clk)
    begin
      if rising_edge(clk) then
        stage(k) <= stage(k - 1);
      end if;
    end process;
  end generate;

  process (all)
  begin
    probes <= (others => '0');
    probes(LA_PIN_COUNT - 1 downto 0) <= ext_f(LA_PIN_COUNT - 1 downto 0);
    probes(LA_PIN_COUNT + 1 downto LA_PIN_COUNT) <= stage(ALIGN);
    if LA_INPUT_COUNT > 0 then
      probes(LA_PIN_COUNT + 2 + LA_INPUT_COUNT - 1 downto LA_PIN_COUNT + 2) <= ext_f(EXT_COUNT - 1 downto LA_PIN_COUNT);
    end if;
  end process;
end architecture;
