-- fprobe_core のロジアナの部分（sys ドメイン。CAP_RATIO > 1 の構成は cap_clk の領域も）: 入力の並び・前段・内蔵バッファ・設定。
--   ch の並び: 0〜LA_PIN_COUNT-1 = 拡張ヘッダのピン、続いて内部の信号 2 本（SWDIO を駆動中・DAP の処理中）、
--   続いて専用の入力 LA_INPUT_COUNT 本（U22。デバッグの線と兼用しない入力）、残りは 0。ケイパビリティ LA_LAYOUT と一致
--   ピンと専用の入力は la_filter（グリッチフィルタ）を通してからロジアナ・多段トリガ・la_pulse（パルス幅のトリガ）へ（U21）
-- 記録の回路は構成で 1 つ: la_capture（sys で 1 周期 1 サンプル）/ la_cap_domain（cap_clk の領域）/ la_lane_capture（1 周期に
-- LA_LANES サンプル）/ 無し（深いバッファのある構成。設定 csr_la は深いバッファも使う）。
-- CSR の窓: la・la_cap・la_testgen・la_mem・la_lanes_mem。
-- fprobe_core の中だけで使う（試験は fprobe_core の試験台から）。generic の意味は fprobe_core と同じ。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;
use work.csr_map_pkg.all;
use work.timing_pkg.all;
use work.core_pkg.all;

