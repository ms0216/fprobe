-- fprobe_core の記録の部分（sys ドメイン。外部メモリは mem_clk のドメイン）: イベント記録と外部メモリ。
--   イベント記録（csr_events）: event_collector が各部の源（DAP の転送・割り込み書き込み・サンプラ・バイト・補助・観測・同期・
--     PHY の設定）に時刻を付けて 1 件 128 ビットにまとめ、外部メモリの深いバッファ（la_deep_buffer）か内蔵 RAM のリング
--     （event_ring。外部メモリの無い構成）へ
--   外部メモリ（csr_mem）: mem_test_sys（状態とメモリ試験。メモリは mem_port で、mem_clk のドメイン）
--   ロジアナの深いバッファ（csr_la_deep）: la_deep_buffer（外部メモリへ記録し、読み出しは stream_out = USB のストリーム）。
--     PS が DDR のリングを直接読む流れの位置の約束は la_ps_port（PYNQ）
-- fprobe_core の中だけで使う（試験は fprobe_core の試験台から）。generic の意味は fprobe_core と同じ。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;
use work.csr_map_pkg.all;
use work.util_pkg.all;
use work.core_pkg.all;

entity core_storage is
  generic (
    MEM_ADDR_BITS              : natural;
    MEM_TEST                   : boolean;
    LA_STREAM_EVENTS           : boolean;
    LA_STREAM_GAPS             : boolean;
    LA_RLE_TOKENS              : boolean;
    LA_RLE_REPEAT              : boolean := false;
    EVENT_RING_BITS            : natural;
    EVENT_COUNT_START_BELOW_4G : natural;
    EVENT_BYTE_PACK            : boolean;
    MEM_SAME_CLOCK             : boolean;
    LA_LANES                   : positive;
    LA_LANE_IN_AGE             : natural;
    USE_DEEP_LANES             : boolean;    -- 深いバッファを 1 周期に LA_LANES サンプルで記録する（la_use_deep_lanes）
    EXT_DELAY                  : natural     -- 外部トリガの揃え（la_ext_delay）
  );
  port (
    clk              : in  std_logic;
    rst              : in  core_resets_t;
    time_count       : in  unsigned(63 downto 0);
    csr_cmd          : in  csr_cmd_t;
    csr_req          : in  std_logic_vector(CSR_WINDOWS'range);
    csr_rsp          : out csr_rsp_array_t(CSR_WINDOWS'range);
    -- ロジアナ（core_la）とクロストリガ
    la_probes        : in  std_logic_vector(LA_WIDTH - 1 downto 0);
    la_lane_data     : in  std_logic_vector(LA_LANES * LA_WIDTH - 1 downto 0);
    trig_cfg         : in  la_trig_cfg_t;
    pw_enable        : in  std_logic;
    pw_event         : in  std_logic;
    la_ext           : in  std_logic;
    deep_triggered   : out std_logic;
    deep_latency     : out unsigned(7 downto 0);
    -- イベント記録の源
    xfer_event       : in  xfer_event_t;
    inject_event     : in  inject_event_t;
    sampler_event    : in  sampler_event_t;
    uart_rx_event    : in  byte_event_t;
    uart_tx_event    : in  byte_event_t;
    swo_event        : in  byte_event_t;
    aux_event        : in  aux_event_t;
    obs_event        : in  obs_event_t;
    sync_event       : in  sync_event_t;
    cfg_word         : in  std_logic_vector(71 downto 0);
    -- 外部メモリ・ストリーム・PS（fprobe_core のポートと同じ）
    mem_clk          : in  std_logic;
    mem_rst          : in  std_logic;
    mem_ready        : in  std_logic;
    mem_cmd_valid    : out std_logic;
    mem_cmd_ready    : in  std_logic;
    mem_cmd_write    : out std_logic;
    mem_cmd_addr     : out unsigned(maximum(MEM_ADDR_BITS, 1) - 1 downto 0);
    mem_cmd_data     : out std_logic_vector(127 downto 0);
    mem_rd_valid     : in  std_logic;
    mem_rd_data      : in  std_logic_vector(127 downto 0);
    mem_wr_pending   : in  unsigned(3 downto 0);
    stream_out_valid : out std_logic;
    stream_out_ready : in  std_logic;
    stream_out_data  : out std_logic_vector(7 downto 0);
    ps_bus_rd        : in  std_logic;
    ps_bus_wr        : in  std_logic;
    ps_bus_addr      : in  unsigned(7 downto 0);
    ps_bus_wdata     : in  std_logic_vector(31 downto 0);
    ps_bus_rdata     : out std_logic_vector(31 downto 0)
  );
end entity;

architecture rtl of core_storage is
  function owns(i : natural) return boolean is
  begin
    return i = CSR_WIN_MEM or i = CSR_WIN_LA_DEEP or i = CSR_WIN_EVENTS;
  end function;

  function deep_lanes_value return positive is
  begin
    if USE_DEEP_LANES then
      return LA_LANES;
    end if;
    return 1;
  end function;
  constant DEEP_LANES : positive := deep_lanes_value;

  -- 外部メモリ
  signal mem_ready_s, mem_busy, mem_done, mem_test_start, mem_in_reset, mem_tick : std_logic := '0';
  signal mem_base, mem_count, mem_seed : std_logic_vector(31 downto 0);
  signal mem_errors, mem_first, mem_wc, mem_rc : std_logic_vector(31 downto 0) := (others => '0');
  -- ロジアナの深いバッファ
  signal deep_run, deep_rle, deep_read, deep_capturing, deep_triggered_i, deep_done, deep_overrun : std_logic := '0';
  signal deep_stream : std_logic := '0';
  signal deep_ps_read : std_logic := '0';
  signal deep_pack : std_logic_vector(2 downto 0) := "000";
  signal deep_step : std_logic_vector(1 downto 0) := "00";
  signal deep_first_ch : std_logic_vector(3 downto 0) := "0000";
  signal deep_gaps, deep_gap_open : std_logic := '0';
  signal deep_rle_fmt : std_logic := '0';
  signal deep_rle_repeat : std_logic := '0';
  signal deep_tick, deep_pad_bytes, deep_keyframes : std_logic_vector(31 downto 0) := (others => '0');
  signal deep_gap_count, deep_gap_last : std_logic_vector(31 downto 0) := (others => '0');
  -- 深いバッファのリングの使用量・起点の時刻・符号器の形（la_deep.la_written・ring_*・start_time・info）
  signal deep_la_written, deep_unread, deep_peak : std_logic_vector(31 downto 0) := (others => '0');
  signal deep_start_time : unsigned(63 downto 0) := (others => '0');
  signal deep_start_valid : std_logic := '0';
  signal deep_latency_i : unsigned(7 downto 0) := (others => '0');
  signal deep_read_ps : std_logic := '0';
  signal deep_stream_beats : std_logic_vector(31 downto 0) := (others => '0');
  signal deep_reading : std_logic := '0';
  signal deep_slot : std_logic_vector(2 downto 0) := (others => '0');
  signal deep_post, deep_first, deep_count : std_logic_vector(31 downto 0);
  signal deep_beat, deep_written : std_logic_vector(31 downto 0) := (others => '0');
  signal deep_time : unsigned(63 downto 0) := (others => '0');
  signal deep_region : std_logic := '0';
  signal deep_ring_beats : std_logic_vector(31 downto 0) := (others => '0');
  -- イベント記録
  signal events_enable : std_logic := '0';
  signal events_written : std_logic_vector(63 downto 0) := (others => '0');            -- 書いた数（64 ビット）
  signal events_written_hi : std_logic_vector(31 downto 0) := (others => '0');         -- 上位。written を読んだ時点の値を保持
  signal events_lost, events_capacity : std_logic_vector(31 downto 0) := (others => '0');
  signal event_valid : std_logic := '0';
  signal event_data : std_logic_vector(127 downto 0) := (others => '0');
  signal byte_lost : unsigned(31 downto 0) := (others => '0');
  signal events_rsp : csr_rsp_t;
begin
  idle_g : for i in CSR_WINDOWS'range generate
    other_g : if not owns(i) generate
      csr_rsp(i) <= CSR_RSP_IDLE;
    end generate;
  end generate;

  deep_triggered <= deep_triggered_i;
  csr_rsp(CSR_WIN_EVENTS) <= events_rsp;
  deep_latency   <= deep_latency_i;

  -- 外部メモリ: 状態とメモリ試験
  mem_csr_i : entity work.csr_mem
    port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_MEM),
              bus_cmd => csr_cmd, bus_rsp => csr_rsp(CSR_WIN_MEM),
              status_ready => mem_ready_s, status_busy => mem_busy, status_done => mem_done,
              status_in_reset => mem_in_reset, status_clock_tick => mem_tick,
              command_test => mem_test_start, test_base => mem_base, test_count => mem_count,
              test_seed => mem_seed, errors => mem_errors, first_error => mem_first,
              write_cycles => mem_wc, read_cycles => mem_rc);

  deep_csr_i : entity work.csr_la_deep
    port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_LA_DEEP),
              bus_cmd => csr_cmd, bus_rsp => csr_rsp(CSR_WIN_LA_DEEP),
              control_run => deep_run, control_rle => deep_rle, control_stream => deep_stream,
              control_ps_read => deep_ps_read, control_pack => deep_pack, control_step => deep_step,
              control_first => deep_first_ch, control_gaps => deep_gaps, control_rle_fmt => deep_rle_fmt, control_rle_repeat => deep_rle_repeat,
              tick => deep_tick, pad_bytes => deep_pad_bytes, keyframes => deep_keyframes,
              post_beats => deep_post, stream_beats => deep_stream_beats,
              status_capturing => deep_capturing, status_triggered => deep_triggered_i, status_done => deep_done,
              status_overrun => deep_overrun, status_reading => deep_reading, status_gap_open => deep_gap_open,
              status_trigger_slot => deep_slot,
              trigger_beat => deep_beat, written => deep_written,
              trigger_time_lo => std_logic_vector(deep_time(31 downto 0)),
              trigger_time_hi => std_logic_vector(deep_time(63 downto 32)),
              read_first => deep_first, read_count => deep_count, command_read => deep_read, command_read_ps => deep_read_ps,
              read_region => deep_region, ring_beats => deep_ring_beats,
              la_written => deep_la_written, ring_unread => deep_unread, ring_peak => deep_peak,
              start_time_lo => std_logic_vector(deep_start_time(31 downto 0)),
              start_time_hi => std_logic_vector(deep_start_time(63 downto 32)),
              info_lanes => std_logic_vector(to_unsigned(DEEP_LANES, 4)), info_latency => std_logic_vector(deep_latency_i),
              info_start_valid => deep_start_valid, gap_last_beat => deep_gap_last, gaps => deep_gap_count);

  events_csr_i : entity work.csr_events
    port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_EVENTS), bus_cmd => csr_cmd,
              bus_rsp => events_rsp,
              control_enable => events_enable, written => events_written(31 downto 0), lost => events_lost,
              capacity => events_capacity, written_hi => events_written_hi);

  -- written_hi は written（下位）を読んだ周期の上位を保持する。written を読む直後（同じ連続読み出しの中）に written_hi を読めば、
  -- 下位と上位は同じ時点の値で、桁上がりをまたいで化けない（二つのワードを別の時点に読むと、2**32 の境で下位だけ 0 に戻る）。
  -- 保持は 1 つの連続読み出しのあいだ他の読み手に割り込まれない前提（CSR の読みは 1 要求ずつ順に処理される）。
  -- 窓への要求の間、csr_cmd.addr・csr_cmd.we はマスタの値と同じ（csr_decoder の写し）
  process (clk)
  begin
    if rising_edge(clk) then
      if csr_req(CSR_WIN_EVENTS) = '1' and events_rsp.ack = '0' and csr_cmd.we = '0'
         and to_integer(csr_cmd.addr(3 downto 0)) = 1 then
        events_written_hi <= events_written(63 downto 32);
      end if;
      if rst(RST_BUS) = '1' then
        events_written_hi <= (others => '0');
      end if;
    end if;
  end process;

  -- イベント記録の集約（形式は event_collector.vhd の先頭と csr_events の仕様）
  collector_i : entity work.event_collector
    generic map (BYTE_PACK => EVENT_BYTE_PACK, CONFIG_EVENTS => true)
    port map (clk => clk, rst => rst(RST_STORAGE), time_count => time_count(47 downto 0), events_enable => events_enable,
              ev_valid => xfer_event.valid, host_kind => xfer_event.kind, ev_parity => xfer_event.parity,
              ev_request => xfer_event.request, ev_ack => xfer_event.ack, ev_data => xfer_event.data,
              ev_addr => xfer_event.addr, ev_time => xfer_event.time,
              inj_written => inject_event.written, inj_transfers => inject_event.transfers,
              inj_value => inject_event.value, inj_addr => inject_event.addr, inj_time => inject_event.time,
              smp_valid => sampler_event.valid, smp_kind => sampler_event.kind, smp_word => sampler_event.word,
              uart_rx_fire => uart_rx_event.fire, uart_rx_data => uart_rx_event.data,
              uart_rx_time => uart_rx_event.time,
              uart_tx_fire => uart_tx_event.fire, uart_tx_data => uart_tx_event.data,
              swo_fire => swo_event.fire, swo_data => swo_event.data, swo_time => swo_event.time,
              aux_valid => aux_event.valid, aux_ch => aux_event.ch, aux_value => aux_event.value,
              aux_src_time => aux_event.time,
              obs_valid => obs_event.valid, obs_sync => obs_event.sync, obs_data => obs_event.data,
              obs_addr => obs_event.addr, obs_time => obs_event.time,
              sync_valid => sync_event.valid, sync_local => sync_event.local, sync_parent => sync_event.parent,
              cfg_word => cfg_word,
              event_valid => event_valid, event_data => event_data, byte_lost => byte_lost);

  memory : if MEM_ADDR_BITS > 0 generate
    signal first : unsigned(MEM_ADDR_BITS - 1 downto 0);
    signal errors, wc, rc : unsigned(31 downto 0);
    signal t_valid, t_write : std_logic;
    signal t_addr : unsigned(MEM_ADDR_BITS - 1 downto 0);
    signal t_data : std_logic_vector(127 downto 0);
  begin
    -- 状態（初期化・較正の完了など）は常に。試験回路は MEM_TEST のときだけ（mem_port の使い手は試験回路か
    -- 深いバッファのどちらか一方）
    test_i : entity work.mem_test_sys
      generic map (ADDR_BITS => MEM_ADDR_BITS, DATA_BITS => 128, TESTER => MEM_TEST)
      port map (clk => clk, rst => rst(RST_STORAGE), start => mem_test_start,
                base => unsigned(mem_base(MEM_ADDR_BITS - 1 downto 0)),
                count => unsigned(mem_count(MEM_ADDR_BITS downto 0)), seed => mem_seed,
                ready => mem_ready_s, in_reset => mem_in_reset, clock_tick => mem_tick,
                busy => mem_busy, done => mem_done, errors => errors,
                first_error => first, write_cycles => wc, read_cycles => rc,
                mem_clk => mem_clk, mem_rst => mem_rst, mem_ready => mem_ready,
                cmd_valid => t_valid, cmd_ready => mem_cmd_ready, cmd_write => t_write,
                cmd_addr => t_addr, cmd_data => t_data, rd_valid => mem_rd_valid, rd_data => mem_rd_data);
    mem_errors <= std_logic_vector(errors);
    mem_first  <= std_logic_vector(resize(first, 32));
    mem_wc     <= std_logic_vector(wc);
    mem_rc     <= std_logic_vector(rc);

    tester_port : if MEM_TEST generate
      mem_cmd_valid <= t_valid;
      mem_cmd_write <= t_write;
      mem_cmd_addr  <= t_addr;
      mem_cmd_data  <= t_data;
      deep_capturing <= '0'; deep_triggered_i <= '0'; deep_done <= '0'; deep_overrun <= '0'; deep_reading <= '0';
      deep_time <= (others => '0'); deep_beat <= (others => '0'); deep_slot <= (others => '0');
      deep_written <= (others => '0'); deep_ring_beats <= (others => '0');
      events_written <= (others => '0'); events_lost <= (others => '0'); events_capacity <= (others => '0');
      deep_stream_beats <= (others => '0');
      deep_la_written <= (others => '0'); deep_unread <= (others => '0'); deep_peak <= (others => '0');
      deep_start_time <= (others => '0'); deep_start_valid <= '0'; deep_latency_i <= (others => '0');
      deep_gap_open <= '0'; deep_gap_count <= (others => '0'); deep_gap_last <= (others => '0'); deep_pad_bytes <= (others => '0'); deep_keyframes <= (others => '0');
      ps_bus_rdata <= (others => '0');
      stream_out_valid <= '0';
      stream_out_data  <= (others => '0');
    end generate;

    deep : if not MEM_TEST generate
      signal beat : unsigned(MEM_ADDR_BITS - 1 downto 0);
      signal slot : unsigned(2 downto 0);
      signal written, la_beats, ev_beats : unsigned(MEM_ADDR_BITS downto 0);
      signal ev_written : unsigned(63 downto 0);
      signal ev_lost, stream_beats_u : unsigned(31 downto 0);
      -- la_deep_buffer ⇄ la_ps_port（PS が DDR を直接読む流れの位置の約束）
      signal ps_epoch_u, ps_pub_epoch_u, ps_ret_epoch_u : unsigned(15 downto 0);
      signal ps_active_u, ps_writing_u, ps_overrun_u, ps_rd_region_u : std_logic;
      signal ps_la_written_u, ps_la_consumed_u, ps_ev_consumed_u, ps_out_beats_u : unsigned(31 downto 0);
      signal ps_la_wptr_u : unsigned(MEM_ADDR_BITS - 1 downto 0);
      signal ps_ev_wpos_u, ps_ev_start_u : unsigned(MEM_ADDR_BITS - 5 downto 0);
      signal ps_rd_epoch_u, ps_rd_ack_u : unsigned(7 downto 0);
      signal ps_rd_first_u : unsigned(MEM_ADDR_BITS - 1 downto 0);
      signal ps_rd_count_u : unsigned(MEM_ADDR_BITS downto 0);
      signal stat_written_u, stat_unread_u, stat_peak_u : unsigned(31 downto 0);
      signal gap_count_u, gap_last_u, tok_pad_u, tok_kfs_u : unsigned(31 downto 0);
    begin
    deep_i : entity work.la_deep_buffer
      generic map (WIDTH => LA_WIDTH, ADDR_BITS => MEM_ADDR_BITS, STREAM_EVENTS => LA_STREAM_EVENTS,
                   STREAM_GAPS => LA_STREAM_GAPS, RLE_TOKENS => LA_RLE_TOKENS, TOK_REPEAT_EN => LA_RLE_REPEAT,
                   COUNT_START_BELOW_4G => EVENT_COUNT_START_BELOW_4G, LANES => DEEP_LANES,
                   IN_AGE => LA_LANE_IN_AGE * boolean'pos(USE_DEEP_LANES), EXT_DELAY => EXT_DELAY,
                   SAME_CLOCK => MEM_SAME_CLOCK)
      port map (clk => clk, rst => rst(RST_STORAGE), probes => la_probes, ext_trigger => la_ext, time => time_count,
                lane_data => la_lane_data(DEEP_LANES * LA_WIDTH - 1 downto 0), step => unsigned(deep_step),
                first_ch => unsigned(deep_first_ch),
                rate => unsigned(trig_cfg.rate), pretrigger => unsigned(trig_cfg.pretrigger),
                trig_mask => trig_cfg.mask, trig_value => trig_cfg.value, trig_rise => trig_cfg.rise,
                trig_fall => trig_cfg.fall, pw_enable => pw_enable, pw_event => pw_event,
                run => deep_run, rle => deep_rle, rle_fmt => deep_rle_fmt, rle_repeat => deep_rle_repeat, tick => unsigned(deep_tick), pack => deep_pack, stream => deep_stream, gaps => deep_gaps,
                ps_read => deep_ps_read, ps_epoch => ps_epoch_u, ps_ret_epoch => ps_ret_epoch_u,
                ps_la_consumed => ps_la_consumed_u, ps_out_beats => ps_out_beats_u,
                ps_ev_consumed => ps_ev_consumed_u(MEM_ADDR_BITS - 5 downto 0),
                ps_pub_epoch => ps_pub_epoch_u, ps_active => ps_active_u, ps_writing => ps_writing_u,
                ps_overrun => ps_overrun_u, ps_la_written => ps_la_written_u, ps_la_wptr => ps_la_wptr_u,
                ps_ev_wpos => ps_ev_wpos_u, ps_ev_start => ps_ev_start_u, wr_pending => mem_wr_pending,
                post_beats => unsigned(deep_post(MEM_ADDR_BITS - 1 downto 0)),
                capturing => deep_capturing, triggered => deep_triggered_i, done => deep_done, overrun => deep_overrun,
                trigger_beat => beat, trigger_slot => slot, written => written, trigger_time => deep_time,
                stream_beats => stream_beats_u,
                stat_written => stat_written_u, stat_unread => stat_unread_u, stat_peak => stat_peak_u,
                start_time => deep_start_time, start_valid => deep_start_valid, latency => deep_latency_i,
                gap_count => gap_count_u, gap_last_beat => gap_last_u, gap_open => deep_gap_open,
                tok_pad_bytes => tok_pad_u, tok_keyframes => tok_kfs_u,
                ev_enable => events_enable, ev_valid => event_valid, ev_data => event_data,
                ev_written => ev_written, ev_lost => ev_lost, la_beats => la_beats, ev_beats => ev_beats,
                read => deep_read, read_region => deep_region,
                read_ps => deep_read_ps, ps_rd_ack => ps_rd_ack_u, ps_rd_epoch => ps_rd_epoch_u,
                ps_rd_region => ps_rd_region_u, ps_rd_first => ps_rd_first_u, ps_rd_count => ps_rd_count_u,
                read_first => unsigned(deep_first(MEM_ADDR_BITS - 1 downto 0)),
                read_count => unsigned(deep_count(MEM_ADDR_BITS downto 0)), reading => deep_reading,
                out_valid => stream_out_valid, out_ready => stream_out_ready, out_data => stream_out_data,
                mem_clk => mem_clk, mem_rst => mem_rst, mem_active => open,
                cmd_valid => mem_cmd_valid, cmd_ready => mem_cmd_ready, cmd_write => mem_cmd_write,
                cmd_addr => mem_cmd_addr, cmd_data => mem_cmd_data, rd_valid => mem_rd_valid, rd_data => mem_rd_data);
    deep_beat    <= std_logic_vector(resize(beat, 32));
    deep_slot    <= std_logic_vector(slot);
    deep_written <= std_logic_vector(resize(written, 32));
    deep_ring_beats <= std_logic_vector(resize(la_beats, 32));
    events_written  <= std_logic_vector(ev_written);
    events_lost     <= std_logic_vector(sat_add(ev_lost, byte_lost));   -- 和を上限で止める（折り返して小さく見せない。C9）
    events_capacity <= std_logic_vector(resize(ev_beats, 32));
    deep_stream_beats <= std_logic_vector(stream_beats_u);
    deep_la_written   <= std_logic_vector(stat_written_u);
    deep_unread       <= std_logic_vector(stat_unread_u);
    deep_peak         <= std_logic_vector(stat_peak_u);
    deep_gap_count    <= std_logic_vector(gap_count_u);
    deep_gap_last     <= std_logic_vector(gap_last_u);
    deep_pad_bytes    <= std_logic_vector(tok_pad_u);
    deep_keyframes    <= std_logic_vector(tok_kfs_u);
    -- PS が DDR を直接読む流れの窓の側のレジスタ（位置の約束を守らせる）
    ps_port_i : entity work.la_ps_port
      port map (clk => clk, rst => rst(RST_STORAGE), bus_rd => ps_bus_rd, bus_wr => ps_bus_wr, bus_addr => ps_bus_addr,
                bus_wdata => ps_bus_wdata, bus_rdata => ps_bus_rdata, epoch => ps_epoch_u, pub_epoch => ps_pub_epoch_u,
                pub_active => ps_active_u, pub_writing => ps_writing_u, pub_overrun => ps_overrun_u,
                pub_la_written => ps_la_written_u, pub_la_wptr => resize(ps_la_wptr_u, 32),
                pub_ev_wpos => resize(ps_ev_wpos_u, 32), pub_ev_start => resize(ps_ev_start_u, 32),
                rd_epoch => ps_rd_epoch_u, rd_region => ps_rd_region_u, rd_first => resize(ps_rd_first_u, 32),
                rd_count => resize(ps_rd_count_u, 32), ret_epoch => ps_ret_epoch_u, la_consumed => ps_la_consumed_u,
                ev_consumed => ps_ev_consumed_u, out_beats => ps_out_beats_u, rd_ack => ps_rd_ack_u);
    end generate;
  end generate;

  -- 外部メモリが無い: 状態・深いバッファ・イベント記録の値も明示的に 0（CSR から読まれる。駆動されない配線は
  -- 実機で値が定まらない）
  no_memory : if MEM_ADDR_BITS = 0 generate
    mem_ready_s <= '0'; mem_busy <= '0'; mem_done <= '0'; mem_in_reset <= '0'; mem_tick <= '0';
    mem_errors <= (others => '0'); mem_first <= (others => '0'); mem_wc <= (others => '0'); mem_rc <= (others => '0');
    deep_capturing <= '0'; deep_triggered_i <= '0'; deep_done <= '0'; deep_overrun <= '0';
    deep_time <= (others => '0'); deep_beat <= (others => '0'); deep_slot <= (others => '0');
    deep_written <= (others => '0'); deep_ring_beats <= (others => '0'); deep_stream_beats <= (others => '0');
    deep_la_written <= (others => '0'); deep_unread <= (others => '0'); deep_peak <= (others => '0');
    deep_start_time <= (others => '0'); deep_start_valid <= '0'; deep_latency_i <= (others => '0');
    deep_gap_open <= '0'; deep_gap_count <= (others => '0'); deep_gap_last <= (others => '0'); deep_pad_bytes <= (others => '0'); deep_keyframes <= (others => '0');
    ps_bus_rdata <= (others => '0');
    mem_cmd_valid <= '0';
    mem_cmd_write <= '0';
    mem_cmd_addr  <= (others => '0');
    mem_cmd_data  <= (others => '0');

    no_events : if EVENT_RING_BITS = 0 generate
      deep_reading <= '0';
      events_written <= (others => '0'); events_lost <= (others => '0'); events_capacity <= (others => '0');
      stream_out_valid <= '0';
      stream_out_data  <= (others => '0');
    end generate;

    -- イベント記録を内蔵 RAM のリングに（外部メモリの深いバッファと同じ CSR・読み出し・ストリーム。
    -- docs/superpowers/specs/2026-10-20-bram-event-ring-design.md）
    ring_events : if EVENT_RING_BITS > 0 generate
      signal ev_written : unsigned(63 downto 0);
      signal ev_lost, ev_beats : unsigned(31 downto 0);
    begin
      ring_i : entity work.event_ring
        generic map (DEPTH_BITS => EVENT_RING_BITS, ADDR_BITS => EVENT_RING_BITS,
                     COUNT_START_BELOW_4G => EVENT_COUNT_START_BELOW_4G)
        port map (clk => clk, rst => rst(RST_STORAGE), ev_enable => events_enable, ev_valid => event_valid, ev_data => event_data,
                  ev_written => ev_written, ev_lost => ev_lost, ev_beats => ev_beats,
                  read => deep_read, read_region => deep_region,
                  read_first => unsigned(deep_first(EVENT_RING_BITS - 1 downto 0)),
                  read_count => unsigned(deep_count(EVENT_RING_BITS downto 0)), reading => deep_reading,
                  out_valid => stream_out_valid, out_ready => stream_out_ready, out_data => stream_out_data);
      events_written  <= std_logic_vector(ev_written);
      events_lost     <= std_logic_vector(sat_add(ev_lost, byte_lost));
      events_capacity <= std_logic_vector(ev_beats);
    end generate;
  end generate;
end architecture;
