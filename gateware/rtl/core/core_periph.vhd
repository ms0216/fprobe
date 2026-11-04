-- fprobe_core の周辺の部分（sys ドメイン）: SWO の受信・UART ブリッジ・補助マイコン・ボードの間の時刻の同期・対象の FPGA の観測。
--   SWO ピン → swo_receiver（設定は DAP の SWO コマンド。core_debug から）→ swo_out（USB の SWO エンドポイントへ）
--   UART（ホスト側ストリーム）⇄ uart_bridge ⇄ ターゲットの UART（状態は csr_uart）。TX はホストが最初の 1 バイトを
--     送るまで駆動しない（uart_tx_oe。B29）
-- CSR の窓: uart・aux・sync・obs。受けたバイト・計測値・同期の組・観測の記録はイベント記録の源として出す。
-- fprobe_core の中だけで使う（試験は fprobe_core の試験台から）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;
use work.csr_map_pkg.all;
use work.core_pkg.all;

entity core_periph is
  generic (
    CLK_HZ             : positive;
    HAS_SWO_MANCHESTER : boolean;
    UART_FLOW          : boolean;
    AUX                : boolean;
    SYNC               : boolean;
    OBS_PORT           : boolean
  );
  port (
    clk             : in  std_logic;
    rst             : in  core_resets_t;
    time_count      : in  unsigned(63 downto 0);
    csr_we          : in  std_logic;
    csr_addr        : in  csr_addr_t;
    csr_wdata       : in  csr_word_t;
    csr_req         : in  std_logic_vector(CSR_WINDOWS'range);
    csr_ack         : out std_logic_vector(CSR_WINDOWS'range);
    csr_err         : out std_logic_vector(CSR_WINDOWS'range);
    csr_rdata       : out csr_word_array_t(CSR_WINDOWS'range);
    -- SWO（設定と状態は core_debug の DAP の SWO コマンド。ほかは fprobe_core のポートと同じ）
    swo_run         : in  std_logic;
    swo_divisor     : in  unsigned(15 downto 0);
    swo_manchester  : in  std_logic;
    swo_error       : out std_logic;
    swo_overrun     : out std_logic;
    swo_i           : in  std_logic;
    swo_out_valid   : out std_logic;
    swo_out_ready   : in  std_logic;
    swo_out_data    : out std_logic_vector(7 downto 0);
    swo_flush       : out std_logic;
    -- UART ブリッジ（fprobe_core のポートと同じ）
    uart_baud       : in  unsigned(31 downto 0);
    uart_baud_load  : in  std_logic;
    uart_in_valid   : in  std_logic;
    uart_in_ready   : out std_logic;
    uart_in_data    : in  std_logic_vector(7 downto 0);
    uart_out_valid  : out std_logic;
    uart_out_ready  : in  std_logic;
    uart_out_data   : out std_logic_vector(7 downto 0);
    uart_rx         : in  std_logic;
    uart_tx         : out std_logic;
    uart_tx_oe      : out std_logic;
    uart_host_rts   : in  std_logic;
    uart_host_break : in  std_logic;
    uart_cts_n      : in  std_logic;
    uart_rts_n      : out std_logic;
    uart_rts_claim  : out std_logic;
    uart_rts_pin    : out std_logic_vector(1 downto 0);
    uart_cts_claim  : out std_logic;
    uart_cts_pin    : out std_logic_vector(1 downto 0);
    uart_cts        : out std_logic;
    -- 共有の割り算器（UART 側の口）
    div_req         : out std_logic;
    div_num         : out unsigned(31 downto 0);
    div_den         : out unsigned(31 downto 0);
    div_done        : in  std_logic;
    div_quot        : in  unsigned(31 downto 0);
    -- 補助マイコン・同期・観測（fprobe_core のポートと同じ）
    aux_rx          : in  std_logic;
    sync_in         : in  std_logic;
    sync_out        : out std_logic;
    obs_tck         : out std_logic;
    obs_tms         : out std_logic;
    obs_tdi         : out std_logic;
    obs_tdo         : in  std_logic;
    obs_oe          : out std_logic;
    obs_match       : out std_logic;                 -- FPGA の観測の値の一致（クロストリガの源 bit4）
    -- イベント記録の源
    uart_rx_event   : out byte_event_t;
    uart_tx_event   : out byte_event_t;
    swo_event       : out byte_event_t;
    aux_event       : out aux_event_t;
    obs_event       : out obs_event_t;
    sync_event      : out sync_event_t
  );
end entity;

architecture rtl of core_periph is
  function owns(i : natural) return boolean is
  begin
    return i = CSR_WIN_UART or i = CSR_WIN_AUX or i = CSR_WIN_SYNC or i = CSR_WIN_OBS;
  end function;

  signal swo_valid_i : std_logic;
  signal swo_data_i : std_logic_vector(7 downto 0);
  signal uart_out_valid_i : std_logic;
  signal uart_out_data_i : std_logic_vector(7 downto 0);
  signal uart_out_time, swo_out_time : unsigned(47 downto 0);   -- 受けたバイトの開始ビットの辺の時刻（R3）
  signal uart_baud_now, uart_frame_errors, uart_overruns : unsigned(31 downto 0);
  -- UART の制御線（U52）
  signal host_rts_s, host_break_s, flow_auto, flow_rts_claim, flow_cts_claim, uart_rts_n_i, uart_cts_i : std_logic;
  signal flow_rts_pin, flow_cts_pin : std_logic_vector(1 downto 0);
  -- 入力のレジスタスライス（ホストリンク側の FIFO と処理部の組み合わせ回路を切り離す）
  signal uart_s_valid, uart_s_ready : std_logic;
  signal uart_s : std_logic_vector(7 downto 0);
  signal uart_tx_used : std_logic;
begin
  idle_g : for i in CSR_WINDOWS'range generate
    other_g : if not owns(i) generate
      csr_ack(i)   <= '0';
      csr_err(i)   <= '0';
      csr_rdata(i) <= (others => '0');
    end generate;
  end generate;

  swo_i_rx : entity work.swo_receiver
    generic map (HAS_MANCHESTER => HAS_SWO_MANCHESTER)
    port map (clk => clk, rst => rst(RST_PERIPH), run => swo_run, manchester => swo_manchester, divisor => swo_divisor, swo => swo_i,
              time => time_count(47 downto 0),
              out_valid => swo_valid_i, out_ready => swo_out_ready, out_data => swo_data_i, out_time => swo_out_time,
              flush => swo_flush, error => swo_error, overrun => swo_overrun);
  swo_out_valid <= swo_valid_i;
  swo_out_data  <= swo_data_i;
  swo_event     <= (fire => swo_valid_i, data => swo_data_i, time => swo_out_time);

  uart_slice_i : entity work.stream_slice
    generic map (WIDTH => 8)
    port map (clk => clk, rst => rst(RST_PERIPH), in_valid => uart_in_valid, in_ready => uart_in_ready, in_data => uart_in_data,
              out_valid => uart_s_valid, out_ready => uart_s_ready, out_data => uart_s);

  host_rts_sync_i : entity work.sync_bit
    port map (clk => clk, d => uart_host_rts, q => host_rts_s);
  host_break_sync_i : entity work.sync_bit
    port map (clk => clk, d => uart_host_break, q => host_break_s);

  uart_i : entity work.uart_bridge
    generic map (CLK_HZ => CLK_HZ, SHARED_DIVIDER => true, FLOW => UART_FLOW)
    port map (clk => clk, rst => rst(RST_PERIPH), baud => uart_baud, baud_load => uart_baud_load,
              in_valid => uart_s_valid, in_ready => uart_s_ready, in_data => uart_s,
              out_valid => uart_out_valid_i, out_ready => uart_out_ready, out_data => uart_out_data_i,
              out_time => uart_out_time, time => time_count(47 downto 0),
              rx => uart_rx, tx => uart_tx, brk => host_break_s, flow_auto => flow_auto, rts_host => host_rts_s,
              cts_n => uart_cts_n, rts_n => uart_rts_n_i, cts => uart_cts_i, current_baud => uart_baud_now,
              frame_errors => uart_frame_errors, overruns => uart_overruns,
              div_req => div_req, div_num => div_num, div_den => div_den,
              div_done => div_done, div_quot => div_quot);
  uart_out_valid <= uart_out_valid_i;
  uart_out_data  <= uart_out_data_i;
  uart_rx_event  <= (fire => uart_out_valid_i and uart_out_ready, data => uart_out_data_i, time => uart_out_time);
  uart_tx_event  <= (fire => uart_s_valid and uart_s_ready, data => uart_s, time => (others => '0'));

  uart_rts_n <= uart_rts_n_i;
  uart_cts   <= uart_cts_i;
  -- 端子の割り当ては回路のある構成だけ（無い構成の CSR の設定は何もしない）
  uart_rts_claim <= flow_rts_claim when UART_FLOW else '0';
  uart_cts_claim <= flow_cts_claim when UART_FLOW else '0';
  uart_rts_pin   <= flow_rts_pin;
  uart_cts_pin   <= flow_cts_pin;

  -- B29: ブリッジへの入力のストリームで最初の 1 バイトが受け取られた周期から TX を駆動する（ブレークも駆動の
  -- 頼み: 線を Low にするには駆動が要る）。リセットで止める
  uart_tx_enable : process (clk)
  begin
    if rising_edge(clk) then
      if rst(RST_PERIPH) = '1' then
        uart_tx_used <= '0';
      elsif (uart_s_valid = '1' and uart_s_ready = '1') or host_break_s = '1' then
        uart_tx_used <= '1';
      end if;
    end if;
  end process;
  uart_tx_oe <= uart_tx_used;

  uart_csr_i : entity work.csr_uart
    port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_UART), bus_we => csr_we,
              bus_addr => csr_addr, bus_wdata => csr_wdata, bus_ack => csr_ack(CSR_WIN_UART),
              bus_err => csr_err(CSR_WIN_UART), bus_rdata => csr_rdata(CSR_WIN_UART),
              baud => std_logic_vector(uart_baud_now), frame_errors => std_logic_vector(uart_frame_errors),
              overruns => std_logic_vector(uart_overruns),
              flow_auto => flow_auto, flow_rts_claim => flow_rts_claim, flow_rts_pin => flow_rts_pin,
              flow_cts_claim => flow_cts_claim, flow_cts_pin => flow_cts_pin,
              lines_cts => uart_cts_i, lines_rts => not uart_rts_n_i, lines_host_rts => host_rts_s,
              lines_host_break => host_break_s);

  -- 補助マイコン（Pico）の計測値: ch 0（ターゲット電圧）の最新値を CSR に、全チャンネルの値をイベント 6 に
  aux_g : if AUX generate
    signal target_mv : std_logic_vector(13 downto 0) := (others => '0');
    signal samples, errors : unsigned(31 downto 0) := (others => '0');
    signal valid : std_logic;
    signal ch : natural range 0 to 3;
    signal value : unsigned(13 downto 0);
    signal stamp : unsigned(47 downto 0);
  begin
    aux_i : entity work.aux_link
      generic map (CLK_HZ => CLK_HZ, CHANNELS => 4)
      port map (clk => clk, rst => rst(RST_PERIPH), rx => aux_rx, time => time_count(47 downto 0), sample_valid => valid,
                sample_ch => ch, sample_value => value, sample_time => stamp, errors => errors);
    aux_event <= (valid => valid, ch => ch, value => value, time => stamp);
    process (clk)
    begin
      if rising_edge(clk) then
        if rst(RST_PERIPH) = '1' then
          samples <= (others => '0');
        elsif valid = '1' then
          if ch = 0 then
            target_mv <= std_logic_vector(value);
          end if;
          samples <= samples + 1;
        end if;
      end if;
    end process;
    aux_csr_i : entity work.csr_aux
      port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_AUX), bus_we => csr_we,
                bus_addr => csr_addr, bus_wdata => csr_wdata, bus_ack => csr_ack(CSR_WIN_AUX),
                bus_err => csr_err(CSR_WIN_AUX), bus_rdata => csr_rdata(CSR_WIN_AUX),
                target_mv => target_mv,
                samples => std_logic_vector(samples), errors => std_logic_vector(errors));
  end generate;
  no_aux_g : if not AUX generate
    aux_event <= (valid => '0', ch => 0, value => (others => '0'), time => (others => '0'));
    aux_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_AUX), bus_ack => csr_ack(CSR_WIN_AUX),
                bus_err => csr_err(CSR_WIN_AUX), bus_rdata => csr_rdata(CSR_WIN_AUX));
  end generate;

  -- ボードの間の時刻の同期: 親として sync_out に出し、子として sync_in の組をイベント 10 に
  sync_g : if SYNC generate
    signal enable, master, valid : std_logic;
    signal period : std_logic_vector(31 downto 0);
    signal local, parent : unsigned(63 downto 0);
    signal crc_errors, pairs : unsigned(15 downto 0);
  begin
    link_i : entity work.sync_link
      port map (clk => clk, rst => rst(RST_PERIPH), time_count => time_count, enable => enable, master => master,
                period => unsigned(period), sync_in => sync_in, sync_out => sync_out, pair_valid => valid,
                pair_local => local, pair_parent => parent, crc_errors => crc_errors);
    sync_event <= (valid => valid, local => std_logic_vector(local(47 downto 0)), parent => std_logic_vector(parent));
    process (clk)
    begin
      if rising_edge(clk) then
        if rst(RST_PERIPH) = '1' or enable = '0' then
          pairs <= (others => '0');
        elsif valid = '1' then
          pairs <= pairs + 1;
        end if;
      end if;
    end process;
    sync_csr_i : entity work.csr_sync
      port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_SYNC), bus_we => csr_we,
                bus_addr => csr_addr, bus_wdata => csr_wdata, bus_ack => csr_ack(CSR_WIN_SYNC),
                bus_err => csr_err(CSR_WIN_SYNC), bus_rdata => csr_rdata(CSR_WIN_SYNC),
                control_enable => enable, control_master => master, period => period,
                status_pairs => std_logic_vector(pairs), status_crc_errors => std_logic_vector(crc_errors));
  end generate;
  no_sync_g : if not SYNC generate
    sync_out   <= '0';
    sync_event <= (valid => '0', local => (others => '0'), parent => (others => '0'));
    sync_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_SYNC), bus_ack => csr_ack(CSR_WIN_SYNC),
                bus_err => csr_err(CSR_WIN_SYNC), bus_rdata => csr_rdata(CSR_WIN_SYNC));
  end generate;

  -- 対象の FPGA の観測: 対象の JTAG の観測の回路をスキャンし続け、記録をイベント 8、時刻合わせをイベント 9 に
  obs_g : if OBS_PORT generate
    signal enable, free_pins, push : std_logic;
    signal half_period, sync_period : std_logic_vector(15 downto 0);
    signal ir_len, ir_before, ir_after : std_logic_vector(5 downto 0);
    signal dr_before, dr_after : std_logic_vector(2 downto 0);
    signal user_ir, read_value, id : std_logic_vector(31 downto 0);
    signal cmd : std_logic_vector(15 downto 0);
    signal words : std_logic_vector(8 downto 0);
    signal running, id_ok, stuck, id_timeout, pending, overflow_seen, dropped, target_running, driving : std_logic;
    signal id_scans : std_logic_vector(7 downto 0);
    signal scans : unsigned(31 downto 0);
    signal m_mask, m_value, m_change : std_logic_vector(15 downto 0);
    signal m_on : std_logic;
    signal m_count : unsigned(31 downto 0);
    signal ev_valid, ev_sync : std_logic;
    signal ev_data, ev_addr : std_logic_vector(31 downto 0);
    signal ev_time : std_logic_vector(47 downto 0);
  begin
    obs_i : entity work.obs_jtag_engine
      port map (clk => clk, rst => rst(RST_PERIPH), time_count => time_count(47 downto 0), enable => enable,
                free_pins => free_pins, half_period => unsigned(half_period),
                sample_delay => 0, ir_len => unsigned(ir_len),   -- ponytail: 観測の口の TDO の遅れは High 相の終わり。遅れて届く対象が出たら CSR に
                ir_before => unsigned(ir_before), ir_after => unsigned(ir_after), dr_before => unsigned(dr_before),
                dr_after => unsigned(dr_after), user_ir => user_ir, words => unsigned(words),
                sync_period => unsigned(sync_period), id_scans => unsigned(id_scans), cmd_push => push, cmd_data => cmd,
                match_mask => m_mask, match_value => m_value, match_change => m_change, match_on => m_on,
                match => obs_match, match_count => m_count,
                running => running, target_running => target_running, driving => driving, id_ok => id_ok,
                stuck => stuck, id_timeout => id_timeout, cmd_pending => pending,
                overflow_seen => overflow_seen, cmd_dropped => dropped, scans => scans, read_value => read_value,
                id => id, ev_valid => ev_valid, ev_sync => ev_sync, ev_data => ev_data, ev_addr => ev_addr,
                ev_time => ev_time, tck => obs_tck, tms => obs_tms, tdi => obs_tdi, tdo => obs_tdo, oe => obs_oe);
    obs_event <= (valid => ev_valid, sync => ev_sync, data => ev_data, addr => ev_addr, time => ev_time);
    obs_csr_i : entity work.csr_obs
      port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_OBS), bus_we => csr_we,
                bus_addr => csr_addr, bus_wdata => csr_wdata, bus_ack => csr_ack(CSR_WIN_OBS),
                bus_err => csr_err(CSR_WIN_OBS), bus_rdata => csr_rdata(CSR_WIN_OBS),
                control_enable => enable, control_release => free_pins, half_period => half_period,
                chain_ir_len => ir_len, chain_ir_before => ir_before, chain_ir_after => ir_after,
                chain_dr_before => dr_before, chain_dr_after => dr_after, user_ir => user_ir, words => words,
                sync_period => sync_period, id_scans => id_scans, cmd => cmd, cmd_push => push,
                status_running => running, status_id_ok => id_ok, status_stuck => stuck, status_id_timeout => id_timeout,
                status_cmd_pending => pending, status_overflow_seen => overflow_seen, status_cmd_dropped => dropped,
                status_target_running => target_running, status_driving => driving,
                scans => std_logic_vector(scans), read_value => read_value, id => id,
                match_mask => m_mask, match_value => m_value, match_change_change => m_change,
                match_change_enable => m_on, match_count => std_logic_vector(m_count));
  end generate;
  no_obs_g : if not OBS_PORT generate
    obs_event <= (valid => '0', sync => '0', data => (others => '0'), addr => (others => '0'), time => (others => '0'));
    obs_tck   <= '1';
    obs_tms   <= '1';
    obs_tdi   <= '1';
    obs_oe    <= '0';
    obs_match <= '0';
    obs_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_OBS), bus_ack => csr_ack(CSR_WIN_OBS),
                bus_err => csr_err(CSR_WIN_OBS), bus_rdata => csr_rdata(CSR_WIN_OBS));
  end generate;
end architecture;
