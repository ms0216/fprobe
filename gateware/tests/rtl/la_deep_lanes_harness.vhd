-- 試験用: 端子 → xilinx_la_iserdes（ISERDESE2 の模型）→ la_deep_buffer（LANES > 1、PS が DDR のリングを読む垂れ流し）の端から端
-- （PYNQ の fprobe_pynq_deep_fast と同じつなぎ方。mem_clk = clk）。ch 0..CHANNELS − 1 が端子、残りの ch は 0。
-- clk_fast = clk × LANES / 2（同じ時刻に立ち上がる）。メモリ（mem_port）と PS の役はテストベンチ。PS の読み終えた位置
-- ps_la_consumed は、いまの流れの値として渡す（テストベンチは流れを始める前に 0 にする。窓の la_ps_port の代わり）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.pynq_z2_pkg.all;

entity la_deep_lanes_harness is
  generic (
    CHANNELS  : positive := 4;
    LANES     : positive := 8;
    ADDR_BITS : positive := 10
  );
  port (
    clk_fast       : in  std_logic;
    clk            : in  std_logic;
    rst            : in  std_logic;
    pins           : in  std_logic_vector(CHANNELS - 1 downto 0);
    time           : in  unsigned(63 downto 0);
    run            : in  std_logic;
    rle            : in  std_logic;
    step           : in  unsigned(1 downto 0);
    pack           : in  std_logic_vector(2 downto 0);
    first_ch       : in  unsigned(3 downto 0);
    rate           : in  unsigned(15 downto 0);
    ps_la_consumed : in  unsigned(31 downto 0);
    ps_la_written  : out unsigned(31 downto 0);
    overrun        : out std_logic;
    reading        : out std_logic;
    start_time     : out unsigned(63 downto 0);
    start_valid    : out std_logic;
    latency        : out unsigned(7 downto 0);
    stat_written   : out unsigned(31 downto 0);
    stat_unread    : out unsigned(31 downto 0);
    stat_peak      : out unsigned(31 downto 0);
    cmd_valid      : out std_logic;
    cmd_ready      : in  std_logic;
    cmd_write      : out std_logic;
    cmd_addr       : out unsigned(ADDR_BITS - 1 downto 0);
    cmd_data       : out std_logic_vector(127 downto 0);
    rd_valid       : in  std_logic;
    rd_data        : in  std_logic_vector(127 downto 0)
  );
end entity;

architecture rtl of la_deep_lanes_harness is
  constant WIDTH : positive := 16;
  signal q : std_logic_vector(LANES * CHANNELS - 1 downto 0);
  signal lane_data : std_logic_vector(LANES * WIDTH - 1 downto 0);
  signal zero16 : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal epoch : unsigned(15 downto 0);
begin
  serdes_i : entity work.xilinx_la_iserdes
    generic map (CHANNELS => CHANNELS, LANES => LANES)
    port map (clk_fast => clk_fast, clk_div => clk, rst => rst, pins => pins, q => q, o => open);

  process (all)
  begin
    lane_data <= (others => '0');
    for k in 0 to LANES - 1 loop
      lane_data(k * WIDTH + CHANNELS - 1 downto k * WIDTH) <= q((k + 1) * CHANNELS - 1 downto k * CHANNELS);
    end loop;
  end process;

  deep_i : entity work.la_deep_buffer
    generic map (WIDTH => WIDTH, ADDR_BITS => ADDR_BITS, STREAM_EVENTS => true, LANES => LANES,
                 IN_AGE => iserdes_newest_age(LANES), SAME_CLOCK => true)
    port map (clk => clk, rst => rst, probes => zero16, lane_data => lane_data, step => step, first_ch => first_ch, ext_trigger => '0',
              time => time, rate => rate, trig_mask => zero16, trig_value => zero16, trig_rise => zero16, trig_fall => zero16,
              run => run, rle => rle, pack => pack, stream => '1', ps_read => '1', ps_epoch => epoch,
              ps_ret_epoch => epoch, ps_la_consumed => ps_la_consumed,
              ps_pub_epoch => open, ps_active => open, ps_writing => open, ps_overrun => open,
              ps_la_written => ps_la_written, ps_la_wptr => open, ps_ev_wpos => open, ps_ev_start => open,
              post_beats => (others => '0'), pretrigger => (others => '0'),
              capturing => open, triggered => open, done => open, overrun => overrun, trigger_beat => open,
              trigger_slot => open, written => open, trigger_time => open, stream_beats => open,
              stat_written => stat_written, stat_unread => stat_unread, stat_peak => stat_peak,
              start_time => start_time, start_valid => start_valid, latency => latency,
              ev_enable => '0', ev_valid => '0', ev_data => (others => '0'), ev_written => open, ev_lost => open,
              la_beats => open, ev_beats => open, read => '0', ps_rd_epoch => open, ps_rd_region => open,
              ps_rd_first => open, ps_rd_count => open, read_region => '0', read_first => (others => '0'),
              read_count => (others => '0'), reading => reading, out_valid => open, out_ready => '1', out_data => open,
              mem_clk => clk, mem_rst => rst, mem_active => open, cmd_valid => cmd_valid, cmd_ready => cmd_ready,
              cmd_write => cmd_write, cmd_addr => cmd_addr, cmd_data => cmd_data, rd_valid => rd_valid, rd_data => rd_data);
end architecture;
