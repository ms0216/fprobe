-- テスト専用: dap_processor + swd_injector + dap_sampler + dap_monitor + swd_phy + swo_receiver、SWDIO の共有線（ターゲットモデルと解決、プルアップ）と nRESET（オープンドレイン）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity dap_harness is
  generic (
    HAS_JTAG : boolean := true;           -- 構成で JTAG を外した版の試験（dap_no_jtag_tb）
    HAS_SWO_MANCHESTER : boolean := true  -- 構成で SWO の Manchester を外した版の試験（swo_tb）
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
    smp_valid    : out std_logic;
    smp_sample_addr : out std_logic_vector(31 downto 0);
    smp_data     : out std_logic_vector(31 downto 0);
    smp_ack      : out std_logic_vector(2 downto 0);
    smp_busy     : out std_logic;
    smp_rounds   : out unsigned(31 downto 0);
    smp_errors   : out unsigned(31 downto 0);
    smp_overruns : out unsigned(31 downto 0);
    smp_restores : out unsigned(15 downto 0);
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
  signal p_valid, p_ready, d_rsp_valid : std_logic;
  signal i_valid, i_ready, i_rsp_valid, inj_busy_i, up_busy : std_logic;
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
begin
  dap_i : entity work.dap_processor
    generic map (CLK_HZ => 100_000_000, HAS_JTAG => HAS_JTAG, HAS_SWO_MANCHESTER => HAS_SWO_MANCHESTER)
    port map (clk => clk, rst => rst, packet_size => packet_size,
              in_valid => in_valid, in_ready => in_ready, in_data => in_data, in_last => in_last,
              out_valid => out_valid, out_ready => out_ready, out_data => out_data, out_last => out_last,
              swd_enable => enable, swd_half_period => half_period, swd_turnaround => turnaround,
              swd_data_phase => data_phase, swd_idle_cycles => idle_cycles,
              swd_cmd_valid => cmd_valid, swd_cmd_ready => cmd_ready, swd_cmd_op => cmd_op,
              swd_cmd_count => cmd_count, swd_cmd_request => cmd_request, swd_cmd_data => cmd_data,
              swd_rsp_valid => d_rsp_valid, swd_rsp_ack => rsp_ack, swd_rsp_data => rsp_data,
              swd_rsp_parity => rsp_parity, nreset_low => nreset_low, nreset_in => rst_in,
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
    port map (clk => clk, rst => rst, request => inj_request, enable => enable, dap_busy => busy,
              select_shadow => sel, csw_shadow => csw_shadow, tar_shadow => tar_shadow,
              shadow_valid => shadow_valid, ap => inj_ap, addr => inj_addr, value => inj_value,
              busy => inj_busy_i, written => inj_written, result => inj_result, last_ack => open,
              count => inj_count,
              dap_cmd_valid => cmd_valid, dap_cmd_ready => cmd_ready, dap_cmd_op => cmd_op,
              dap_cmd_count => cmd_count, dap_cmd_req => cmd_request, dap_cmd_data => cmd_data,
              dap_rsp_valid => d_rsp_valid,
              phy_cmd_valid => i_valid, phy_cmd_ready => i_ready, phy_cmd_op => i_op,
              phy_cmd_count => i_count, phy_cmd_req => i_request, phy_cmd_data => i_data,
              phy_rsp_valid => i_rsp_valid, phy_rsp_ack => rsp_ack, phy_rsp_data => rsp_data(31 downto 0),
              phy_rsp_par => rsp_parity);
  inj_busy <= inj_busy_i;

  up_busy <= busy or inj_busy_i;
  smp_i : entity work.dap_sampler
    port map (clk => clk, rst => rst, run => smp_run, period => smp_period, jitter => smp_jitter, ap => smp_ap,
              tbl_write => smp_write, tbl_index => smp_index, tbl_addr => smp_addr, tbl_enable => smp_enable,
              tbl_words => smp_words,
              enable => enable, up_busy => up_busy,
              select_shadow => sel, csw_shadow => csw_shadow, tar_shadow => tar_shadow, shadow_valid => shadow_valid,
              sample_valid => smp_valid, sample_addr => smp_sample_addr, sample_data => smp_data, sample_ack => smp_ack,
              busy => smp_busy, rounds => smp_rounds, errors => smp_errors, overruns => smp_overruns,
              restores => smp_restores, held => smp_held, last_ack => open,
              up_cmd_valid => i_valid, up_cmd_ready => i_ready, up_cmd_op => i_op, up_cmd_count => i_count,
              up_cmd_req => i_request, up_cmd_data => i_data, up_rsp_valid => i_rsp_valid,
              phy_cmd_valid => p_valid, phy_cmd_ready => p_ready, phy_cmd_op => p_op,
              phy_cmd_count => p_count, phy_cmd_req => p_request, phy_cmd_data => p_data,
              phy_rsp_valid => rsp_valid, phy_rsp_ack => rsp_ack, phy_rsp_data => rsp_data(31 downto 0),
              phy_rsp_par => rsp_parity);

  fire <= cmd_valid and cmd_ready;
  mon_i : entity work.dap_monitor
    port map (clk => clk, rst => rst, cmd_fire => fire, cmd_op => cmd_op, cmd_req => cmd_request,
              cmd_data => cmd_data(31 downto 0), rsp_valid => d_rsp_valid, rsp_ack => rsp_ack,
              rsp_data => rsp_data(31 downto 0), rsp_parity => rsp_parity,
              match_addr => match_addr, addr_mask => addr_mask, match_data => match_data,
              data_mask => data_mask, on_write => on_write, on_read => on_read,
              select_shadow => sel, csw_shadow => csw_shadow, tar_shadow => tar_shadow,
              shadow_valid => shadow_valid, match => match, ev_valid => ev_valid, ev_request => ev_request,
              ev_ack => ev_ack, ev_parity => open, ev_addr => ev_addr, ev_data => ev_data);

  phy_i : entity work.debug_port
    generic map (HAS_JTAG => HAS_JTAG)
    port map (clk => clk, rst => rst, enable => enable, jtag => jtag, half_period => half_period,
              turnaround => turnaround, data_phase => data_phase, idle_cycles => idle_cycles, sample_delay => 0,
              jtag_dr_before => dr_before, jtag_dr_after => dr_after, jtag_ir_before => ir_before,
              jtag_ir_after => ir_after, jtag_ir_len => ir_len,
              cmd_valid => p_valid, cmd_ready => p_ready, cmd_op => p_op, cmd_count => p_count,
              cmd_request => p_request, cmd_data => p_data, rsp_valid => rsp_valid,
              rsp_ack => rsp_ack, rsp_data => rsp_data, rsp_parity_error => rsp_parity,
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
