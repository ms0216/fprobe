-- 内蔵のロジアナ一式を、sys の整数倍の速いクロック cap_clk の領域で動かす（ボード非依存）。
-- 仕様: docs/superpowers/specs/2026-10-30-la-capture-clock-design.md
--
-- cap 領域に入れるもの: ピンのグリッチフィルタ（la_filter）、パルス幅のトリガ（la_pulse）、前段（la_front: 同期化・サンプル間隔・トリガ判定）、
-- BRAM への記録（la_capture）。サンプルは cap の周期ごとに最高 1 つ（rate = 0）。
-- clk（sys）と cap_clk は、同じ MMCM の VCO から割った整数比 RATIO のクロックで、時刻 0 の立ち上がりがそろう（関連クロック）。
-- そのため、領域の間は 1 段のレジスタで渡す（非同期の FIFO や 2 段の同期は使わない）。配線では関連クロックとして解析させ、
-- cap → sys・sys → cap の経路は 1 つのクロックの周期（cap の 1 周期）で間に合う形（レジスタからレジスタ、論理なし）にしてある。
--   sys → cap: 設定（run・rate・pretrigger・トリガの条件・フィルタ・パルスの設定）、ext_trigger、sys 領域の内部信号 2 本、タイムベース、
--              sys の周期の頭を示す反転ビット。設定は「止めてから変える」約束で、run は最後に書かれる（CSR の別の書き込み）。
--   cap → sys: 状態・番号・トリガの時刻と位相・BRAM の読み出し値。値はすべて同じ cap のエッジで確定し、同じ sys のエッジで一緒に取る。
-- 位相: トリガのサンプルを取った cap の周期の、sys の周期の中での番号（0〜RATIO − 1）。sys の周期の頭を示す反転ビットを cap で受けた
-- 周期を 0 として数える。ピンが変わった cap の周期の番号 n（ピンを cap の立ち上がり n の後に変えたとき、n + 1 の立ち上がりで取る）は
-- n = time × RATIO + phase − latency（cap の周期の数。latency は固定の遅れで、出力 latency に定数で出す。値は試験が RATIO = 2〜4 で測って確かめる）。
-- 配線の都合: 設定・タイムベース・周期の頭の印は、sys の側で 1 段のレジスタ（_s）を通してから cap に渡す（レジスタを cap の近くに置かせ、
-- cap の 1 周期で届くようにする）。これでタイムベースと印は一緒に 1 sys 周期（RATIO 周期）古くなるので、latency から RATIO を引いてある（RATIO <= 5）。
-- run・設定は止めてから変える約束なので 1 周期遅れてよい。rd_addr・ext_trigger・内部信号 2 本は sys の側の _s を通さない（読み出しの
-- 2 周期の約束と、ピンとの時間関係を段数で決めるため）。どれも cap の側の 1 段（_c）は通る。内部信号 2 本は cap の領域で、フィルタの
-- 段数と揃うまで遅らせる（la_probe_inputs。時刻の正しさの R2）。ext_trigger は la_front で遅らせ、外部トリガが 1 になった sys の周期 X
-- の頭（cap の周期 X × RATIO）の後に変わったピンのサンプルにトリガを付ける（時刻の正しさ S1b の N3: ピンはフィルタの 3 段、ext_trigger は
-- 写しの 1 段を通って la_front に来るので、la_front の揃え LA_FRONT_EXT_ALIGN に差の 2 段を足す。それまでは遅らせず、トリガのサンプルが
-- 外部トリガより 4 cap 周期前のピンのものになっていた）。docs/superpowers/specs/2026-11-01-time-accuracy-design.md
-- BRAM の読み出し: rd_addr を出してから 2 周期（sys）後に rd_data が有効（RATIO >= 2。la_mem_csr の応答の時間と同じ。BRAM の出力のレジスタ込み）。読むのは捕捉が止まっている間だけ。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.timing_pkg.all;

