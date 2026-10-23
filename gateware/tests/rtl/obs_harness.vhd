-- obs_jtag_engine ↔（JTAG の線）↔ obs_soft_core（対象。clk は fprobe と非同期の tgt_clk。ONE_CLOCK で 1 クロック版）。
-- CHAIN = true で対象の TDO 側と TDI 側に BYPASS の装置（観測の回路の無い obs_soft_tap、IR 4 ビット）を 1 つずつ置く。
-- stuck_tdo = 1 で fprobe に返る TDO を 1 に、stuck_low = 1 で 0 に張り付ける（観測の回路が答えない）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity obs_harness is
  generic (
    CHAIN     : boolean := false;
    ONE_CLOCK : boolean := false;
    SNAP      : boolean := false         -- 対象を標本型（obs_snap_core）にする
  );
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    tgt_clk     : in  std_logic;
    tgt_rst     : in  std_logic;
    probes      : in  std_logic_vector(15 downto 0);
    stuck_tdo   : in  std_logic;
    stuck_low   : in  std_logic;
    -- エンジンの設定
    enable      : in  std_logic;
    free_pins     : in  std_logic;
    half_period : in  unsigned(15 downto 0);
    ir_len      : in  unsigned(5 downto 0);
    ir_before   : in  unsigned(5 downto 0);
    ir_after    : in  unsigned(5 downto 0);
    dr_before   : in  unsigned(2 downto 0);
    dr_after    : in  unsigned(2 downto 0);
    user_ir     : in  std_logic_vector(31 downto 0);
    words       : in  unsigned(8 downto 0);
    sync_period : in  unsigned(15 downto 0);
    cmd_push    : in  std_logic;
    cmd_data    : in  std_logic_vector(15 downto 0);
    match_mask  : in  std_logic_vector(15 downto 0);
    match_value : in  std_logic_vector(15 downto 0);
    match_change : in std_logic_vector(15 downto 0);
    match_on    : in  std_logic;
    -- 状態・イベント
    running       : out std_logic;
    target_running : out std_logic;
    driving       : out std_logic;
    id_ok         : out std_logic;
    stuck         : out std_logic;
    cmd_pending   : out std_logic;
    overflow_seen : out std_logic;
    cmd_dropped   : out std_logic;
    scans         : out unsigned(31 downto 0);
    read_value    : out std_logic_vector(31 downto 0);
    id            : out std_logic_vector(31 downto 0);
    ev_valid      : out std_logic;
    ev_sync       : out std_logic;
    ev_data       : out std_logic_vector(31 downto 0);
    ev_addr       : out std_logic_vector(31 downto 0);
    ev_time       : out std_logic_vector(47 downto 0);
    match         : out std_logic;
    match_count   : out unsigned(31 downto 0);
    -- 線（見るだけ）
    tck           : out std_logic;
    tms           : out std_logic;
    oe            : out std_logic;
    time_now      : out unsigned(47 downto 0)
  );
end entity;

architecture sim of obs_harness is
  signal time_count : unsigned(47 downto 0) := (others => '0');
  signal j_tck, j_tms, j_tdi, j_tdo, tgt_tdo : std_logic;
  signal core_tdi : std_logic;    -- 対象の TAP の TDI
  signal core_tdo : std_logic;    -- 対象の TAP の TDO
begin
  process (clk)
  begin
    if rising_edge(clk) then
      time_count <= time_count + 1;
    end if;
  end process;
  time_now <= time_count;
  tck <= j_tck;
  tms <= j_tms;

  engine : entity work.obs_jtag_engine
    port map (clk => clk, rst => rst, time_count => time_count, enable => enable, free_pins => free_pins,
              half_period => half_period, sample_delay => 0, ir_len => ir_len, ir_before => ir_before, ir_after => ir_after,
              dr_before => dr_before, dr_after => dr_after, user_ir => user_ir, words => words,
              sync_period => sync_period, cmd_push => cmd_push, cmd_data => cmd_data,
              match_mask => match_mask, match_value => match_value, match_change => match_change,
              match_on => match_on, match => match, match_count => match_count,
              running => running, target_running => target_running, driving => driving, id_ok => id_ok, stuck => stuck, cmd_pending => cmd_pending,
              overflow_seen => overflow_seen, cmd_dropped => cmd_dropped, scans => scans,
              read_value => read_value, id => id, ev_valid => ev_valid, ev_sync => ev_sync, ev_data => ev_data,
              ev_addr => ev_addr, ev_time => ev_time,
              tck => j_tck, tms => j_tms, tdi => j_tdi, tdo => j_tdo, oe => oe);

  j_tdo <= '1' when stuck_tdo = '1' else '0' when stuck_low = '1' else tgt_tdo;

  stream_g : if not SNAP generate
    target : entity work.obs_soft_core
      generic map (GROUPS => 1, FIFO_DEPTH => 64, CLOCK_HZ => 50_000_000, ONE_CLOCK => ONE_CLOCK)
      port map (clk => tgt_clk, rst => tgt_rst, probes => probes, tck => j_tck, tms => j_tms, tdi => core_tdi,
                tdo => core_tdo, tdo_oe => open);
  end generate;
  snap_g : if SNAP generate
    target : entity work.obs_snap_core
      port map (clk => tgt_clk, rst => tgt_rst, probes => probes, tck => j_tck, tms => j_tms, tdi => core_tdi,
                tdo => core_tdo, tdo_oe => open);
  end generate;

  -- 鎖: TDI → [後の装置] → 対象 → [前の装置] → TDO
  chain_g : if CHAIN generate
    signal after_tdo : std_logic;
  begin
    after_dev : entity work.obs_soft_tap
      port map (tck => j_tck, tms => j_tms, tdi => j_tdi, tdo => after_tdo, tdo_oe => open,
                user_tdi => open, user_tdo => '0', user_sel => open, user_capture => open,
                user_shift => open, user_update => open, user_tlr => open);
    core_tdi <= after_tdo;
    before_dev : entity work.obs_soft_tap
      port map (tck => j_tck, tms => j_tms, tdi => core_tdo, tdo => tgt_tdo, tdo_oe => open,
                user_tdi => open, user_tdo => '0', user_sel => open, user_capture => open,
                user_shift => open, user_update => open, user_tlr => open);
  end generate;

  no_chain_g : if not CHAIN generate
    core_tdi <= j_tdi;
    tgt_tdo <= core_tdo;
  end generate;
end architecture;
