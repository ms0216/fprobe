-- テスト専用: dap_processor + swd_injector + dap_sampler + dap_monitor + swd_phy + swo_receiver、SWDIO の共有線（ターゲットモデルと解決、プルアップ）と nRESET（オープンドレイン）。
-- now: リセットから数える周期のカウンタ（タイムベースの代わり。線上の点の時刻 rsp_time・ev_time 等の試験が端子の辺と比べる）。
-- PHY の設定（cfg_*）は dap_processor が持つ値をそのまま出す（time-accuracy R7 の試験がホストの式に渡す）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity dap_harness is
  generic (
    HAS_JTAG : boolean := true;           -- 構成で JTAG を外した版の試験（dap_no_jtag_tb）
    HAS_SWO_MANCHESTER : boolean := true; -- 構成で SWO の Manchester を外した版の試験（swo_tb）
    SAMPLER_CHECK_MAX : positive := 16    -- サンプラの JTAG の確認の間隔 K の上限
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    packet_size  : in  natural range 64 to 512;
    in_valid     : in  std_logic;
    in_ready     : out std_logic;
    in_data      : in  std_logic_vector(7 downto 0);
    in_last      : in  std_logic;
    out_valid    : out std_logic;
    out_ready    : in  std_logic;
    out_data     : out std_logic_vector(7 downto 0);
    out_last     : out std_logic;
    swclk        : out std_logic;
    swdio        : out std_logic;
    tdi          : out std_logic;                      -- JTAG（SWDIO = TMS、SWCLK = TCK）
    tdo          : in  std_logic := '1';
    target_swdio : in  std_logic;
    swd_sample_delay  : in natural range 0 to 7 := 0;   -- 取り込みの遅れ（CSR debug.sample_delay の代わり）
    jtag_sample_delay : in natural range 0 to 7 := 0;
    phy_fire     : out std_logic;                      -- PHY が命令を受けた周期（転送の間の空きの試験）
    nreset       : out std_logic;
    nreset_ext   : in  std_logic;         -- ターゲット側からの駆動（'Z' で解放、'0' でリセット保持）
    -- 割り込み（swd_injector）
    inj_request  : in  std_logic := '0';
    inj_ap       : in  std_logic_vector(7 downto 0) := x"00";
    inj_addr     : in  std_logic_vector(31 downto 0) := x"E000EDF0";
    inj_value    : in  std_logic_vector(31 downto 0) := x"A05F0003";
    inj_busy     : out std_logic;
    inj_written  : out std_logic;
    inj_result   : out std_logic_vector(1 downto 0);
    inj_count    : out unsigned(15 downto 0);
    inj_transfers : out unsigned(7 downto 0);
    inj_extra    : in  natural range 0 to 7 := 0;
    inj_tbl_write : in std_logic := '0';
    inj_tbl_index : in natural range 0 to 7 := 0;
    inj_tbl_addr : in  std_logic_vector(31 downto 0) := (others => '0');
    inj_tbl_value : in std_logic_vector(31 downto 0) := (others => '0');
    inj_written_words : out unsigned(3 downto 0);
    inj_written_addr : out std_logic_vector(31 downto 0);
    inj_written_value : out std_logic_vector(31 downto 0);
    inj_start_request : in std_logic := '0';            -- 始動の列（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md §3.2）
    inj_start_first : in natural range 0 to 7 := 0;
    inj_start_last : in natural range 0 to 7 := 0;
    inj_start_pending : out std_logic;
    inj_kind_start : out std_logic;
    inj_done     : out std_logic;
    -- 一致（dap_monitor）
    match_addr   : in  std_logic_vector(31 downto 0) := x"00000000";
    addr_mask    : in  std_logic_vector(31 downto 0) := x"FFFFFFFF";
    match_data   : in  std_logic_vector(31 downto 0) := x"00000000";
    data_mask    : in  std_logic_vector(31 downto 0) := x"00000000";
    on_write     : in  std_logic := '0';
    on_read      : in  std_logic := '0';
    match        : out std_logic;
    ev_valid     : out std_logic;
    ev_request   : out std_logic_vector(3 downto 0);
    ev_ack       : out std_logic_vector(2 downto 0);
    ev_addr      : out std_logic_vector(31 downto 0);
    ev_data      : out std_logic_vector(31 downto 0);
    ev_time      : out unsigned(63 downto 0);
    rsp_time     : out unsigned(63 downto 0);            -- debug_port の応答の線上の点（どの転送でも）
    rsp_valid_out : out std_logic;                       -- debug_port の応答（どの転送でも）
    now          : out unsigned(63 downto 0);
    inj_written_time : out unsigned(63 downto 0);
    cfg_half_period : out unsigned(15 downto 0);
    cfg_turnaround : out natural range 1 to 4;
    cfg_idle_cycles : out natural range 0 to 255;
    cfg_data_phase : out std_logic;
    cfg_jtag     : out std_logic;
    -- サンプラ（dap_sampler）
    smp_run      : in  std_logic := '0';
    smp_period   : in  unsigned(31 downto 0) := (others => '0');
    smp_jitter   : in  natural range 0 to 15 := 0;
    smp_ap       : in  std_logic_vector(7 downto 0) := x"00";
    smp_write    : in  std_logic := '0';                 -- か所の表への書き込み（1 周期）
    smp_index    : in  natural range 0 to 7 := 0;
    smp_addr     : in  std_logic_vector(31 downto 0) := (others => '0');
    smp_enable   : in  std_logic := '0';
    smp_words    : in  std_logic_vector(3 downto 0) := (others => '0');
    smp_check_words : in natural range 0 to 127 := 1;
    smp_valid    : out std_logic;
    smp_sample_addr : out std_logic_vector(31 downto 0);
    smp_data     : out std_logic_vector(31 downto 0);
    smp_ack      : out std_logic_vector(2 downto 0);
    smp_sample_index : out unsigned(6 downto 0);
    smp_sample_time : out unsigned(47 downto 0);
    smp_rec_valid : out std_logic;
    smp_rec_kind : out std_logic_vector(7 downto 0);
    smp_rec_word : out std_logic_vector(119 downto 0);
    smp_busy     : out std_logic;
    smp_rounds   : out unsigned(31 downto 0);
    smp_errors   : out unsigned(31 downto 0);
    smp_overruns : out unsigned(31 downto 0);
    smp_restores : out unsigned(15 downto 0);
    smp_handoffs : out unsigned(15 downto 0);
    smp_held     : out std_logic;
    -- SWO
    swo          : in  std_logic := '1';
    swo_valid    : out std_logic;
    swo_ready    : in  std_logic := '1';
    swo_data     : out std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of dap_harness is
  signal enable, data_phase, cmd_valid, cmd_ready, rsp_valid, rsp_parity, busy : std_logic;
  signal wait_retry : unsigned(15 downto 0);
  signal p_valid, p_ready, d_rsp_valid : std_logic;
  signal cmd_cond, i_cond, p_cond : std_logic;
  signal i_valid, i_ready, i_rsp_valid, inj_busy_i, inj_held_i, up_busy, smp_held_i, smp_handed_i : std_logic;
  signal i_op : std_logic_vector(1 downto 0);
  signal i_count : natural range 1 to 64;
  signal i_request : std_logic_vector(3 downto 0);
  signal i_data : std_logic_vector(63 downto 0);
  signal p_op : std_logic_vector(1 downto 0);
  signal p_count : natural range 1 to 64;
  signal p_request : std_logic_vector(3 downto 0);
  signal p_data : std_logic_vector(63 downto 0);
  signal sel : std_logic_vector(31 downto 0);
  signal csw_shadow, tar_shadow : std_logic_vector(31 downto 0);
  signal shadow_valid : std_logic;
  signal shadow_ap : std_logic_vector(7 downto 0);
  signal fire : std_logic;
  signal jtag : std_logic;
  signal dr_before, dr_after : natural range 0 to 7;
  signal ir_before, ir_after : natural range 0 to 63;
  signal ir_len : natural range 1 to 32;
  signal swo_run, swo_error, swo_overrun, swo_manchester : std_logic;
  signal swo_divisor : unsigned(15 downto 0);
  signal half_period : unsigned(15 downto 0);
  signal turnaround  : natural range 1 to 4;
  signal idle_cycles : natural range 0 to 255;
  signal cmd_op      : std_logic_vector(1 downto 0);
  signal cmd_count   : natural range 1 to 64;
  signal cmd_request : std_logic_vector(3 downto 0);
  signal cmd_data, rsp_data : std_logic_vector(63 downto 0);
  signal rsp_ack     : std_logic_vector(2 downto 0);
  signal sclk, sclk_oe, sdo, sdo_oe, nreset_low : std_logic;
  signal line, sdi, rst_line, rst_in : std_logic;
  signal count, p_time : unsigned(63 downto 0) := (others => '0');
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        count <= (others => '0');
      else
        count <= count + 1;
      end if;
    end if;
  end process;
  now <= count;
  rsp_time <= p_time;
  rsp_valid_out <= rsp_valid;
  cfg_half_period <= half_period;
  cfg_turnaround <= turnaround;
  cfg_idle_cycles <= idle_cycles;
  cfg_data_phase <= data_phase;
  cfg_jtag <= jtag;

  dap_i : entity work.dap_processor
    generic map (CLK_HZ => 100_000_000, HAS_JTAG => HAS_JTAG, HAS_SWO_MANCHESTER => HAS_SWO_MANCHESTER)
    port map (clk => clk, rst => rst, packet_size => packet_size,
              in_valid => in_valid, in_ready => in_ready, in_data => in_data, in_last => in_last,
              out_valid => out_valid, out_ready => out_ready, out_data => out_data, out_last => out_last,
              swd_enable => enable, swd_half_period => half_period, swd_turnaround => turnaround,
              swd_data_phase => data_phase, swd_idle_cycles => idle_cycles,
              swd_cmd_valid => cmd_valid, swd_cmd_ready => cmd_ready, swd_cmd_cond => cmd_cond, swd_cmd_op => cmd_op,
              swd_cmd_count => cmd_count, swd_cmd_request => cmd_request, swd_cmd_data => cmd_data,
              swd_rsp_valid => d_rsp_valid, swd_rsp_ack => rsp_ack, swd_rsp_data => rsp_data,
              swd_rsp_parity => rsp_parity, swd_rsp_time => p_time, nreset_low => nreset_low, nreset_in => rst_in,
              wait_retry_out => wait_retry,
              jtag => jtag, jtag_dr_before => dr_before, jtag_dr_after => dr_after,
              jtag_ir_before => ir_before, jtag_ir_after => ir_after, jtag_ir_len => ir_len,
              swo_run => swo_run, swo_divisor => swo_divisor, swo_manchester => swo_manchester,
              swo_error => swo_error, swo_overrun => swo_overrun, busy => busy);

  swo_i : entity work.swo_receiver
    generic map (HAS_MANCHESTER => HAS_SWO_MANCHESTER)
    port map (clk => clk, rst => rst, run => swo_run, manchester => swo_manchester, divisor => swo_divisor, swo => swo,
              out_valid => swo_valid, out_ready => swo_ready, out_data => swo_data, flush => open,
              error => swo_error, overrun => swo_overrun);

  inj_i : entity work.swd_injector
    port map (clk => clk, rst => rst, wait_retry => wait_retry, request => inj_request, enable => enable, jtag => jtag, dap_busy => busy,
              select_shadow => sel, csw_shadow => csw_shadow, tar_shadow => tar_shadow,
              shadow_valid => shadow_valid, shadow_ap => shadow_ap, ap => inj_ap, addr => inj_addr, value => inj_value,
              busy => inj_busy_i, held => inj_held_i, written => inj_written, result => inj_result, last_ack => open,
              count => inj_count, transfers => inj_transfers, written_words => inj_written_words,
              written_addr => inj_written_addr, written_value => inj_written_value, written_time => inj_written_time,
              extra => inj_extra, tbl_write => inj_tbl_write, tbl_index => inj_tbl_index, tbl_addr => inj_tbl_addr,
              tbl_value => inj_tbl_value, line_held => smp_held_i, line_handed => smp_handed_i,
              start_request => inj_start_request, start_first => inj_start_first, start_last => inj_start_last,
              start_pending => inj_start_pending, kind_start => inj_kind_start, done => inj_done,
              dap_cmd_valid => cmd_valid, dap_cmd_ready => cmd_ready, dap_cmd_cond => cmd_cond, dap_cmd_op => cmd_op,
              dap_cmd_count => cmd_count, dap_cmd_req => cmd_request, dap_cmd_data => cmd_data,
              dap_rsp_valid => d_rsp_valid,
              phy_cmd_valid => i_valid, phy_cmd_ready => i_ready, phy_cmd_cond => i_cond, phy_cmd_op => i_op,
              phy_cmd_count => i_count, phy_cmd_req => i_request, phy_cmd_data => i_data,
              phy_rsp_valid => i_rsp_valid, phy_rsp_ack => rsp_ack, phy_rsp_data => rsp_data(31 downto 0),
              phy_rsp_par => rsp_parity, phy_rsp_time => p_time);
  inj_busy <= inj_busy_i;
  smp_held <= smp_held_i;

  up_busy <= busy or inj_busy_i;
  smp_i : entity work.dap_sampler
    generic map (CHECK_MAX => SAMPLER_CHECK_MAX)
    port map (clk => clk, rst => rst, wait_retry => wait_retry, run => smp_run, period => smp_period, jitter => smp_jitter, ap => smp_ap,
              tbl_write => smp_write, tbl_index => smp_index, tbl_addr => smp_addr, tbl_enable => smp_enable,
              tbl_words => smp_words, check_words => smp_check_words,
              enable => enable, jtag => jtag, up_busy => up_busy, line_handed => smp_handed_i,
              select_shadow => sel, csw_shadow => csw_shadow, tar_shadow => tar_shadow, shadow_valid => shadow_valid, shadow_ap => shadow_ap,
              sample_valid => smp_valid, sample_addr => smp_sample_addr, sample_data => smp_data, sample_ack => smp_ack, sample_index => smp_sample_index,
              sample_time => smp_sample_time, rec_valid => smp_rec_valid, rec_kind => smp_rec_kind, rec_word => smp_rec_word,
              busy => smp_busy, rounds => smp_rounds, errors => smp_errors, overruns => smp_overruns,
              restores => smp_restores, handoffs => smp_handoffs,
              up_injector_held => inj_held_i, up_injector_ap => inj_ap, held => smp_held_i, last_ack => open,
              up_cmd_valid => i_valid, up_cmd_ready => i_ready, up_cmd_cond => i_cond, up_cmd_op => i_op, up_cmd_count => i_count,
              up_cmd_req => i_request, up_cmd_data => i_data, up_rsp_valid => i_rsp_valid,
              phy_cmd_valid => p_valid, phy_cmd_ready => p_ready, phy_cmd_cond => p_cond, phy_cmd_op => p_op,
              phy_cmd_count => p_count, phy_cmd_req => p_request, phy_cmd_data => p_data,
              phy_rsp_valid => rsp_valid, phy_rsp_ack => rsp_ack, phy_rsp_data => rsp_data(31 downto 0),
              phy_rsp_par => rsp_parity, phy_rsp_time => p_time);

  fire <= cmd_valid and cmd_ready;
  phy_fire <= p_valid and p_ready;
  mon_i : entity work.dap_monitor
    port map (clk => clk, rst => rst, enable => enable, cmd_fire => fire, cmd_op => cmd_op, cmd_req => cmd_request,
              cmd_data => cmd_data(31 downto 0), rsp_valid => d_rsp_valid, rsp_ack => rsp_ack,
              rsp_data => rsp_data(31 downto 0), rsp_parity => rsp_parity, rsp_time => p_time,
              match_addr => match_addr, addr_mask => addr_mask, match_data => match_data,
              data_mask => data_mask, on_write => on_write, on_read => on_read,
              select_shadow => sel, csw_shadow => csw_shadow, tar_shadow => tar_shadow,
              shadow_valid => shadow_valid, shadow_ap => shadow_ap, match => match, ev_valid => ev_valid, ev_request => ev_request,
              ev_ack => ev_ack, ev_parity => open, ev_addr => ev_addr, ev_data => ev_data, ev_time => ev_time);

  phy_i : entity work.debug_port
    generic map (HAS_JTAG => HAS_JTAG)
    port map (clk => clk, rst => rst, enable => enable, jtag => jtag, half_period => half_period,
              turnaround => turnaround, data_phase => data_phase, idle_cycles => idle_cycles, sample_delay => swd_sample_delay,
              jtag_sample_delay => jtag_sample_delay,
              jtag_dr_before => dr_before, jtag_dr_after => dr_after, jtag_ir_before => ir_before,
              jtag_ir_after => ir_after, jtag_ir_len => ir_len, now => count,
              cmd_valid => p_valid, cmd_ready => p_ready, cmd_cond => p_cond, cmd_op => p_op, cmd_count => p_count,
              cmd_request => p_request, cmd_data => p_data, rsp_valid => rsp_valid,
              rsp_ack => rsp_ack, rsp_data => rsp_data, rsp_parity_error => rsp_parity, rsp_time => p_time,
              swclk_tck => sclk, swclk_tck_oe => sclk_oe, swdio_tms_o => sdo, swdio_tms_oe => sdo_oe,
              swdio_i => sdi, tdi => tdi, tdi_oe => open, tdo => tdo);

  line <= sdo when sdo_oe = '1' else 'Z';
  line <= target_swdio;
  line <= 'H';
  sdi   <= to_x01(line);
  swdio <= sdi;
  swclk <= sclk when sclk_oe = '1' else '1';

  rst_line <= '0' when nreset_low = '1' else 'Z';
  rst_line <= nreset_ext;
  rst_line <= 'H';
  rst_in <= to_x01(rst_line);
  nreset <= rst_in;
end architecture;