entity core_la is
  generic (
    LA_PIN_COUNT      : positive;
    LA_INPUT_COUNT    : natural;
    LA_INTERNAL       : boolean;
    LA_FILTER         : boolean;
    LA_PULSE          : boolean;
    CAP_RATIO         : natural;
    LA_TESTGEN        : boolean;
    LA_LANES          : positive;
    LA_LANE_ADDR_BITS : positive;
    LA_LANE_IN_AGE    : natural;
    LA_FINE_PINS      : boolean;
    USE_DEEP_LANES    : boolean          -- 深いバッファが 1 周期に LA_LANES サンプルで記録する（la_use_deep_lanes）
  );
  port (
    clk            : in  std_logic;
    cap_clk        : in  std_logic;
    rst            : in  core_resets_t;
    time_count     : in  unsigned(63 downto 0);
    csr_we         : in  std_logic;
    csr_addr       : in  csr_addr_t;
    csr_wdata      : in  csr_word_t;
    csr_req        : in  std_logic_vector(CSR_WINDOWS'range);
    csr_ack        : out std_logic_vector(CSR_WINDOWS'range);
    csr_err        : out std_logic_vector(CSR_WINDOWS'range);
    csr_rdata      : out csr_word_array_t(CSR_WINDOWS'range);
    -- 入力（fprobe_core のポートと同じ）
    la_pins        : in  std_logic_vector(LA_PIN_COUNT - 1 downto 0);
    la_inputs      : in  std_logic_vector(maximum(LA_INPUT_COUNT, 1) - 1 downto 0);
    la_input_lanes : in  std_logic_vector(maximum(LA_LANES * LA_INPUT_COUNT, 1) - 1 downto 0);
    la_pin_lanes   : in  std_logic_vector(LA_LANES * LA_PIN_COUNT - 1 downto 0);
    -- 内部の信号（core_debug）
    dap_busy       : in  std_logic;
    swdio_oe       : in  std_logic;
    la_ext         : in  std_logic;                  -- 外部トリガ（クロストリガ）
    deep_latency   : in  unsigned(7 downto 0);       -- 深いバッファの式の遅れ（USE_DEEP_LANES のとき la_cap.info.latency に出す）
    -- 深いバッファ・多段トリガへ
    la_probes      : out std_logic_vector(LA_WIDTH - 1 downto 0);   -- 前段を通した ch（sys）
    la_lane_data   : out std_logic_vector(LA_LANES * LA_WIDTH - 1 downto 0);   -- 1 周期の LA_LANES サンプル（LA_LANES > 1）
    trig_cfg       : out la_trig_cfg_t;
    pw_enable      : out std_logic;                  -- パルス幅のトリガ（la_front の pw_enable / pw_event）
    pw_event       : out std_logic;
    la_triggered   : out std_logic;
    -- 試験用の方形波（fprobe_core のポートと同じ）
    testgen_out    : out std_logic;
    testgen_oe     : out std_logic
  );
end entity;

architecture rtl of core_la is
  function owns(i : natural) return boolean is
  begin
    return i = CSR_WIN_LA or i = CSR_WIN_LA_CAP or i = CSR_WIN_LA_TESTGEN or i = CSR_WIN_LA_MEM or i = CSR_WIN_LA_LANES_MEM;
  end function;

  constant USE_LANES : boolean := la_use_lanes(LA_LANES, LA_INTERNAL);
  constant USE_CAP   : boolean := la_use_cap(CAP_RATIO, LA_LANES, LA_INTERNAL);
  constant EXT_DELAY : natural := la_ext_delay(LA_FILTER and not USE_CAP);
  constant LANE_WIDTH : positive := LA_LANES * LA_WIDTH;
  -- la_cap.info: 比（細かいサンプルの数 / sys の周期）とレーンの数
  function info_ratio_value return natural is
  begin
    if USE_LANES or USE_DEEP_LANES then
      return LA_LANES;
    end if;
    return CAP_RATIO;
  end function;
  constant LANES_OR_RATIO : natural := info_ratio_value;
  constant INFO_LANES : natural := LA_LANES * boolean'pos(USE_LANES or USE_DEEP_LANES) + 1 - boolean'pos(USE_LANES or USE_DEEP_LANES);
  -- 外部の入力（拡張ヘッダのピン、続いて専用の入力。U22）
  constant EXT_COUNT : positive := LA_PIN_COUNT + LA_INPUT_COUNT;

  signal ext_raw : std_logic_vector(EXT_COUNT - 1 downto 0);
  signal probes : std_logic_vector(LA_WIDTH - 1 downto 0);
  signal lane_data : std_logic_vector(LANE_WIDTH - 1 downto 0);
  signal run, capturing, triggered, done : std_logic;
  signal rate : std_logic_vector(15 downto 0);
  signal pretrigger, start_index, trigger_index : std_logic_vector(31 downto 0);
  signal mask, value, rise, fall : std_logic_vector(LA_WIDTH - 1 downto 0);
  -- グリッチフィルタ・パルス幅（U21）
  signal filter_channels : std_logic_vector(LA_WIDTH - 1 downto 0);     -- ビット n = ch n
  signal filter_cycles : std_logic_vector(7 downto 0);
  signal pulse_mode : std_logic_vector(1 downto 0);
  signal pulse_level : std_logic;
  signal pulse_channel : std_logic_vector(3 downto 0);
  signal pulse_min, pulse_max : std_logic_vector(31 downto 0);
  signal pw_enable_i, pw_event_i : std_logic;
  signal trigger_time : unsigned(63 downto 0);
  signal lane_rd_addr : unsigned(LA_LANE_ADDR_BITS - 1 downto 0);
  signal lane_rd_data : std_logic_vector(LANE_WIDTH - 1 downto 0);
  signal phase : unsigned(3 downto 0);
  signal selftest : std_logic := '0';                                        -- la_cap.control.selftest
  signal tg_en, tg_edge : std_logic := '0';                                  -- la_testgen.control
  signal tg_period, tg_high : std_logic_vector(31 downto 0) := (others => '0');
  signal selftest_tog : std_logic := '0';
  signal sys_bit_b : std_logic;                      -- sys の周期ごとに反転する信号、cap の ch LA_PIN_COUNT + 1 に入れる信号
  signal latency : unsigned(7 downto 0);                                -- トリガの位相（la_cap_domain。使わない構成は 0）
  signal rd_addr : unsigned(LA_ADDR_BITS - 1 downto 0);
  signal rd_data : std_logic_vector(LA_WIDTH - 1 downto 0);
  signal start_u, trigger_u : unsigned(31 downto 0);
begin
  idle_g : for i in CSR_WINDOWS'range generate
    other_g : if not owns(i) generate
      csr_ack(i)   <= '0';
      csr_err(i)   <= '0';
      csr_rdata(i) <= (others => '0');
    end generate;
  end generate;

  la_probes    <= probes;
  la_lane_data <= lane_data;
  trig_cfg     <= (rate => rate, pretrigger => pretrigger, mask => mask, value => value, rise => rise, fall => fall);
  pw_enable    <= pw_enable_i;
  pw_event     <= pw_event_i;
  la_triggered <= triggered;

  -- 外部の入力: [LA_PIN_COUNT-1:0] = 拡張ヘッダのピン、その上 = 専用の入力
  assert LA_PIN_COUNT + 2 + LA_INPUT_COUNT <= LA_WIDTH
    report "fprobe_core: LA_PIN_COUNT + 2 + LA_INPUT_COUNT exceeds LA_WIDTH" severity failure;
  assert not (USE_LANES or USE_DEEP_LANES) or (not LA_FILTER and not LA_PULSE and LA_LANES mod 2 = 0 and LA_LANES <= 8)
    report "fprobe_core: LA_LANES > 1 needs LA_FILTER = LA_PULSE = false and an even LA_LANES <= 8" severity failure;
  -- sys の構成のパルス幅のトリガは同期化した値（グリッチフィルタの出力）を見る。フィルタが無いと同期化していない値を 1 段で取る
  -- （メタ安定）。ケイパビリティの検査（fprobe_gw.caps）と同じ（時刻の正しさ S1b の N5）
  assert USE_CAP or LA_FILTER or not LA_PULSE
    report "fprobe_core: LA_PULSE needs LA_FILTER (the pulse-width trigger reads the synchronized probes)" severity failure;
  ext_raw(LA_PIN_COUNT - 1 downto 0)  <= la_pins;
  ext_inputs_g : if LA_INPUT_COUNT > 0 generate
    -- 1 周期に複数のサンプルの構成: 多段トリガ（sys）には、その周期の最も新しいレーンを渡す
    ext_raw(EXT_COUNT - 1 downto LA_PIN_COUNT)  <= la_inputs when not USE_LANES else
      la_input_lanes(LA_LANES * LA_INPUT_COUNT - 1 downto (LA_LANES - 1) * LA_INPUT_COUNT);
  end generate;

  -- ロジアナの入力（la_probe_inputs）: 0..LA_PIN_COUNT-1 = 外部ピン、続いて SWDIO をプローブが駆動中、DAP 処理中、専用の入力、残りは 0。
  -- グリッチフィルタ（U21）: 外部の入力の ch のうち filter_channels のものは filter_cycles 周期より短い変化を捨てる
  -- （フィルタした ch だけは cycles - 1 周期遅れる）。フィルタの段（timing_pkg の LA_FILTER_DELAY）を通らない内部の 2 本は、
  -- 同じ段数のレジスタで遅らせて全 ch の時刻を揃える（R2。docs/superpowers/specs/2026-11-01-time-accuracy-design.md）。
  -- 取り込みの領域の構成（USE_CAP）では、フィルタと揃えは la_cap_domain の中にあり、ここは多段トリガ（sys）に渡す素の値
  probe_inputs_i : entity work.la_probe_inputs
    generic map (WIDTH => LA_WIDTH, LA_PIN_COUNT => LA_PIN_COUNT, LA_INPUT_COUNT => LA_INPUT_COUNT,
                 FILTER => LA_FILTER and not USE_CAP, INTERNAL_AGE => 0)
    port map (clk => clk, rst => rst(RST_LA), ext_raw => ext_raw, internal => dap_busy & swdio_oe,
              filter_channels => filter_channels, filter_cycles => unsigned(filter_cycles), probes => probes);

  -- パルス幅のトリガ（U21）: ロジアナのトリガの条件に加える（la_front の pw_enable / pw_event）
  pulse_g : if LA_PULSE and not USE_CAP generate
    pulse_i : entity work.la_pulse
      generic map (WIDTH => LA_WIDTH, COUNT_BITS => 32)
      port map (clk => clk, rst => rst(RST_LA), probes => probes, mode => pulse_mode,
                channel => unsigned(pulse_channel), level => pulse_level,
                min_cycles => unsigned(pulse_min), max_cycles => unsigned(pulse_max), event => pw_event_i);
    pw_enable_i <= pulse_mode(0) xor pulse_mode(1);         -- mode 1・2（3 は何もしない: la_pulse と同じ）
  end generate;
  no_pulse_g : if not (LA_PULSE and not USE_CAP) generate
    pw_enable_i <= '0';
    pw_event_i  <= '0';
  end generate;

  -- ロジアナ一式を cap_clk の領域で動かす（ピンのフィルタ・パルス幅・前段・記録。sys との受け渡しは la_cap_domain の中）
  cap_la : if USE_CAP generate
    la_cap_i : entity work.la_cap_domain
      generic map (WIDTH => LA_WIDTH, DEPTH => 2 ** LA_ADDR_BITS, LA_PIN_COUNT => LA_PIN_COUNT,
                   LA_INPUT_COUNT => LA_INPUT_COUNT, RATIO => CAP_RATIO, LA_FILTER => LA_FILTER, LA_PULSE => LA_PULSE)
      port map (clk => clk, cap_clk => cap_clk, rst => rst(RST_LA), ext_raw => ext_raw,
                sys_bit_a => swdio_oe, sys_bit_b => sys_bit_b, time => time_count, ext_trigger => la_ext,
                run => run, rate => unsigned(rate), pretrigger => unsigned(pretrigger),
                trig_mask => mask, trig_value => value, trig_rise => rise, trig_fall => fall,
                filter_channels => filter_channels, filter_cycles => unsigned(filter_cycles),
                pulse_mode => pulse_mode, pulse_level => pulse_level, pulse_channel => unsigned(pulse_channel),
                pulse_min => unsigned(pulse_min), pulse_max => unsigned(pulse_max),
                capturing => capturing, triggered => triggered, done => done,
                start_index => start_u, trigger_index => trigger_u, trigger_time => trigger_time,
                trigger_phase => phase, latency => latency, rd_addr => rd_addr, rd_data => rd_data);
    start_index   <= std_logic_vector(start_u);
    trigger_index <= std_logic_vector(trigger_u);

    la_mem_cap_i : entity work.la_mem_csr
      generic map (WIDTH => LA_WIDTH, ADDR_BITS => LA_ADDR_BITS)
      port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_LA_MEM), bus_addr => csr_addr,
                bus_ack => csr_ack(CSR_WIN_LA_MEM), bus_rdata => csr_rdata(CSR_WIN_LA_MEM),
                rd_addr => rd_addr, rd_data => rd_data);
    csr_err(CSR_WIN_LA_MEM) <= '0';
  end generate;
  -- 1 周期に LA_LANES サンプル（la_lane_inputs）: 専用の入力はボードの直並列変換のレーン。ピンは LA_FINE_PINS ならレーン、そうでなければ
  -- sys で同期化した値、内部の信号は sys の値を、レーンごとにそのサンプルの細かい辺の前の sys の周期の値にして並べる
  -- （内部の ch を細かい ch の時間軸に揃える。時刻の正しさ S1b の N2: docs/superpowers/specs/2026-11-01-time-accuracy-design.md）
  lane_data_g : if LA_LANES > 1 generate
    lane_inputs_i : entity work.la_lane_inputs
      generic map (WIDTH => LA_WIDTH, LANES => LA_LANES, LA_PIN_COUNT => LA_PIN_COUNT, LA_INPUT_COUNT => LA_INPUT_COUNT,
                   FINE_PINS => LA_FINE_PINS, IN_AGE => LA_LANE_IN_AGE)
      port map (clk => clk, pins => la_pins, pin_lanes => la_pin_lanes, internal => sys_bit_b & swdio_oe,
                input_lanes => la_input_lanes, lane_data => lane_data);
  end generate;
  no_lane_data_g : if LA_LANES = 1 generate
    lane_data <= (others => '0');
  end generate;

  lanes_la : if USE_LANES generate
    la_lane_i : entity work.la_lane_capture
      generic map (WIDTH => LA_WIDTH, LANES => LA_LANES, DEPTH => 2 ** LA_LANE_ADDR_BITS, IN_AGE => LA_LANE_IN_AGE)
      port map (clk => clk, rst => rst(RST_LA), lane_data => lane_data, time => time_count, ext_trigger => la_ext,
                run => run, pretrigger => unsigned(pretrigger),
                trig_mask => mask, trig_value => value, trig_rise => rise, trig_fall => fall,
                capturing => capturing, triggered => triggered, done => done,
                start_index => start_u, trigger_index => trigger_u, trigger_time => trigger_time,
                trigger_lane => phase, latency => latency, rd_addr => lane_rd_addr, rd_data => lane_rd_data);
    start_index   <= std_logic_vector(start_u);
    trigger_index <= std_logic_vector(trigger_u);

    la_lanes_mem_i : entity work.la_mem_csr
      generic map (WIDTH => LANE_WIDTH, ADDR_BITS => LA_LANE_ADDR_BITS, SLICES => LA_LANES / 2)
      port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_LA_LANES_MEM), bus_addr => csr_addr,
                bus_ack => csr_ack(CSR_WIN_LA_LANES_MEM), bus_rdata => csr_rdata(CSR_WIN_LA_LANES_MEM),
                rd_addr => lane_rd_addr, rd_data => lane_rd_data);
    csr_err(CSR_WIN_LA_LANES_MEM) <= '0';

    -- 1 サンプル 1 ワードの窓 la_mem は無い（読むとエラー。バッファは la_lanes_mem）
    la_mem_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_LA_MEM), bus_ack => csr_ack(CSR_WIN_LA_MEM),
                bus_err => csr_err(CSR_WIN_LA_MEM), bus_rdata => csr_rdata(CSR_WIN_LA_MEM));
  end generate;
  no_lanes_la : if not USE_LANES generate
    lane_rd_addr <= (others => '0');
    lane_rd_data <= (others => '0');
    la_lanes_mem_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_LA_LANES_MEM), bus_ack => csr_ack(CSR_WIN_LA_LANES_MEM),
                bus_err => csr_err(CSR_WIN_LA_LANES_MEM), bus_rdata => csr_rdata(CSR_WIN_LA_LANES_MEM));
  end generate;
  no_cap_la : if not USE_CAP and not USE_LANES generate
    phase <= (others => '0');
    -- 深いバッファを N 並列で記録する構成: la_cap.info.latency は深いバッファの式の遅れ（la_deep.info.latency と同じ）
    latency <= deep_latency when USE_DEEP_LANES else (others => '0');
  end generate;

  -- 試験用: sys の周期ごとに反転する信号（selftest = 1 のとき ch LA_PIN_COUNT + 1 に dap_busy の代わりに入れる）
  process (clk)
  begin
    if rising_edge(clk) then
      selftest_tog <= not selftest_tog;
    end if;
  end process;
  sys_bit_b <= selftest_tog when selftest = '1' else dap_busy;   -- 段を置かない（dap_busy と同じ時間関係）

  -- 取り込みのクロックの比とトリガの位相（比 1 の構成には無い: 読むとエラー）
  la_cap_info : if CAP_RATIO > 1 or USE_LANES generate
    la_cap_csr_i : entity work.csr_la_cap
      port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_LA_CAP), bus_we => csr_we,
                bus_addr => csr_addr, bus_wdata => csr_wdata, bus_ack => csr_ack(CSR_WIN_LA_CAP),
                bus_err => csr_err(CSR_WIN_LA_CAP), bus_rdata => csr_rdata(CSR_WIN_LA_CAP),
                info_ratio => std_logic_vector(to_unsigned(LANES_OR_RATIO, 4)),
                info_lanes => std_logic_vector(to_unsigned(INFO_LANES, 4)),
                info_trigger_phase => std_logic_vector(phase),
                info_latency => std_logic_vector(latency),
                control_selftest => selftest);
  end generate;
  no_la_cap_info : if not (CAP_RATIO > 1 or USE_LANES) generate
    selftest <= '0';
    la_cap_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_LA_CAP), bus_ack => csr_ack(CSR_WIN_LA_CAP),
                bus_err => csr_err(CSR_WIN_LA_CAP), bus_rdata => csr_rdata(CSR_WIN_LA_CAP));
  end generate;

  -- 試験用の方形波（ケイパビリティ la_testgen）。刻むクロックは、取り込みが速い構成（CAP_RATIO > 1）なら cap_clk、そうでなければ clk。
  -- クロックを式で切り替えず、generate で配線を選ぶ（clk と cap_clk が同じでも la_testgen の受け渡し cdc_value は値を正しく届ける）
  la_testgen_csr : if LA_TESTGEN generate
    la_testgen_csr_i : entity work.csr_la_testgen
      port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_LA_TESTGEN), bus_we => csr_we,
                bus_addr => csr_addr, bus_wdata => csr_wdata, bus_ack => csr_ack(CSR_WIN_LA_TESTGEN),
                bus_err => csr_err(CSR_WIN_LA_TESTGEN), bus_rdata => csr_rdata(CSR_WIN_LA_TESTGEN),
                control_enable => tg_en, control_edge => tg_edge, period => tg_period, high => tg_high);
  end generate;
  testgen_fast : if LA_TESTGEN and CAP_RATIO > 1 generate
    testgen_i : entity work.la_testgen
      port map (clk => clk, rst => rst(RST_BUS), cap_clk => cap_clk, enable => tg_en, edge_sel => tg_edge,
                period => tg_period(15 downto 0), high => tg_high(15 downto 0), out_q => testgen_out, out_oe => testgen_oe);
  end generate;
  testgen_sys : if LA_TESTGEN and CAP_RATIO <= 1 generate
    testgen_i : entity work.la_testgen
      port map (clk => clk, rst => rst(RST_BUS), cap_clk => clk, enable => tg_en, edge_sel => tg_edge,
                period => tg_period(15 downto 0), high => tg_high(15 downto 0), out_q => testgen_out, out_oe => testgen_oe);
  end generate;
  no_testgen : if not LA_TESTGEN generate
    testgen_out <= '0';
    testgen_oe  <= '0';
    tg_en       <= '0';
    tg_edge     <= '0';
    tg_period   <= (others => '0');
    tg_high     <= (others => '0');
    la_testgen_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_LA_TESTGEN), bus_ack => csr_ack(CSR_WIN_LA_TESTGEN),
                bus_err => csr_err(CSR_WIN_LA_TESTGEN), bus_rdata => csr_rdata(CSR_WIN_LA_TESTGEN));
  end generate;

  internal_la : if LA_INTERNAL and not USE_CAP and not USE_LANES generate
  la_i : entity work.la_capture
    generic map (WIDTH => LA_WIDTH, DEPTH => 2 ** LA_ADDR_BITS, EXT_DELAY => EXT_DELAY)
    port map (clk => clk, rst => rst(RST_LA), probes => probes, time => time_count, ext_trigger => la_ext,
              run => run, rate => unsigned(rate), pretrigger => unsigned(pretrigger),
              trig_mask => mask, trig_value => value, trig_rise => rise, trig_fall => fall,
              pw_enable => pw_enable_i, pw_event => pw_event_i,
              capturing => capturing, triggered => triggered, done => done,
              start_index => start_u, trigger_index => trigger_u, trigger_time => trigger_time,
              rd_addr => rd_addr, rd_data => rd_data);
  start_index   <= std_logic_vector(start_u);
  trigger_index <= std_logic_vector(trigger_u);

  la_mem_i : entity work.la_mem_csr
    generic map (WIDTH => LA_WIDTH, ADDR_BITS => LA_ADDR_BITS)
    port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_LA_MEM), bus_addr => csr_addr,
              bus_ack => csr_ack(CSR_WIN_LA_MEM), bus_rdata => csr_rdata(CSR_WIN_LA_MEM),
              rd_addr => rd_addr, rd_data => rd_data);
  csr_err(CSR_WIN_LA_MEM) <= '0';                          -- バッファの窓は範囲外も 0 で答える（エラーにしない）
  end generate;

  -- 内蔵ロジアナが無い: 状態は 0、バッファの窓は読むとエラー（ケイパビリティの LA_DEPTH = 0）
  no_internal_la : if not LA_INTERNAL generate
    capturing     <= '0';
    triggered     <= '0';
    done          <= '0';
    start_index   <= (others => '0');
    trigger_index <= (others => '0');
    trigger_time  <= (others => '0');
    la_mem_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_LA_MEM), bus_ack => csr_ack(CSR_WIN_LA_MEM),
                bus_err => csr_err(CSR_WIN_LA_MEM), bus_rdata => csr_rdata(CSR_WIN_LA_MEM));
  end generate;

  la_csr_i : entity work.csr_la
    port map (clk => clk, rst => rst(RST_LA), bus_req => csr_req(CSR_WIN_LA), bus_we => csr_we,
              bus_addr => csr_addr, bus_wdata => csr_wdata, bus_ack => csr_ack(CSR_WIN_LA),
              bus_err => csr_err(CSR_WIN_LA), bus_rdata => csr_rdata(CSR_WIN_LA),
              control_run => run, rate => rate, pretrigger => pretrigger,
              trig_mask => mask, trig_value => value, trig_rise => rise, trig_fall => fall,
              status_capturing => capturing, status_triggered => triggered, status_done => done,
              start_index => start_index, trigger_index => trigger_index,
              trigger_time_lo => std_logic_vector(trigger_time(31 downto 0)),
              trigger_time_hi => std_logic_vector(trigger_time(63 downto 32)),
              filter_channels => filter_channels, filter_cycles => filter_cycles,
              pulse_mode => pulse_mode, pulse_level => pulse_level, pulse_channel => pulse_channel,
              pulse_min => pulse_min, pulse_max => pulse_max);
end architecture;
