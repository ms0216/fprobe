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
-- サンプル間隔は降順のカウンタ cnt と「cnt = 0」の旗 cnt_zero（次の値から求めて持つ）で数える（出力の周期は元の昇順の数え方と同じ）。
-- 外部トリガの揃え（時刻の正しさ S1b の N3。docs/superpowers/specs/2026-11-01-time-accuracy-design.md）: ext_trigger を EXT_DELAY 周期
--   遅らせてから使う。EXT_DELAY = LA_FRONT_EXT_ALIGN（probes の同期化 s1・s2 の段）+ probes がこの la_front に来るまでに、ext_trigger の
--   来た周期より余計に通った段数（グリッチフィルタ等。呼び手が足す）にすると、ext_trigger が 1 の周期 X に端子が変わったピンのサンプル
--   （ピンの時間軸の X 以上 X + 1 未満）にトリガが付く（rate > 0 なら X 以後の最初のサンプル）。パターンのトリガとトリガ時刻の意味が同じになる。
-- FAST = true（速いクロック、300 MHz 級の取り込み領域）: 広い比較を毎周期の経路に置かない。
--   値一致とエッジの判定は、s2 になる前の値 s1 を 1 周期早く評価して match_r・edge_r に持つ（prev も次の値を使う）。
--   設定（rate・mask・value・rise・fall）は 1 周期遅れで取り、run の開始（最初のサンプル）も 1 周期遅らせる
--   （設定と run を同じ周期に変えても、最初のサンプルは新しい設定で判定される。run_prev は run を 2 周期遅らせたもの。
--   サンプルの格子は、FAST = false より 1 周期遅れて始まる）。FAST = false は元の回路と周期まで同じ。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.la_pkg.all;
use work.timing_pkg.all;

