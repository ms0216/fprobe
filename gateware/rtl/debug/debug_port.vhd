-- デバッグポート: swd_phy と jtag_phy を jtag で切り替える（命令 I/F は共通）。
-- ピン: SWCLK/TCK、SWDIO/TMS は共用。TDI は JTAG のときだけ駆動、TDO は JTAG のときだけ見る（SWO と同じピン）。
-- jtag は PHY が命令を実行していないとき（DAP_Connect）にだけ変わること。
-- 応答（rsp_*）は 1 段のレジスタを通す（PHY の選択を応答の経路から外す。sys のタイミングのため）。
-- HAS_JTAG = false の構成では jtag_phy を作らない（jtag は無視して常に SWD。DAP_Connect が JTAG を断る）。
-- rsp_time: 応答の転送の線上の点の時刻（SWD は ACK の後の立ち上がり、JTAG は Update-DR を出る立ち上がり。各 PHY の先頭の
-- コメント）。応答と同じ 1 段で運ぶ（docs/superpowers/specs/2026-11-01-time-accuracy-design.md R5）。
-- 続けて受ける・cmd_cond（前の転送が OK のときだけ受ける）は各 PHY の先頭のコメント。PHY は前の命令の応答の前に次の命令を
-- 受けることがある（受理は応答より最大 1 つ先）。
-- swdio_tms_lead: SWDIO / TMS の出力の、その周期の前半の値（ODDR で出すボード用。JTAG の half_period = 1 で TMS を立ち下がりの
-- 半周期後に変えるため。jtag_phy の tms_lead）。SWD と、ODDR を使わないボードは swdio_tms_o だけを使う。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity debug_port is
  generic (
    HAS_JTAG : boolean := true                               -- jtag_phy を持つ（ポート jtag と別名にする: VHDL は大小文字を区別しない）
  );
  port (
    clk             : in  std_logic;
    rst             : in  std_logic;
    -- 設定
    enable          : in  std_logic;
    jtag            : in  std_logic;
    half_period     : in  unsigned(15 downto 0);
    turnaround      : in  natural range 1 to 4;
    data_phase      : in  std_logic;
    idle_cycles     : in  natural range 0 to 255;
    sample_delay    : in  natural range 0 to 7;              -- SWD の取り込みの遅れ（swd_phy）
    jtag_sample_delay : in natural range 0 to 7;             -- JTAG の TDO の取り込みの遅れ（jtag_phy）
    jtag_dr_before  : in  natural range 0 to 7;
    jtag_dr_after   : in  natural range 0 to 7;
    jtag_ir_before  : in  natural range 0 to 63;
    jtag_ir_after   : in  natural range 0 to 63;
    jtag_ir_len     : in  natural range 1 to 32;
    now             : in  unsigned(63 downto 0);             -- タイムベース
    -- 命令（swd_phy と同じ）
    cmd_valid       : in  std_logic;
    cmd_ready       : out std_logic;
    cmd_cond        : in  std_logic := '0';
    cmd_op          : in  std_logic_vector(1 downto 0);
    cmd_count       : in  natural range 1 to 64;
    cmd_request     : in  std_logic_vector(3 downto 0);
    cmd_data        : in  std_logic_vector(63 downto 0);
    rsp_valid       : out std_logic;
    rsp_ack         : out std_logic_vector(2 downto 0);
    rsp_data        : out std_logic_vector(63 downto 0);
    rsp_parity_error : out std_logic;
    rsp_time        : out unsigned(63 downto 0);
    -- ピン
    swclk_tck       : out std_logic;
    swclk_tck_oe    : out std_logic;
    swdio_tms_o     : out std_logic;
    swdio_tms_oe    : out std_logic;
    swdio_tms_lead  : out std_logic;
    swdio_i         : in  std_logic;
    tdi             : out std_logic;
    tdi_oe          : out std_logic;
    tdo             : in  std_logic
  );
end entity;

