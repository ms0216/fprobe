-- fprobe_core のデバッガの部分（sys ドメイン）: CMSIS-DAP の処理部から線までと、線を借りる回路。
--   DAP コマンド → dap_processor → swd_injector → dap_sampler → debug_port（swd_phy / jtag_phy）→ SWCLK/TCK、SWDIO/TMS、TDI、nRESET
--                ← 応答            ↑ dap_monitor（ホストの転送を覗く: SELECT の控え、番地一致）
-- CSR の窓: inject・debug・sampler・sampler_match。割り込み書き込みの要求と語 0・番地一致の条件はクロストリガ（core_xtrig）から。
-- fprobe_core の中だけで使う（試験は fprobe_core の試験台から）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.event_pkg.all;
use work.csr_bus_pkg.all;
use work.csr_map_pkg.all;
use work.core_pkg.all;

entity core_debug is
  generic (
    CLK_HZ             : positive;
    HAS_JTAG           : boolean;
    HAS_SWO_MANCHESTER : boolean;
    XTRIG              : boolean;
    INJECTOR           : boolean;
    SAMPLER            : boolean;
    SAMPLER_MATCH      : boolean;
    SAMPLER_CHECK_MAX  : positive range 1 to 64
  );
  port (
    clk            : in  std_logic;
    rst            : in  core_resets_t;
    time_count     : in  unsigned(63 downto 0);
    -- CSR（csr_decoder の窓の側。この部分の窓以外は応答しない）
    csr_cmd        : in  csr_cmd_t;
    csr_req        : in  std_logic_vector(CSR_WINDOWS'range);
    csr_rsp        : out csr_rsp_array_t(CSR_WINDOWS'range);
    -- CMSIS-DAP
    packet_size    : in  natural range 64 to 512;
    dap_in_valid   : in  std_logic;
    dap_in_ready   : out std_logic;
    dap_in_data    : in  std_logic_vector(7 downto 0);
    dap_in_last    : in  std_logic;
    dap_out_valid  : out std_logic;
    dap_out_ready  : in  std_logic;
    dap_out_data   : out std_logic_vector(7 downto 0);
    dap_out_last   : out std_logic;
    -- デバッグポートのピン（fprobe_core のポートと同じ）
    swclk_o        : out std_logic;
    swclk_oe       : out std_logic;
    swdio_o        : out std_logic;
    swdio_oe       : out std_logic;
    swdio_o_lead   : out std_logic;
    swdio_i        : in  std_logic;
    tdi_o          : out std_logic;
    tdi_oe         : out std_logic;
    tdo_i          : in  std_logic;
    nreset_low     : out std_logic;
    nreset_i       : in  std_logic;
    -- SWO の受信（core_periph の swo_receiver。設定は DAP の SWO コマンド）
    swo_run        : out std_logic;
    swo_divisor    : out unsigned(15 downto 0);
    swo_manchester : out std_logic;
    swo_error      : in  std_logic;
    swo_overrun    : in  std_logic;
    -- 共有の割り算器（DAP 側の口）
    div_req        : out std_logic;
    div_num        : out unsigned(31 downto 0);
    div_den        : out unsigned(31 downto 0);
    div_done       : in  std_logic;
    div_quot       : in  unsigned(31 downto 0);
    -- クロストリガ（core_xtrig）
    inj_trigger    : in  std_logic;                  -- 源が割り込み書き込みへ発火した周期
    inj_request    : in  std_logic;                  -- その 1 周期後（swd_injector の request）
    inject_cfg     : in  inject_cfg_t;
    match_cfg      : in  match_cfg_t;
    inject_status  : out inject_status_t;
    match          : out std_logic;                  -- 番地一致（クロストリガの源 bit1）
    match_time     : out unsigned(63 downto 0);      -- match の周期: その転送の線上の点
    smp_match      : out std_logic;                  -- サンプラの値の一致（クロストリガの源 bit5。R21）
    -- ロジアナの内部の信号
    dap_busy       : out std_logic;
    -- イベント記録の源
    xfer_event     : out xfer_event_t;
    inject_event   : out inject_event_t;
    sampler_event  : out sampler_event_t;
    cfg_word       : out std_logic_vector(71 downto 0)   -- PHY の設定（イベント 16 の [119:48]。time-accuracy R7）
  );
end entity;

architecture rtl of core_debug is
  function owns(i : natural) return boolean is
  begin
    return i = CSR_WIN_INJECT or i = CSR_WIN_DEBUG or i = CSR_WIN_SAMPLER or i = CSR_WIN_SAMPLER_MATCH;
  end function;

  signal wait_retry : unsigned(15 downto 0);                -- DAP_TransferConfigure の WAIT の再試行（サンプラ・割り込み書き込みも）
  signal swd_enable, swd_data_phase, swd_cmd_valid, swd_cmd_ready, swd_rsp_valid, swd_rsp_parity : std_logic;
  signal swd_half_period : unsigned(15 downto 0);
  signal swd_turnaround  : natural range 1 to 4;
  signal swd_idle_cycles : natural range 0 to 255;
  signal swd_sample_delay : std_logic_vector(2 downto 0);
  signal swd_cmd_cond, phy_cmd_cond, port_cmd_cond : std_logic;   -- 前の転送が OK のときだけ受ける（dap_link の先出し）
  signal jtag_sample_delay : std_logic_vector(2 downto 0);
  signal swd_cmd_op      : std_logic_vector(1 downto 0);
  signal swd_cmd_count   : natural range 1 to 64;
  signal swd_cmd_request : std_logic_vector(3 downto 0);
  signal swd_cmd_data, swd_rsp_data : std_logic_vector(63 downto 0);
  signal swd_rsp_time    : unsigned(63 downto 0);          -- 応答の転送の線上の点（debug_port。time-accuracy R5）
  signal swd_rsp_ack     : std_logic_vector(2 downto 0);
  signal dap_busy_i, swdio_oe_i : std_logic;
  signal jtag : std_logic;
  signal jtag_dr_before, jtag_dr_after : natural range 0 to 7;
  signal jtag_ir_before, jtag_ir_after : natural range 0 to 63;
  signal jtag_ir_len : natural range 1 to 32;
  -- 割り込み・モニタ
  signal phy_cmd_valid, phy_cmd_ready, dap_rsp_valid, dap_fire : std_logic;
  signal phy_cmd_op : std_logic_vector(1 downto 0);
  signal phy_cmd_count : natural range 1 to 64;
  signal phy_cmd_request : std_logic_vector(3 downto 0);
  signal phy_cmd_data : std_logic_vector(63 downto 0);
  signal select_shadow : std_logic_vector(31 downto 0);
  signal csw_shadow, tar_shadow : std_logic_vector(31 downto 0);
  signal shadow_valid : std_logic;
  signal shadow_ap : std_logic_vector(7 downto 0);
  -- DAP サンプラ（swd_injector と debug_port の間）
  signal port_cmd_valid, port_cmd_ready, up_rsp_valid, up_busy : std_logic;
  signal port_cmd_op : std_logic_vector(1 downto 0);
  signal port_cmd_count : natural range 1 to 64;
  signal port_cmd_request : std_logic_vector(3 downto 0);
  signal port_cmd_data : std_logic_vector(63 downto 0);
  signal smp_valid, smp_busy, smp_run : std_logic := '0';
  signal smp_held, smp_handed : std_logic;                  -- サンプラが線を持っている・直接渡した線の SELECT・CSW が分かっている（swd_injector へ）
  signal smp_addr, smp_data : std_logic_vector(31 downto 0) := (others => '0');
  signal smp_ack, smp_last_ack : std_logic_vector(2 downto 0) := (others => '0');
  signal smp_index : unsigned(6 downto 0) := (others => '0');     -- 標本の番号（か所 × 16 + 語）
  signal smp_time : unsigned(47 downto 0) := (others => '0');     -- 標本の時刻（読みに行った転送の線上の点。time-accuracy R6）
  signal smp_rec_valid : std_logic := '0';                        -- サンプラのイベントの件（種類 7・17・18）
  signal smp_rec_kind : std_logic_vector(7 downto 0) := (others => '0');
  signal smp_rec_word : std_logic_vector(119 downto 0) := (others => '0');
  signal inj_busy, inj_written, inj_claim : std_logic;
  signal inj_held : std_logic := '0';                  -- 割り込み書き込みが PHY を借りている（サンプラの直接の受け渡し）
  signal inj_result : std_logic_vector(1 downto 0);
  signal inj_ack : std_logic_vector(2 downto 0);
  signal inj_count : unsigned(15 downto 0);
  signal inj_transfers : unsigned(7 downto 0);         -- 停止の書き込みまでの転送数（イベント 11。R20）
  signal inj_words : unsigned(3 downto 0);             -- 最後の要求で受理された語の数
  signal inj_w_addr, inj_w_value : std_logic_vector(31 downto 0);   -- 書いた語（イベント 11）
  signal inj_w_time : unsigned(63 downto 0);                        -- その語の DRW の線上の点
  signal inject_time, inject_first_time : unsigned(63 downto 0) := (others => '0');
  signal ev_valid, ev_parity, ev_addr_known : std_logic;
  signal ev_request : std_logic_vector(3 downto 0);
  signal ev_ack : std_logic_vector(2 downto 0);
  signal ev_addr, ev_data : std_logic_vector(31 downto 0);
  signal ev_time : unsigned(63 downto 0);                  -- その転送の線上の点（dap_monitor）
  signal host_kind : std_logic_vector(7 downto 0);         -- ホストの転送のイベントの種類（1 か 12）
  -- 入力のレジスタスライス（ホストリンク側の FIFO と処理部の組み合わせ回路を切り離す）
  signal dap_s_valid, dap_s_ready : std_logic;
  signal dap_s : std_logic_vector(8 downto 0);
  -- 始動の列（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md §3.3）: 成功で値の一致の門を開ける
  signal inj_kind_start, inj_done : std_logic;
  signal start_ok : std_logic := '0';                  -- 始動の列が成功で終わった周期
  signal start_first_time, start_last_time : unsigned(63 downto 0) := (others => '0');
begin
  idle_g : for i in CSR_WINDOWS'range generate
    other_g : if not owns(i) generate
      csr_rsp(i) <= CSR_RSP_IDLE;
    end generate;
  end generate;

  dap_slice_i : entity work.stream_slice
    generic map (WIDTH => 9)
    port map (clk => clk, rst => rst(RST_DEBUG), in_valid => dap_in_valid, in_ready => dap_in_ready,
              in_data => dap_in_last & dap_in_data, out_valid => dap_s_valid, out_ready => dap_s_ready,
              out_data => dap_s);

  dap_i : entity work.dap_processor
    generic map (CLK_HZ => CLK_HZ, HAS_JTAG => HAS_JTAG, HAS_SWO_MANCHESTER => HAS_SWO_MANCHESTER,
                 SHARED_DIVIDER => true)
    port map (clk => clk, rst => rst(RST_DEBUG), packet_size => packet_size,
              in_valid => dap_s_valid, in_ready => dap_s_ready, in_data => dap_s(7 downto 0),
              in_last => dap_s(8), out_valid => dap_out_valid, out_ready => dap_out_ready,
              out_data => dap_out_data, out_last => dap_out_last,
              swd_enable => swd_enable, swd_half_period => swd_half_period,
              swd_turnaround => swd_turnaround, swd_data_phase => swd_data_phase,
              swd_idle_cycles => swd_idle_cycles, swd_cmd_valid => swd_cmd_valid,
              swd_cmd_ready => swd_cmd_ready, swd_cmd_cond => swd_cmd_cond, swd_cmd_op => swd_cmd_op,
              swd_cmd_count => swd_cmd_count,
              swd_cmd_request => swd_cmd_request, swd_cmd_data => swd_cmd_data,
              swd_rsp_valid => dap_rsp_valid, swd_rsp_ack => swd_rsp_ack, swd_rsp_data => swd_rsp_data,
              swd_rsp_parity => swd_rsp_parity, swd_rsp_time => swd_rsp_time, nreset_low => nreset_low, nreset_in => nreset_i,
              wait_retry_out => wait_retry,
              jtag => jtag, jtag_dr_before => jtag_dr_before, jtag_dr_after => jtag_dr_after,
              jtag_ir_before => jtag_ir_before, jtag_ir_after => jtag_ir_after, jtag_ir_len => jtag_ir_len,
              swo_run => swo_run, swo_divisor => swo_divisor, swo_manchester => swo_manchester,
              swo_error => swo_error, swo_overrun => swo_overrun, busy => dap_busy_i,
              div_req => div_req, div_num => div_num, div_den => div_den,
              div_done => div_done, div_quot => div_quot);
  dap_busy <= dap_busy_i;

  xtrig_inject : if XTRIG and INJECTOR generate
    signal extra_f, index_f : std_logic_vector(2 downto 0);
    signal tbl_addr, tbl_value : std_logic_vector(31 downto 0);
    signal tbl_write : std_logic;
    signal st_first, st_last : std_logic_vector(2 downto 0);
    signal st_go, st_pending : std_logic;
    signal st_result : std_logic_vector(1 downto 0) := "00";
    signal st_words : unsigned(3 downto 0) := (others => '0');
    signal st_count : unsigned(15 downto 0) := (others => '0');
  begin
  inject_i : entity work.swd_injector
    port map (clk => clk, rst => rst(RST_DEBUG), wait_retry => wait_retry, request => inj_request, enable => swd_enable, jtag => jtag, dap_busy => dap_busy_i,
              select_shadow => select_shadow, csw_shadow => csw_shadow, tar_shadow => tar_shadow,
              shadow_valid => shadow_valid, shadow_ap => shadow_ap, ap => inject_cfg.ap, addr => inject_cfg.addr,
              value => inject_cfg.value,
              busy => inj_busy, held => inj_held, written => inj_written, result => inj_result, last_ack => inj_ack,
              count => inj_count, transfers => inj_transfers, written_words => inj_words,
              written_addr => inj_w_addr, written_value => inj_w_value, written_time => inj_w_time,
              extra => to_integer(unsigned(extra_f)), tbl_write => tbl_write, tbl_index => to_integer(unsigned(index_f)),
              tbl_addr => tbl_addr, tbl_value => tbl_value,
              line_held => smp_held, line_handed => smp_handed,
              start_request => st_go, start_first => to_integer(unsigned(st_first)),
              start_last => to_integer(unsigned(st_last)), start_pending => st_pending, kind_start => inj_kind_start,
              done => inj_done,
              dap_cmd_valid => swd_cmd_valid, dap_cmd_ready => swd_cmd_ready, dap_cmd_cond => swd_cmd_cond,
              dap_cmd_op => swd_cmd_op,
              dap_cmd_count => swd_cmd_count, dap_cmd_req => swd_cmd_request, dap_cmd_data => swd_cmd_data,
              dap_rsp_valid => dap_rsp_valid,
              phy_cmd_valid => phy_cmd_valid, phy_cmd_ready => phy_cmd_ready, phy_cmd_cond => phy_cmd_cond,
              phy_cmd_op => phy_cmd_op,
              phy_cmd_count => phy_cmd_count, phy_cmd_req => phy_cmd_request, phy_cmd_data => phy_cmd_data,
              phy_rsp_valid => up_rsp_valid, phy_rsp_ack => swd_rsp_ack,
              phy_rsp_data => swd_rsp_data(31 downto 0), phy_rsp_par => swd_rsp_parity, phy_rsp_time => swd_rsp_time);
  inject_csr_i : entity work.csr_inject
    port map (clk => clk, rst => rst(RST_LA), bus_req => csr_req(CSR_WIN_INJECT),
              bus_cmd => csr_cmd, bus_rsp => csr_rsp(CSR_WIN_INJECT),
              control_extra => extra_f, entry => index_f, addr => tbl_addr, value => tbl_value, entry_write => tbl_write,
              status_written_words => std_logic_vector(inj_words),
              first_time_lo => std_logic_vector(inject_first_time(31 downto 0)),
              first_time_hi => std_logic_vector(inject_first_time(63 downto 32)),
              start_first => st_first, start_last => st_last, start_go => st_go,
              start_status_pending => st_pending, start_status_result => st_result,
              start_status_written_words => std_logic_vector(st_words), start_status_count => std_logic_vector(st_count),
              start_first_time_lo => std_logic_vector(start_first_time(31 downto 0)),
              start_first_time_hi => std_logic_vector(start_first_time(63 downto 32)),
              start_last_time_lo => std_logic_vector(start_last_time(31 downto 0)),
              start_last_time_hi => std_logic_vector(start_last_time(63 downto 32)));
  -- 始動の列の結果を残す（割り込み書き込みの result・written_words は次の要求で変わる）
  process (clk)
  begin
    if rising_edge(clk) then
      if rst(RST_LA) = '1' then
        st_result <= "00";
        st_words  <= (others => '0');
        st_count  <= (others => '0');
      elsif inj_done = '1' and inj_kind_start = '1' then
        st_result <= inj_result;
        st_words  <= inj_words;
        st_count  <= st_count + 1;
      end if;
    end if;
  end process;
  -- サンプラへ: 割り込み書き込みがこれから線を求める（源の発火の周期と、登録した要求の周期。その次の周期から
  -- swd_injector の busy が立つ）。サンプラは up_busy の間、次のか所を始めずに境界で待つので、一致した標本のか所を
  -- 読み終えた境界で線を渡す（stop-precision-design §3.2。値の一致は標本の 2 周期後、サンプラが次のか所を始めるのは
  -- 標本の 3 周期後なので、要求を登録する前の発火の周期から含める）
  inj_claim <= inj_trigger or inj_request;
  end generate;

  -- 故障注入が無い（クロストリガごと無いか、構成で外した）: DAP の命令はそのまま PHY へ。状態の result = 3（構成に無い）
  no_xtrig_inject : if not (XTRIG and INJECTOR) generate
    phy_cmd_valid   <= swd_cmd_valid;
    swd_cmd_ready   <= phy_cmd_ready;
    phy_cmd_cond    <= swd_cmd_cond;
    phy_cmd_op      <= swd_cmd_op;
    phy_cmd_count   <= swd_cmd_count;
    phy_cmd_request <= swd_cmd_request;
    phy_cmd_data    <= swd_cmd_data;
    dap_rsp_valid   <= up_rsp_valid;
    inj_busy        <= '0';
    inj_claim       <= '0';
    inj_held        <= '0';
    inj_written     <= '0';
    inj_result      <= "11";
    inj_ack         <= (others => '0');
    inj_count       <= (others => '0');
    inj_transfers   <= (others => '0');
    inj_words       <= (others => '0');
    inj_w_addr      <= (others => '0');
    inj_w_value     <= (others => '0');
    inj_w_time      <= (others => '0');
    inj_kind_start  <= '0';
    inj_done        <= '0';
    inject_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_INJECT), bus_rsp => csr_rsp(CSR_WIN_INJECT));
  end generate;

  -- 語の時刻: クロストリガの要求は inject_time・inject_first_time、始動の列は start_*_time（見張りの停止要求の時刻を
  -- 再開の時刻で上書きしない。halt-watch-fpga-restart-design §3.2）
  process (clk)
  begin
    if rising_edge(clk) then
      if inj_written = '1' and inj_kind_start = '0' then
        inject_time <= inj_w_time;                       -- 最後の語の DRW の線上の点（time-accuracy R5）
        if inj_words = 1 then
          inject_first_time <= inj_w_time;               -- 最初の語
        end if;
      end if;
      if inj_written = '1' and inj_kind_start = '1' then
        start_last_time <= inj_w_time;
        if inj_words = 1 then
          start_first_time <= inj_w_time;
        end if;
      end if;
      -- 始動の列が成功で終わった周期の次に、値の一致の門を開ける（その後に始めたサンプラのか所の標本だけが比べられる）
      start_ok <= inj_done and inj_kind_start and not inj_result(0) and not inj_result(1);
    end if;
  end process;

  inject_status <= (busy => inj_busy, result => inj_result, ack => inj_ack, count => inj_count, time => inject_time);
  inject_event  <= (written => inj_written, transfers => inj_transfers, value => inj_w_value, addr => inj_w_addr,
                    time => std_logic_vector(inj_w_time(47 downto 0)));

  -- DAP サンプラ: 上流（dap_processor・swd_injector）の命令の境界で線を借り、周期的にメモリを読む
  sampler_g : if SAMPLER generate
    signal period : std_logic_vector(31 downto 0);
    signal ap : std_logic_vector(7 downto 0);
    signal tbl_write, tbl_enable : std_logic;
    signal jitter : std_logic_vector(3 downto 0);
    signal tbl_index : std_logic_vector(2 downto 0);
    signal tbl_addr : std_logic_vector(31 downto 0);
    signal tbl_words : std_logic_vector(3 downto 0);
    signal rounds, errors, overruns : unsigned(31 downto 0);
    signal check_words : std_logic_vector(6 downto 0);
  begin
    up_busy <= dap_busy_i or inj_busy or inj_claim;
    smp_i : entity work.dap_sampler
      generic map (CHECK_MAX => SAMPLER_CHECK_MAX)
      port map (clk => clk, rst => rst(RST_DEBUG), wait_retry => wait_retry, run => smp_run, period => unsigned(period),
                jitter => to_integer(unsigned(jitter)), ap => ap,
                tbl_write => tbl_write, tbl_index => to_integer(unsigned(tbl_index)), tbl_addr => tbl_addr,
                tbl_enable => tbl_enable, tbl_words => tbl_words, check_words => to_integer(unsigned(check_words)),
                enable => swd_enable, jtag => jtag, up_busy => up_busy, line_handed => smp_handed,
                select_shadow => select_shadow, csw_shadow => csw_shadow, tar_shadow => tar_shadow,
                shadow_valid => shadow_valid, shadow_ap => shadow_ap,
                sample_valid => smp_valid, sample_addr => smp_addr, sample_data => smp_data, sample_ack => smp_ack,
                sample_index => smp_index, sample_time => smp_time,
                rec_valid => smp_rec_valid, rec_kind => smp_rec_kind, rec_word => smp_rec_word,
                busy => smp_busy, held => smp_held, rounds => rounds, errors => errors, overruns => overruns,
                restores => open, last_ack => smp_last_ack,     -- restores は試験用（数えない）
                up_injector_held => inj_held, up_injector_ap => inject_cfg.ap,
                up_cmd_valid => phy_cmd_valid, up_cmd_ready => phy_cmd_ready, up_cmd_cond => phy_cmd_cond,
                up_cmd_op => phy_cmd_op,
                up_cmd_count => phy_cmd_count, up_cmd_req => phy_cmd_request, up_cmd_data => phy_cmd_data,
                up_rsp_valid => up_rsp_valid,
                phy_cmd_valid => port_cmd_valid, phy_cmd_ready => port_cmd_ready, phy_cmd_cond => port_cmd_cond,
                phy_cmd_op => port_cmd_op,
                phy_cmd_count => port_cmd_count, phy_cmd_req => port_cmd_request, phy_cmd_data => port_cmd_data,
                phy_rsp_valid => swd_rsp_valid, phy_rsp_ack => swd_rsp_ack,
                phy_rsp_data => swd_rsp_data(31 downto 0), phy_rsp_par => swd_rsp_parity, phy_rsp_time => swd_rsp_time);
    csr_sampler_i : entity work.csr_sampler
      port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_SAMPLER),
                bus_cmd => csr_cmd, bus_rsp => csr_rsp(CSR_WIN_SAMPLER),
                control_run => smp_run, control_ap => ap, control_jitter => jitter, period => period,
                slot_index => tbl_index, slot_addr => tbl_addr, slot_config_enable => tbl_enable,
                slot_config_words => tbl_words, slot_write => tbl_write,
                rounds => std_logic_vector(rounds), errors => std_logic_vector(errors),
                overruns => std_logic_vector(overruns), status_last_ack => smp_last_ack, status_busy => smp_busy,
                status_held => smp_held, check_words => check_words,
                check_max => std_logic_vector(to_unsigned(SAMPLER_CHECK_MAX, 7)));
  end generate;

  -- サンプラの値の一致（R21）: サンプラの標本が条件に入った瞬間 → クロストリガの源 bit5
  smatch_g : if SAMPLER and SAMPLER_MATCH generate
    signal m_addr, m_addr_mask, m_mask, m_value : std_logic_vector(31 downto 0);
    signal m_fire_index : unsigned(6 downto 0);
    signal m_enable, m_once, m_matched, m_any : std_logic;
    signal m_seen, m_count : unsigned(31 downto 0);
    signal m_time, m_prev : unsigned(47 downto 0);
    signal m_unmatched : std_logic_vector(127 downto 0);
    signal m_after, m_first_sample, m_fire_first, m_started : std_logic;
  begin
    smp_match_i : entity work.sampler_match
      port map (clk => clk, rst => rst(RST_DEBUG), enable => m_enable,
                match_addr => m_addr, match_addr_mask => m_addr_mask, match_mask => m_mask, match_value => m_value,
                once => m_once, after_start => m_after, first_sample => m_first_sample,
                start => start_ok, start_time => start_first_time(47 downto 0),
                sample_valid => smp_valid, sample_addr => smp_addr, sample_data => smp_data, sample_ack => smp_ack,
                sample_index => smp_index, sample_time => smp_time,
                fire => smp_match, matched => m_matched, seen_any => m_any, seen => m_seen, count => m_count,
                fire_time => m_time, prev_time => m_prev, fire_index => m_fire_index, unmatched_seen => m_unmatched,
                fire_first => m_fire_first, started => m_started);
    csr_smatch_i : entity work.csr_sampler_match
      port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_SAMPLER_MATCH),
                bus_cmd => csr_cmd, bus_rsp => csr_rsp(CSR_WIN_SAMPLER_MATCH),
                addr => m_addr, mask => m_mask, value => m_value, control_enable => m_enable, control_once => m_once,
                control_after_start => m_after, control_first_sample => m_first_sample, control_started => m_started,
                fire_first => m_fire_first,
                control_matched => m_matched, control_seen_any => m_any,
                seen => std_logic_vector(m_seen), count => std_logic_vector(m_count),
                time_lo => std_logic_vector(m_time(31 downto 0)), time_hi => x"0000" & std_logic_vector(m_time(47 downto 32)),
                prev_lo => std_logic_vector(m_prev(31 downto 0)), prev_hi => x"0000" & std_logic_vector(m_prev(47 downto 32)),
                addr_mask => m_addr_mask, fire_index => std_logic_vector(m_fire_index),
                unmatched0 => m_unmatched(31 downto 0), unmatched1 => m_unmatched(63 downto 32),
                unmatched2 => m_unmatched(95 downto 64), unmatched3 => m_unmatched(127 downto 96));
  end generate;

  no_smatch_g : if not (SAMPLER and SAMPLER_MATCH) generate
    smp_match <= '0';
    smatch_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_SAMPLER_MATCH), bus_rsp => csr_rsp(CSR_WIN_SAMPLER_MATCH));
  end generate;

  -- DAP サンプラが無い: 上流の命令はそのまま PHY へ、サンプラの窓は読むとエラー
  no_sampler_g : if not SAMPLER generate
    port_cmd_valid   <= phy_cmd_valid;
    phy_cmd_ready    <= port_cmd_ready;
    port_cmd_cond    <= phy_cmd_cond;
    port_cmd_op      <= phy_cmd_op;
    port_cmd_count   <= phy_cmd_count;
    port_cmd_request <= phy_cmd_request;
    port_cmd_data    <= phy_cmd_data;
    up_rsp_valid     <= swd_rsp_valid;
    up_busy          <= dap_busy_i or inj_busy;
    smp_run <= '0'; smp_valid <= '0'; smp_busy <= '0'; smp_held <= '0'; smp_handed <= '0';
    smp_addr <= (others => '0'); smp_data <= (others => '0'); smp_ack <= (others => '0'); smp_last_ack <= (others => '0');
    smp_index <= (others => '0'); smp_time <= (others => '0');
    smp_rec_valid <= '0'; smp_rec_kind <= (others => '0'); smp_rec_word <= (others => '0');
    sampler_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => csr_req(CSR_WIN_SAMPLER), bus_rsp => csr_rsp(CSR_WIN_SAMPLER));
  end generate;

  sampler_event <= (valid => smp_rec_valid, kind => smp_rec_kind, word => smp_rec_word);

  dap_fire <= swd_cmd_valid and swd_cmd_ready;
  monitor_i : entity work.dap_monitor
    port map (clk => clk, rst => rst(RST_DEBUG), enable => swd_enable, cmd_fire => dap_fire, cmd_op => swd_cmd_op, cmd_req => swd_cmd_request,
              cmd_data => swd_cmd_data(31 downto 0), rsp_valid => dap_rsp_valid, rsp_ack => swd_rsp_ack,
              rsp_data => swd_rsp_data(31 downto 0), rsp_parity => swd_rsp_parity,
              match_addr => match_cfg.addr, addr_mask => match_cfg.addr_mask, match_data => match_cfg.data,
              data_mask => match_cfg.data_mask, on_write => match_cfg.on_write, on_read => match_cfg.on_read,
              select_shadow => select_shadow, csw_shadow => csw_shadow, tar_shadow => tar_shadow,
              shadow_valid => shadow_valid, shadow_ap => shadow_ap, match => match, ev_valid => ev_valid, ev_request => ev_request,
              ev_ack => ev_ack, ev_parity => ev_parity, ev_addr => ev_addr, ev_addr_known => ev_addr_known,
              ev_data => ev_data, rsp_time => swd_rsp_time, ev_time => ev_time);
  match_time <= ev_time;                                   -- 一致した転送の線上の点（match は ev と同じ周期）
  host_kind  <= EVENT_KIND_HOST when ev_addr_known = '1' else EVENT_KIND_HOST_UNTRACKED;   -- DRW でメモリの番地を追えなかった
  xfer_event <= (valid => ev_valid, kind => host_kind, parity => ev_parity,
                 request => ev_request, ack => ev_ack, data => ev_data, addr => ev_addr,
                 time => std_logic_vector(ev_time(47 downto 0)));

  port_i : entity work.debug_port
    generic map (HAS_JTAG => HAS_JTAG)
    port map (clk => clk, rst => rst(RST_DEBUG), enable => swd_enable, jtag => jtag, half_period => swd_half_period,
              turnaround => swd_turnaround, data_phase => swd_data_phase, idle_cycles => swd_idle_cycles,
              sample_delay => to_integer(unsigned(swd_sample_delay)),
              jtag_sample_delay => to_integer(unsigned(jtag_sample_delay)),
              jtag_dr_before => jtag_dr_before, jtag_dr_after => jtag_dr_after,
              jtag_ir_before => jtag_ir_before, jtag_ir_after => jtag_ir_after, jtag_ir_len => jtag_ir_len,
              now => time_count,
              cmd_valid => port_cmd_valid, cmd_ready => port_cmd_ready, cmd_cond => port_cmd_cond, cmd_op => port_cmd_op,
              cmd_count => port_cmd_count, cmd_request => port_cmd_request, cmd_data => port_cmd_data,
              rsp_valid => swd_rsp_valid, rsp_ack => swd_rsp_ack, rsp_data => swd_rsp_data,
              rsp_parity_error => swd_rsp_parity, rsp_time => swd_rsp_time, swclk_tck => swclk_o, swclk_tck_oe => swclk_oe,
              swdio_tms_o => swdio_o, swdio_tms_oe => swdio_oe_i, swdio_tms_lead => swdio_o_lead, swdio_i => swdio_i,
              tdi => tdi_o, tdi_oe => tdi_oe, tdo => tdo_i);
  swdio_oe <= swdio_oe_i;

  debug_csr_i : entity work.csr_debug
    port map (clk => clk, rst => rst(RST_BUS), bus_req => csr_req(CSR_WIN_DEBUG),
              bus_cmd => csr_cmd, bus_rsp => csr_rsp(CSR_WIN_DEBUG),
              sample_delay_swd => swd_sample_delay, sample_delay_jtag => jtag_sample_delay);

  -- PHY の設定（イベント 16 の [119:48]。並びは event_collector.vhd の先頭）。設定は PHY が命令を実行していない間に変わる
  -- SWD の取り込みの遅れの上位ビット（0〜7 に広げた分）は互換のため [57] に置く（下位 2 ビットは [28:27] のまま）
  cfg_word <= (71 downto 58 => '0') & swd_sample_delay(2) & std_logic_vector(to_unsigned(jtag_ir_len - 1, 5))
              & std_logic_vector(to_unsigned(jtag_ir_after, 6)) & std_logic_vector(to_unsigned(jtag_ir_before, 6))
              & std_logic_vector(to_unsigned(jtag_dr_after, 3)) & std_logic_vector(to_unsigned(jtag_dr_before, 3))
              & swd_enable & jtag & jtag_sample_delay & swd_sample_delay(1 downto 0) & swd_data_phase
              & std_logic_vector(to_unsigned(swd_turnaround - 1, 2)) & std_logic_vector(to_unsigned(swd_idle_cycles, 8))
              & std_logic_vector(swd_half_period);
end architecture;
