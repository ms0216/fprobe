-- 試験用: 端子 → xilinx_la_iserdes（ISERDESE2 の模型）→ la_lane_capture（端から端。PYNQ の fprobe_pynq_fast と同じつなぎ方）。
-- ch 0..CHANNELS − 1 が端子、残りの ch は 0。clk_fast = clk × LANES / 2（同じ時刻に立ち上がる）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.pynq_z2_pkg.all;

entity la_lane_e2e_harness is
  generic (
    WIDTH    : positive := 4;
    CHANNELS : positive := 4;
    LANES    : positive := 6;
    DEPTH    : positive := 32
  );
  port (
    clk_fast      : in  std_logic;
    clk           : in  std_logic;
    rst           : in  std_logic;
    pins          : in  std_logic_vector(CHANNELS - 1 downto 0);
    time          : in  unsigned(63 downto 0);
    ext_trigger   : in  std_logic;
    run           : in  std_logic;
    pretrigger    : in  unsigned(31 downto 0);
    trig_mask     : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_value    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_rise     : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_fall     : in  std_logic_vector(WIDTH - 1 downto 0);
    capturing     : out std_logic;
    triggered     : out std_logic;
    done          : out std_logic;
    start_index   : out unsigned(31 downto 0);
    trigger_index : out unsigned(31 downto 0);
    trigger_time  : out unsigned(63 downto 0);
    trigger_lane  : out unsigned(3 downto 0);
    latency       : out unsigned(7 downto 0);
    rd_addr       : in  unsigned(clog2(DEPTH) - 1 downto 0);
    rd_data       : out std_logic_vector(LANES * WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of la_lane_e2e_harness is
  signal q : std_logic_vector(LANES * CHANNELS - 1 downto 0);
  signal lane_data : std_logic_vector(LANES * WIDTH - 1 downto 0);
begin
  serdes_i : entity work.xilinx_la_iserdes
    generic map (CHANNELS => CHANNELS, LANES => LANES)
    port map (clk_fast => clk_fast, clk_div => clk, rst => rst, pins => pins, q => q);

  process (all)
  begin
    lane_data <= (others => '0');
    for k in 0 to LANES - 1 loop
      lane_data(k * WIDTH + CHANNELS - 1 downto k * WIDTH) <= q((k + 1) * CHANNELS - 1 downto k * CHANNELS);
    end loop;
  end process;

  cap_i : entity work.la_lane_capture
    generic map (WIDTH => WIDTH, LANES => LANES, DEPTH => DEPTH, IN_AGE => iserdes_newest_age(LANES))
    port map (clk => clk, rst => rst, lane_data => lane_data, time => time, ext_trigger => ext_trigger, run => run,
              pretrigger => pretrigger, trig_mask => trig_mask, trig_value => trig_value, trig_rise => trig_rise,
              trig_fall => trig_fall, capturing => capturing, triggered => triggered, done => done,
              start_index => start_index, trigger_index => trigger_index, trigger_time => trigger_time,
              trigger_lane => trigger_lane, latency => latency, rd_addr => rd_addr, rd_data => rd_data);
end architecture;