architecture rtl of debug_port is
  signal jtag_on, swd_en, jtag_en, s_cmd_valid, j_cmd_valid : std_logic;
  signal s_ready, s_valid, s_parity, s_clk, s_clk_oe, s_dio, s_dio_oe : std_logic;
  signal j_ready, j_valid, j_tck, j_tms, j_tms_lead, j_tdi : std_logic;
  signal s_ack, j_ack : std_logic_vector(2 downto 0);
  signal s_data, j_data : std_logic_vector(63 downto 0);
  signal s_time, j_time : unsigned(63 downto 0);
begin
  jtag_on <= jtag when HAS_JTAG else '0';
  swd_en  <= enable and not jtag_on;
  jtag_en <= enable and jtag_on;
  s_cmd_valid <= cmd_valid and not jtag_on;
  j_cmd_valid <= cmd_valid and jtag_on;

  swd_i : entity work.swd_phy
    port map (clk => clk, rst => rst, enable => swd_en, half_period => half_period, turnaround => turnaround,
              data_phase => data_phase, idle_cycles => idle_cycles, sample_delay => sample_delay, now => now,
              cmd_valid => s_cmd_valid, cmd_ready => s_ready, cmd_cond => cmd_cond, cmd_op => cmd_op, cmd_count => cmd_count,
              cmd_request => cmd_request, cmd_data => cmd_data, rsp_valid => s_valid, rsp_ack => s_ack,
              rsp_data => s_data, rsp_parity_error => s_parity, rsp_time => s_time,
              swclk => s_clk, swclk_oe => s_clk_oe, swdio_o => s_dio, swdio_oe => s_dio_oe, swdio_i => swdio_i);

  jtag_g : if HAS_JTAG generate
  jtag_i : entity work.jtag_phy
    port map (clk => clk, rst => rst, enable => jtag_en, half_period => half_period, idle_cycles => idle_cycles,
              dr_before => jtag_dr_before, dr_after => jtag_dr_after, ir_before => jtag_ir_before,
              ir_after => jtag_ir_after, ir_len => jtag_ir_len, sample_delay => jtag_sample_delay, now => now,
              cmd_valid => j_cmd_valid, cmd_ready => j_ready, cmd_cond => cmd_cond, cmd_op => cmd_op, cmd_count => cmd_count,
              cmd_request => cmd_request, cmd_data => cmd_data, rsp_valid => j_valid, rsp_ack => j_ack,
              rsp_data => j_data, rsp_time => j_time, tck => j_tck, tms => j_tms, tms_lead => j_tms_lead, tdi => j_tdi,
              tdo => tdo);
  end generate;

  -- JTAG の PHY が無い: その出力も明示的に駆動する（jtag_on = '0' なので使われない）
  no_jtag_g : if not HAS_JTAG generate
    j_ready <= '0';
    j_valid <= '0';
    j_ack   <= (others => '0');
    j_data  <= (others => '0');
    j_time  <= (others => '0');
    j_tck   <= '0';
    j_tms   <= '0';
    j_tms_lead <= '0';
    j_tdi   <= '0';
  end generate;

  cmd_ready        <= j_ready when jtag_on = '1' else s_ready;
  process (clk)
  begin
    if rising_edge(clk) then
      if jtag_on = '1' then
        rsp_valid        <= j_valid and not rst;
        rsp_ack          <= j_ack;
        rsp_data         <= j_data;
        rsp_parity_error <= '0';
        rsp_time         <= j_time;
      else
        rsp_valid        <= s_valid and not rst;
        rsp_ack          <= s_ack;
        rsp_data         <= s_data;
        rsp_parity_error <= s_parity;
        rsp_time         <= s_time;
      end if;
    end if;
  end process;

  swclk_tck    <= j_tck when jtag_on = '1' else s_clk;
  swclk_tck_oe <= jtag_en or s_clk_oe;
  swdio_tms_o  <= j_tms when jtag_on = '1' else s_dio;
  swdio_tms_lead <= j_tms_lead when jtag_on = '1' else s_dio;
  swdio_tms_oe <= jtag_en or s_dio_oe;
  tdi          <= j_tdi;
  tdi_oe       <= jtag_en;
end architecture;