entity la_front is
  generic (
    WIDTH     : positive := 16;
    FAST      : boolean := false;
    EXT_DELAY : natural := LA_FRONT_EXT_ALIGN     -- ext_trigger を遅らせる周期（上の注）
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
  signal run_p1, run_prev : std_logic := '0';                                   -- run を 1・2 周期遅らせたもの
  signal cnt        : unsigned(15 downto 0) := (others => '0');                   -- 次のサンプルまでの残り周期
  signal cnt_zero   : boolean := true;                                            -- cnt = 0
  signal rate_r     : unsigned(15 downto 0) := (others => '0');
  signal rate_zero  : boolean := true;
  signal mask_r, vm_r, rise_r, fall_r : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');   -- vm_r = value and mask
  signal match_r, edge_r : boolean := false;                                      -- s2 について（1 周期早く評価）
  signal now        : boolean;                                                    -- この周期がサンプル
  signal hit        : boolean;
  signal hit_r      : boolean := false;
  signal valid_r    : boolean := false;
  signal ext_seen   : boolean := false;
  signal pw_seen    : boolean := false;                                           -- 前のサンプルから pw_event があった
  signal pw_ok      : boolean;
  signal edge_next, match_next, live_hit : boolean;
  signal sel_r, sel_next : boolean := true;                                     -- 「prev の次の値が s2 になる周期」（rst_r か run = 0 か now）を 1 周期早く決めて持つ
  signal rst_r : std_logic := '0';  -- rst を 1 周期遅らせたもの（edge_next の選択だけに使う。リセット信号の配線が遅い経路だった）
  signal rate_u     : unsigned(15 downto 0);
  signal rate_zero_u : boolean;
  type ext_line_t is array (0 to EXT_DELAY) of std_logic;
  signal ext_line   : ext_line_t := (others => '0');                            -- ext_line(k) = ext_trigger を k 周期遅らせたもの
  signal ext_d      : std_logic;
begin
  ext_line(0) <= ext_trigger;
  ext_delay_g : for k in 1 to EXT_DELAY generate
    process (clk)
    begin
      if rising_edge(clk) then
        ext_line(k) <= ext_line(k - 1);
      end if;
    end process;
  end generate;
  ext_d <= ext_line(EXT_DELAY);

  rate_u      <= rate_r when FAST else rate;
  rate_zero_u <= rate_zero when FAST else rate = 0;
  now <= run = '1' and run_prev = '1' and cnt_zero;
  pw_ok <= pw_enable = '0' or pw_seen or pw_event = '1';

  -- トリガ条件（s2 が今回のサンプル、prev が前回）。s1 は次の周期の s2、prev の次の値は「止まっている間か今がサンプルなら s2、でなければ prev」
  process (all)
    variable edge_s2, edge_prev : boolean;
  begin
    if (rise_r or fall_r) = (rise_r'range => '0') then
      edge_s2 := true;
      edge_prev := true;
    else
      edge_s2 := trig_edge(rise_r, fall_r, s1, s2);
      edge_prev := trig_edge(rise_r, fall_r, s1, prev);
    end if;
    if sel_r then                                        -- prev の次の値が s2 になる周期
      edge_next <= edge_s2;
    else
      edge_next <= edge_prev;
    end if;
    match_next <= (s1 and mask_r) = vm_r;
  end process;
  hit <= (match_r and edge_r) when FAST else live_hit;

  -- sel_r の次の値: 次の周期の now（run_prev は run_p1 の 1 周期後、cnt_zero は更新後の値）を、今の値から求める。
  -- run が変わる周期だけ実際の now と違うが、その周期のエッジは使われない（now = false か、止まっていて prev が s2 に追いつく）
  process (all)
    variable cz_n : boolean;
  begin
    if rst_r = '1' or run = '0' or run_prev = '0' then
      cz_n := true;
    elsif cnt_zero then
      cz_n := rate_zero_u;
    else
      cz_n := cnt = 1;
    end if;
    sel_next <= rst_r = '1' or run = '0' or (run_p1 = '1' and cz_n);
  end process;

  -- FAST = false: 元の回路（s2 と prev から、設定をそのまま使って今の周期で判定する）
  process (all)
    variable edges : boolean;
  begin
    if (trig_rise or trig_fall) = (trig_rise'range => '0') then
      edges := true;
    else
      edges := trig_edge(trig_rise, trig_fall, s2, prev);
    end if;
    live_hit <= (s2 and trig_mask) = (trig_value and trig_mask) and edges;
  end process;

  process (clk)
  begin
    if rising_edge(clk) then
      s1       <= probes;
      s2       <= s1;
      s3       <= s2;
      run_p1   <= run;
      if FAST then
        run_prev <= run_p1;
      else
        run_prev <= run;
      end if;
      mask_r   <= trig_mask;
      vm_r     <= trig_value and trig_mask;
      rise_r   <= trig_rise;
      fall_r   <= trig_fall;
      rate_r   <= rate;
      rate_zero <= rate = 0;
      edge_r   <= edge_next;
      rst_r    <= rst;
      sel_r    <= sel_next;
      match_r  <= match_next;
      valid_r  <= now;
      hit_r    <= now and hit and pw_ok;
      if ext_d = '1' then
        ext_seen <= true;
      end if;
      if now then                                     -- このサンプルで使った（pw_ok に含めた）
        pw_seen <= false;
      elsif pw_event = '1' then
        pw_seen <= true;
      end if;
      if rst = '1' or run = '0' then
        cnt      <= (others => '0');
        cnt_zero <= true;
        valid_r  <= false;
        ext_seen <= false;
        pw_seen  <= false;
        prev     <= s2;                               -- 止まっている間は現在の値を持つ（前の捕捉の値を持ち越さない）
      else
        if run_prev = '0' then
          cnt      <= (others => '0');
          cnt_zero <= true;
        elsif cnt_zero then                               -- 今がサンプル: 次のサンプルまで rate 周期
          cnt      <= rate_u;
          cnt_zero <= rate_zero_u;
        else
          cnt      <= cnt - 1;
          cnt_zero <= cnt = 1;
        end if;
        if now then
          prev <= s2;
        end if;
        -- 外部トリガは次のサンプルで報告して消す。arm = '0' なら報告せずに消す
        if valid_r and ext_d = '0' then
          ext_seen <= false;
        end if;
      end if;
    end if;
  end process;

  sample_valid <= '1' when valid_r else '0';
  sample       <= s3;
  sample_hit   <= '1' when valid_r and (hit_r or (ext_seen and arm = '1')) else '0';
end architecture;
