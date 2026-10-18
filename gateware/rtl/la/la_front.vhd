-- ロジアナの前段（ボード非依存、clk のドメイン）: 入力の同期化、サンプル間隔、トリガ判定。
-- 内蔵バッファ（la_capture）と深いバッファ（la_encoder → 外部メモリ）が共用する。
-- 仕様: docs/superpowers/specs/2026-10-16-m7-logic-analyzer-design.md §2、2026-10-16-m8-ddr3-design.md §5
--
-- probes は非同期でよい（2 段で同期化する）。run = '1' の間、rate + 1 周期ごとに sample_valid を 1 周期出す
-- （run の 0→1 で数え始める）。sample_hit はそのサンプルでトリガが成立したか:
--   値一致 ((s and mask) = (value and mask)) かつ エッジ条件（rise / fall で指定したチャンネルのどれかが前のサンプルから
--   その向きに変化。指定なしなら真）、または ext_trigger（1 周期のパルスでよい。次のサンプルで報告する）。
-- arm = '0' の間に来た ext_trigger は捨てる（プリトリガを溜める間など）。
-- pw_enable = '1' なら、値一致・エッジ条件にさらに「前のサンプルからこのサンプルまでに pw_event（la_pulse のパルス幅の
-- 成立。1 周期のパルス）があった」を加える（U21。パターンの条件を使わないなら mask = 0・エッジなしにする）。
-- 判定は 1 段のパイプライン: サンプル n の値と判定は、1 周期遅れて sample / sample_hit に揃って出る。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity la_front is
  generic (
    WIDTH : positive := 16
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    probes       : in  std_logic_vector(WIDTH - 1 downto 0);
    ext_trigger  : in  std_logic;
    run          : in  std_logic;
    arm          : in  std_logic;
    rate         : in  unsigned(15 downto 0);
    trig_mask    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_value   : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_rise    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_fall    : in  std_logic_vector(WIDTH - 1 downto 0);
    pw_enable    : in  std_logic := '0';
    pw_event     : in  std_logic := '0';
    sample_valid : out std_logic;
    sample       : out std_logic_vector(WIDTH - 1 downto 0);
    sample_hit   : out std_logic
  );
end entity;

architecture rtl of la_front is
  signal s1, s2, s3 : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');   -- 同期化と 1 段の遅れ
  signal prev       : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');   -- 直前のサンプル
  signal run_prev   : std_logic := '0';
  signal tick       : unsigned(15 downto 0) := (others => '0');
  signal now        : boolean;                                                    -- この周期がサンプル
  signal hit        : boolean;
  signal hit_r      : boolean := false;
  signal valid_r    : boolean := false;
  signal ext_seen   : boolean := false;
  signal pw_seen    : boolean := false;                                           -- 前のサンプルから pw_event があった
  signal pw_ok      : boolean;
begin
  now <= run = '1' and run_prev = '1' and tick = 0;
  pw_ok <= pw_enable = '0' or pw_seen or pw_event = '1';

  -- トリガ条件（s2 が今回のサンプル、prev が前回）
  process (all)
    variable edges : boolean;
  begin
    if (trig_rise or trig_fall) = (trig_rise'range => '0') then
      edges := true;
    else
      edges := ((trig_rise and s2 and not prev) or (trig_fall and prev and not s2)) /= (s2'range => '0');
    end if;
    hit <= (s2 and trig_mask) = (trig_value and trig_mask) and edges;
  end process;

  process (clk)
  begin
    if rising_edge(clk) then
      s1       <= probes;
      s2       <= s1;
      s3       <= s2;
      run_prev <= run;
      valid_r  <= now;
      hit_r    <= now and hit and pw_ok;
      if ext_trigger = '1' then
        ext_seen <= true;
      end if;
      if now then                                     -- このサンプルで使った（pw_ok に含めた）
        pw_seen <= false;
      elsif pw_event = '1' then
        pw_seen <= true;
      end if;
      if rst = '1' or run = '0' then
        tick     <= (others => '0');
        valid_r  <= false;
        ext_seen <= false;
        pw_seen  <= false;
      else
        if run_prev = '0' or tick = rate then
          tick <= (others => '0');
        else
          tick <= tick + 1;
        end if;
        if now then
          prev <= s2;
        end if;
        -- 外部トリガは次のサンプルで報告して消す。arm = '0' なら報告せずに消す
        if valid_r and ext_trigger = '0' then
          ext_seen <= false;
        end if;
      end if;
    end if;
  end process;

  sample_valid <= '1' when valid_r else '0';
  sample       <= s3;
  sample_hit   <= '1' when valid_r and (hit_r or (ext_seen and arm = '1')) else '0';
end architecture;
