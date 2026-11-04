-- fprobe_core のクロストリガの部分（sys ドメイン）: 源 → 動作の対応、多段トリガ、出力の端子。
--   源（sources）: bit0 ロジアナのトリガ（内蔵・深いバッファの立ち上がり）、bit1 番地一致、bit2 ホストの指令、bit3 多段トリガ、
--   bit4 FPGA の観測の値の一致、bit5 サンプラの値の一致 → 動作: ロジアナのトリガ（la_ext）・割り込み書き込み（inj_trigger）・
--   出力の端子（xtrig_out。docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md）。1 周期で届く
-- CSR の窓: xtrig（対応表・割り込み書き込みの語 0・番地一致の条件）・trigseq・xtrig_out。
-- fprobe_core の中だけで使う（試験は fprobe_core の試験台から）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;
use work.csr_map_pkg.all;
use work.core_pkg.all;

entity core_xtrig is
  generic (
    XTRIG        : boolean;
    TRIG_SEQ     : boolean;
    XOUT_COUNT   : natural range 0 to 2;
    LA_PIN_COUNT : positive
  );
  port (
    clk            : in  std_logic;
    rst            : in  core_resets_t;
    time_count     : in  unsigned(63 downto 0);
    csr_we         : in  std_logic;
    csr_addr       : in  csr_addr_t;
    csr_wdata      : in  csr_word_t;
    csr_req        : in  std_logic_vector(CSR_WINDOWS'range);
    csr_ack        : out std_logic_vector(CSR_WINDOWS'range);
    csr_err        : out std_logic_vector(CSR_WINDOWS'range);
    csr_rdata      : out csr_word_array_t(CSR_WINDOWS'range);
    -- 源
    la_probes      : in  std_logic_vector(LA_WIDTH - 1 downto 0);   -- 多段トリガのパターン（ロジアナと同じ ch）
    la_pins        : in  std_logic_vector(LA_PIN_COUNT - 1 downto 0);   -- 出力の端子の並びの条件（外部ピン 0〜7）
    la_triggered   : in  std_logic;
    deep_triggered : in  std_logic;
    match          : in  std_logic;
    match_time     : in  unsigned(63 downto 0);
    obs_match      : in  std_logic;
    smp_match      : in  std_logic;
    -- 動作
    la_ext         : out std_logic;                  -- ロジアナの外部トリガ
    inj_trigger    : out std_logic;                  -- 割り込み書き込みへ発火した周期
    inj_request    : out std_logic;                  -- その 1 周期後（swd_injector の request）
    -- 割り込み書き込み・番地一致の設定と状態（core_debug）
    inject_cfg     : out inject_cfg_t;
    match_cfg      : out match_cfg_t;
    inject_status  : in  inject_status_t;
    -- 出力の端子（fprobe_core のポートと同じ）
    xout_o         : out std_logic_vector(1 downto 0);
    xout_oe        : out std_logic_vector(1 downto 0);
    xout_claim     : out std_logic_vector(1 downto 0);
    xout_pin       : out std_logic_vector(3 downto 0)
  );
end entity;

architecture rtl of core_xtrig is
  function owns(i : natural) return boolean is
  begin
    return i = CSR_WIN_XTRIG or i = CSR_WIN_TRIGSEQ or i = CSR_WIN_XTRIG_OUT;
  end function;

  signal route_to_la, route_to_inject, sources : std_logic_vector(5 downto 0);
  signal host_fire, la_triggered_prev, deep_triggered_prev, inj_trigger_i : std_logic := '0';
  signal seq_fire : std_logic;
  signal match_count : unsigned(31 downto 0) := (others => '0');
  signal match_time_r : unsigned(63 downto 0) := (others => '0');
begin
  idle_g : for i in CSR_WINDOWS'range generate
    other_g : if not owns(i) generate
      csr_ack(i)   <= '0';
      csr_err(i)   <= '0';
      csr_rdata(i) <= (others => '0');
    end generate;
  end generate;

  inj_trigger_i <= or (sources and route_to_inject);
  inj_trigger   <= inj_trigger_i;
  sources <= smp_match & obs_match & seq_fire & host_fire & match & ((la_triggered and not la_triggered_prev) or
                                    (deep_triggered and not deep_triggered_prev));
  process (clk)
  begin
    if rising_edge(clk) then
      la_triggered_prev   <= la_triggered;
      deep_triggered_prev <= deep_triggered;
      la_ext      <= or (sources and route_to_la);
      inj_request <= inj_trigger_i;
      if rst(RST_LA) = '1' then
        match_count <= (others => '0');
      elsif match = '1' then
        match_count  <= match_count + 1;
        match_time_r <= match_time;
      end if;
    end if;
  end process;

  -- 多段トリガ: パターン A / B（ロジアナと同じピン）・番地一致・ホストの指令を段ごとの条件で数える
  trig_seq_g : if TRIG_SEQ generate
    signal seq_enable, seq_fired : std_logic;
    signal seq_stages : std_logic_vector(2 downto 0);
    signal seq_stage : unsigned(1 downto 0);
    signal seq_a_mask, seq_a_value, seq_a_rise, seq_a_fall : std_logic_vector(15 downto 0);
    signal seq_b_mask, seq_b_value, seq_b_rise, seq_b_fall : std_logic_vector(15 downto 0);
    signal seq_cond0, seq_cond1, seq_cond2, seq_cond3 : std_logic_vector(3 downto 0);
    signal seq_count0, seq_count1, seq_count2, seq_count3 : std_logic_vector(15 downto 0);
    signal seq_within1, seq_within2, seq_within3 : std_logic_vector(31 downto 0);
    signal seq_time : unsigned(63 downto 0) := (others => '0');
  begin
  seq_i : entity work.trig_seq
    generic map (WIDTH => 16)
    port map (clk => clk, rst => rst(RST_LA), probes => la_probes, match => match, host => host_fire,
              enable => seq_enable, stages => unsigned(seq_stages),
              a_mask => seq_a_mask, a_value => seq_a_value, a_rise => seq_a_rise, a_fall => seq_a_fall,
              b_mask => seq_b_mask, b_value => seq_b_value, b_rise => seq_b_rise, b_fall => seq_b_fall,
              cond0 => seq_cond0, cond1 => seq_cond1, cond2 => seq_cond2, cond3 => seq_cond3,
              count0 => unsigned(seq_count0), count1 => unsigned(seq_count1),
              count2 => unsigned(seq_count2), count3 => unsigned(seq_count3),
              within1 => unsigned(seq_within1), within2 => unsigned(seq_within2), within3 => unsigned(seq_within3),
              fire => seq_fire, fired => seq_fired, stage => seq_stage);
  process (clk)
  begin
    if rising_edge(clk) then
      if seq_fire = '1' then
        seq_time <= time_count;
      end if;
    end if;
  end process;

  trigseq_csr_i : entity work.csr_trigseq
    port map (clk => clk, rst => rst(RST_LA), bus_req => csr_req(CSR_WIN_TRIGSEQ), bus_we => csr_we,
              bus_addr => csr_addr, bus_wdata => csr_wdata, bus_ack => csr_ack(CSR_WIN_TRIGSEQ),
              bus_err => csr_err(CSR_WIN_TRIGSEQ), bus_rdata => csr_rdata(CSR_WIN_TRIGSEQ),
              control_enable => seq_enable, control_stages => seq_stages,
              status_stage => std_logic_vector(seq_stage), status_fired => seq_fired,
              pattern_a_mask => seq_a_mask, pattern_a_value => seq_a_value,
              edge_a_rise => seq_a_rise, edge_a_fall => seq_a_fall,
              pattern_b_mask => seq_b_mask, pattern_b_value => seq_b_value,
              edge_b_rise => seq_b_rise, edge_b_fall => seq_b_fall,
              stage0_cond => seq_cond0, stage0_count => seq_count0, stage1_cond => seq_cond1, stage1_count => seq_count1,
              stage2_cond => seq_cond2, stage2_count => seq_count2, stage3_cond => seq_cond3, stage3_count => seq_count3,
              within1 => seq_within1, within2 => seq_within2, within3 => seq_within3,
              fire_time_lo => std_logic_vector(seq_time(31 downto 0)),
              fire_time_hi => std_logic_vector(seq_time(63 downto 32)));
  end generate;

  no_trig_seq : if not TRIG_SEQ generate
    seq_fire  <= '0';
    trigseq_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_TRIGSEQ), bus_ack => csr_ack(CSR_WIN_TRIGSEQ),
                bus_err => csr_err(CSR_WIN_TRIGSEQ), bus_rdata => csr_rdata(CSR_WIN_TRIGSEQ));
  end generate;

  xtrig_csr_g : if XTRIG generate
  xtrig_csr_i : entity work.csr_xtrig
    port map (clk => clk, rst => rst(RST_LA), bus_req => csr_req(CSR_WIN_XTRIG), bus_we => csr_we,
              bus_addr => csr_addr, bus_wdata => csr_wdata, bus_ack => csr_ack(CSR_WIN_XTRIG),
              bus_err => csr_err(CSR_WIN_XTRIG), bus_rdata => csr_rdata(CSR_WIN_XTRIG),
              route_to_la => route_to_la, route_to_inject => route_to_inject, command_fire => host_fire,
              inject_ap => inject_cfg.ap, inject_addr => inject_cfg.addr, inject_value => inject_cfg.value,
              inject_status_busy => inject_status.busy, inject_status_result => inject_status.result,
              inject_status_ack => inject_status.ack, inject_status_count => std_logic_vector(inject_status.count),
              inject_time_lo => std_logic_vector(inject_status.time(31 downto 0)),
              inject_time_hi => std_logic_vector(inject_status.time(63 downto 32)),
              match_addr => match_cfg.addr, match_addr_mask => match_cfg.addr_mask, match_data => match_cfg.data,
              match_data_mask => match_cfg.data_mask, match_mode_on_write => match_cfg.on_write,
              match_mode_on_read => match_cfg.on_read, match_count => std_logic_vector(match_count),
              match_time_lo => std_logic_vector(match_time_r(31 downto 0)),
              match_time_hi => std_logic_vector(match_time_r(63 downto 32)));
  end generate;

  no_xtrig_csr : if not XTRIG generate
    inject_cfg      <= (ap => (others => '0'), addr => (others => '0'), value => (others => '0'));
    route_to_la     <= (others => '0');
    route_to_inject <= (others => '0');
    host_fire       <= '0';
    match_cfg       <= (addr => (others => '0'), addr_mask => (others => '0'), data => (others => '0'),
                        data_mask => (others => '0'), on_write => '0', on_read => '0');
    xtrig_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_XTRIG), bus_ack => csr_ack(CSR_WIN_XTRIG),
                bus_err => csr_err(CSR_WIN_XTRIG), bus_rdata => csr_rdata(CSR_WIN_XTRIG));
  end generate;

  -- クロストリガの動作「出力の端子」: 源（sources）と、ロジアナの外部ピン 0〜7 の並び → 出力 k
  xout_g : if XOUT_COUNT > 0 generate
    constant N : positive := XOUT_COUNT;
    type cfg8_t is array (0 to 1) of std_logic_vector(7 downto 0);
    type cfg6_t is array (0 to 1) of std_logic_vector(5 downto 0);
    type cfg3_t is array (0 to 1) of std_logic_vector(2 downto 0);
    type cfg2_t is array (0 to 1) of std_logic_vector(1 downto 0);
    signal w_mask, w_value, c_length : cfg8_t;
    signal w_sources : cfg6_t;
    signal c_unit : cfg3_t;
    signal c_mode, c_pin : cfg2_t;
    signal w_any, c_enable, c_level, c_claim : std_logic_vector(1 downto 0);
    signal f_mask, f_value, f_length : std_logic_vector(8 * N - 1 downto 0);
    signal f_src : std_logic_vector(6 * N - 1 downto 0);
    signal f_unit : std_logic_vector(3 * N - 1 downto 0);
    signal f_mode : std_logic_vector(2 * N - 1 downto 0);
    signal out_o, out_oe : std_logic_vector(N - 1 downto 0);
    signal pins8 : std_logic_vector(7 downto 0);
    signal active : std_logic_vector(1 downto 0);
  begin
    xout_csr_i : entity work.csr_xtrig_out
      port map (clk => clk, rst => rst(RST_LA), bus_req => csr_req(CSR_WIN_XTRIG_OUT), bus_we => csr_we,
                bus_addr => csr_addr, bus_wdata => csr_wdata, bus_ack => csr_ack(CSR_WIN_XTRIG_OUT),
                bus_err => csr_err(CSR_WIN_XTRIG_OUT), bus_rdata => csr_rdata(CSR_WIN_XTRIG_OUT),
                status_active => active,
                when0_mask => w_mask(0), when0_value => w_value(0), when0_sources => w_sources(0), when0_any => w_any(0),
                ctrl0_enable => c_enable(0), ctrl0_mode => c_mode(0), ctrl0_level => c_level(0),
                ctrl0_claim => c_claim(0), ctrl0_length => c_length(0), ctrl0_unit => c_unit(0), ctrl0_pin => c_pin(0),
                when1_mask => w_mask(1), when1_value => w_value(1), when1_sources => w_sources(1), when1_any => w_any(1),
                ctrl1_enable => c_enable(1), ctrl1_mode => c_mode(1), ctrl1_level => c_level(1),
                ctrl1_claim => c_claim(1), ctrl1_length => c_length(1), ctrl1_unit => c_unit(1), ctrl1_pin => c_pin(1));

    flat : for k in 0 to N - 1 generate
      f_mask(8 * k + 7 downto 8 * k)   <= w_mask(k);
      f_value(8 * k + 7 downto 8 * k)  <= w_value(k);
      f_length(8 * k + 7 downto 8 * k) <= c_length(k);
      f_src(6 * k + 5 downto 6 * k)    <= w_sources(k);
      f_unit(3 * k + 2 downto 3 * k)   <= c_unit(k);
      f_mode(2 * k + 1 downto 2 * k)   <= c_mode(k);
    end generate;

    -- 端子の並びはロジアナの外部ピン 0〜7（ピンが 8 本より少なければ残りは 0）
    process (all)
    begin
      pins8 <= (others => '0');
      pins8(minimum(LA_PIN_COUNT, 8) - 1 downto 0) <= la_pins(minimum(LA_PIN_COUNT, 8) - 1 downto 0);
    end process;

    xout_i : entity work.xtrig_out
      generic map (COUNT => N, WIDTH => 8)
      port map (clk => clk, rst => rst(RST_LA), pins => pins8, sources => sources, time => time_count(31 downto 0),
                enable => c_enable(N - 1 downto 0), mode => f_mode, level => c_level(N - 1 downto 0),
                any => w_any(N - 1 downto 0), mask => f_mask, value => f_value, src => f_src,
                length => f_length, unit => f_unit, o => out_o, oe => out_oe);

    process (all)
    begin
      xout_o     <= (others => '0');
      xout_oe    <= (others => '0');
      xout_claim <= (others => '0');
      xout_pin   <= (others => '0');
      active     <= (others => '0');
      xout_o(N - 1 downto 0)     <= out_o;
      xout_oe(N - 1 downto 0)    <= out_oe;
      xout_claim(N - 1 downto 0) <= c_claim(N - 1 downto 0);
      for k in 0 to N - 1 loop
        xout_pin(2 * k + 1 downto 2 * k) <= c_pin(k);
      end loop;
      active(N - 1 downto 0)     <= out_oe;
    end process;
  end generate;

  no_xout_g : if XOUT_COUNT = 0 generate
    xout_o     <= (others => '0');
    xout_oe    <= (others => '0');
    xout_claim <= (others => '0');
    xout_pin   <= (others => '0');
    xout_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_XTRIG_OUT), bus_ack => csr_ack(CSR_WIN_XTRIG_OUT),
                bus_err => csr_err(CSR_WIN_XTRIG_OUT), bus_rdata => csr_rdata(CSR_WIN_XTRIG_OUT));
  end generate;
end architecture;