entity la_cap_domain is
  generic (
    WIDTH          : positive := 16;
    DEPTH          : positive := 8192;
    LA_PIN_COUNT   : natural := 8;               -- 外のピンの数（ch 0..LA_PIN_COUNT − 1）
    LA_INPUT_COUNT : natural := 0;               -- 専用の入力の数（ch LA_PIN_COUNT + 2 以降）
    RATIO          : positive range 2 to 5 := 3;
    LA_FILTER      : boolean := true;
    LA_PULSE       : boolean := true
  );
  port (
    clk             : in  std_logic;             -- sys
    cap_clk         : in  std_logic;
    rst             : in  std_logic;             -- sys（同期）
    ext_raw         : in  std_logic_vector(LA_PIN_COUNT + LA_INPUT_COUNT - 1 downto 0);   -- 非同期の外の信号
    sys_bit_a       : in  std_logic;             -- sys 領域の内部信号（ch LA_PIN_COUNT）
    sys_bit_b       : in  std_logic;             -- 同（ch LA_PIN_COUNT + 1）
    time            : in  unsigned(63 downto 0);
    ext_trigger     : in  std_logic;
    run             : in  std_logic;
    rate            : in  unsigned(15 downto 0);
    pretrigger      : in  unsigned(31 downto 0);
    trig_mask       : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_value      : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_rise       : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_fall       : in  std_logic_vector(WIDTH - 1 downto 0);
    filter_channels : in  std_logic_vector(WIDTH - 1 downto 0);
    filter_cycles   : in  unsigned(7 downto 0);   -- cap の周期
    pulse_mode      : in  std_logic_vector(1 downto 0);
    pulse_level     : in  std_logic;
    pulse_channel   : in  unsigned(3 downto 0);
    pulse_min       : in  unsigned(31 downto 0);  -- cap の周期
    pulse_max       : in  unsigned(31 downto 0);
    capturing       : out std_logic;
    triggered       : out std_logic;
    done            : out std_logic;
    start_index     : out unsigned(31 downto 0);
    trigger_index   : out unsigned(31 downto 0);
    trigger_time    : out unsigned(63 downto 0);
    trigger_phase   : out unsigned(3 downto 0);
    latency         : out unsigned(7 downto 0);  -- 定数（cap の周期）
    rd_addr         : in  unsigned(clog2(DEPTH) - 1 downto 0);
    rd_data         : out std_logic_vector(WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of la_cap_domain is
  constant ADDR_BITS : natural := clog2(DEPTH);
  -- 固定の遅れ（時刻の表 fprobe_gw.timing の LA_CAP_LATENCY_BASE − RATIO）。tests/tb/la_cap_domain_tb.py が RATIO = 2〜4 で測った値と
  -- 一致を確かめる
  constant LATENCY_CAP_CYCLES : natural := LA_CAP_LATENCY_BASE - RATIO;

  -- sys 領域
  signal tog, tog_s : std_logic := '0';
  signal run_s : std_logic := '0';
  signal rate_s : unsigned(15 downto 0) := (others => '0');
  signal pretrigger_s, pulse_min_s, pulse_max_s : unsigned(31 downto 0) := (others => '0');
  signal mask_s, value_s, rise_s, fall_s, filter_channels_s : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal filter_cycles_s : unsigned(7 downto 0) := (others => '0');
  signal pulse_mode_s : std_logic_vector(1 downto 0) := (others => '0');
  signal pulse_level_s : std_logic := '0';
  signal pulse_channel_s : unsigned(3 downto 0) := (others => '0');
  signal time_s : unsigned(63 downto 0) := (others => '0');

  -- cap 領域: sys からの写し
  signal rst_c, run_c, ext_trigger_c, sys_a_c, sys_b_c, pulse_level_c : std_logic := '0';
  signal rate_c : unsigned(15 downto 0) := (others => '0');
  signal pretrigger_c, pulse_min_c, pulse_max_c : unsigned(31 downto 0) := (others => '0');
  signal mask_c, value_c, rise_c, fall_c, filter_channels_c : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal filter_cycles_c : unsigned(7 downto 0) := (others => '0');
  signal pulse_mode_c : std_logic_vector(1 downto 0) := (others => '0');
  signal pulse_channel_c : unsigned(3 downto 0) := (others => '0');
  signal time_c : unsigned(63 downto 0) := (others => '0');
  signal rd_addr_c : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal tog_c1 : std_logic := '0';
  signal phase_c : unsigned(3 downto 0) := (others => '0');

  -- cap 領域: ロジアナ
  signal filter_mask_c : std_logic_vector(WIDTH - 1 downto 0);
  signal filter_cycles_m : unsigned(7 downto 0);
  signal probes : std_logic_vector(WIDTH - 1 downto 0);
  signal pw_enable, pw_event : std_logic;
  signal capturing_c, triggered_c, done_c : std_logic;
  signal start_c, trigger_c : unsigned(31 downto 0);
  signal time_trig_c : unsigned(63 downto 0);
  signal phase_trig_c : unsigned(3 downto 0);
  signal q_c : std_logic_vector(WIDTH - 1 downto 0);
begin
  latency <= to_unsigned(LATENCY_CAP_CYCLES, 8);

  assert LA_PIN_COUNT + 2 + LA_INPUT_COUNT <= WIDTH
    report "la_cap_domain: LA_PIN_COUNT + 2 + LA_INPUT_COUNT exceeds WIDTH" severity failure;

  -- sys の周期ごとに反転するビット（cap 領域が sys の周期の頭を知るため）
  process (clk)
  begin
    if rising_edge(clk) then
      tog <= not tog;
      tog_s           <= tog;
      time_s          <= time;
      run_s           <= run;
      rate_s          <= rate;
      pretrigger_s    <= pretrigger;
      mask_s          <= trig_mask;
      value_s         <= trig_value;
      rise_s          <= trig_rise;
      fall_s          <= trig_fall;
      filter_channels_s <= filter_channels;
      filter_cycles_s <= filter_cycles;
      pulse_mode_s    <= pulse_mode;
      pulse_level_s   <= pulse_level;
      pulse_channel_s <= pulse_channel;
      pulse_min_s     <= pulse_min;
      pulse_max_s     <= pulse_max;
    end if;
  end process;

  -- sys → cap（毎周期、そのまま写す）
  process (cap_clk)
  begin
    if rising_edge(cap_clk) then
      rst_c           <= rst;
      run_c           <= run_s;
      ext_trigger_c   <= ext_trigger;
      sys_a_c         <= sys_bit_a;
      sys_b_c         <= sys_bit_b;
      rate_c          <= rate_s;
      pretrigger_c    <= pretrigger_s;
      mask_c          <= mask_s;
      value_c         <= value_s;
      rise_c          <= rise_s;
      fall_c          <= fall_s;
      filter_channels_c <= filter_channels_s;
      filter_cycles_c <= filter_cycles_s;
      pulse_mode_c    <= pulse_mode_s;
      pulse_level_c   <= pulse_level_s;
      pulse_channel_c <= pulse_channel_s;
      pulse_min_c     <= pulse_min_s;
      pulse_max_c     <= pulse_max_s;
      time_c          <= time_s;
      rd_addr_c       <= rd_addr;
      -- 位相: sys の周期の頭を 0 とし、次の頭まで数える。頭は、cap の最初のエッジ（新しい反転ビットを取るエッジ）で見つける
      -- （tog と、前に取った値 tog_c1 の違い）。タイムベースの写し time_c が新しい値になるのと同じエッジなので、
      -- time_c と phase_c は常に同じ sys の周期のものになる。
      tog_c1 <= tog_s;
      if tog_s /= tog_c1 then
        phase_c <= (others => '0');
      elsif phase_c = RATIO - 1 then
        phase_c <= (others => '0');
      else
        phase_c <= phase_c + 1;
      end if;
    end if;
  end process;

  -- ロジアナの入力（la_probe_inputs）: 外の信号はグリッチフィルタ（cap の周期）を通り、sys 領域の内部信号 2 本（sys_a_c・sys_b_c。
  -- sys → cap の 1 段を通った後）は、フィルタと同じ段数になるまで遅らせて全 ch の時刻を揃える（R2。sys の FF が変わる辺は
  -- cap の辺でもあるので、cap の周期で揃う）。LA_FILTER = false の構成も同期化と揃えの段は同じにし（フィルタの ch の選びを 0 に
  -- する = 数えは定数で消える）、latency（LA_CAP_LATENCY_BASE − RATIO）と揃えが構成に依らないようにする
  filter_mask_c   <= filter_channels_c when LA_FILTER else (others => '0');
  filter_cycles_m <= filter_cycles_c when LA_FILTER else (others => '0');
  inputs_i : entity work.la_probe_inputs
    generic map (WIDTH => WIDTH, LA_PIN_COUNT => LA_PIN_COUNT, LA_INPUT_COUNT => LA_INPUT_COUNT, FILTER => true,
                 INTERNAL_AGE => 1)
    port map (clk => cap_clk, rst => rst_c, ext_raw => ext_raw, internal => sys_b_c & sys_a_c,
              filter_channels => filter_mask_c, filter_cycles => filter_cycles_m, probes => probes);

  pulse_g : if LA_PULSE generate
    pulse_i : entity work.la_pulse
      generic map (WIDTH => WIDTH, COUNT_BITS => 32)
      port map (clk => cap_clk, rst => rst_c, probes => probes, mode => pulse_mode_c, channel => pulse_channel_c,
                level => pulse_level_c, min_cycles => pulse_min_c, max_cycles => pulse_max_c, event => pw_event);
    pw_enable <= pulse_mode_c(0) xor pulse_mode_c(1);
  end generate;
  no_pulse_g : if not LA_PULSE generate
    pw_enable <= '0';
    pw_event  <= '0';
  end generate;

  capture_i : entity work.la_capture
    generic map (WIDTH => WIDTH, DEPTH => DEPTH, RD_REG => true, FAST => true,
                 EXT_DELAY => LA_FRONT_EXT_ALIGN + LA_FILTER_DELAY - 1)   -- 上の注（ピンのフィルタ 3 段 − ext_trigger の写し 1 段）
    port map (clk => cap_clk, rst => rst_c, probes => probes, time => time_c, phase => phase_c, ext_trigger => ext_trigger_c,
              run => run_c, rate => rate_c, pretrigger => pretrigger_c,
              trig_mask => mask_c, trig_value => value_c, trig_rise => rise_c, trig_fall => fall_c,
              pw_enable => pw_enable, pw_event => pw_event,
              capturing => capturing_c, triggered => triggered_c, done => done_c,
              start_index => start_c, trigger_index => trigger_c, trigger_time => time_trig_c,
              trigger_phase => phase_trig_c, rd_addr => rd_addr_c, rd_data => q_c);

  -- cap → sys（1 段）
  process (clk)
  begin
    if rising_edge(clk) then
      capturing     <= capturing_c;
      triggered     <= triggered_c;
      done          <= done_c;
      start_index   <= start_c;
      trigger_index <= trigger_c;
      trigger_time  <= time_trig_c;
      trigger_phase <= phase_trig_c;
      rd_data       <= q_c;
    end if;
  end process;
end architecture;
