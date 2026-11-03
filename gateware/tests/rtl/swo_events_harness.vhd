-- 試験用: SWO の端子 → swo_receiver → event_collector（fprobe_core と同じつなぎ方: swo_fire = out_valid）→ イベント。
-- BYTE_PACK で古い形（種類 5）と詰めた形（種類 15）を切り替え、同じ線の入力で同じバイト・同じ時刻になることを確かめる。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity swo_events_harness is
  generic (BYTE_PACK : boolean := false);
  port (
    clk, rst      : in  std_logic;
    swo           : in  std_logic;
    run           : in  std_logic;
    manchester    : in  std_logic;
    divisor       : in  unsigned(15 downto 0);
    events_enable : in  std_logic;
    swo_valid     : out std_logic;                      -- 受信器がバイトを出した周期
    swo_time      : out unsigned(47 downto 0);          -- そのバイトの時刻（受信器が付けたもの）
    time_count    : out unsigned(47 downto 0);
    event_valid   : out std_logic;
    event_data    : out std_logic_vector(127 downto 0);
    byte_lost     : out unsigned(31 downto 0)
  );
end entity;

architecture sim of swo_events_harness is
  signal count : unsigned(63 downto 0);
  signal held_hi : std_logic_vector(31 downto 0);
  signal rx_valid : std_logic;
  signal rx_data : std_logic_vector(7 downto 0);
  signal rx_time : unsigned(47 downto 0);
  signal flush, error, overrun : std_logic;
begin
  timebase_i : entity work.timebase
    port map (clk => clk, rst => rst, count => count, hold => '0', held_hi => held_hi);
  time_count <= count(47 downto 0);
  swo_valid <= rx_valid;
  swo_time  <= rx_time;

  receiver_i : entity work.swo_receiver
    generic map (HAS_MANCHESTER => true, FLUSH_CYCLES => 32)
    port map (clk => clk, rst => rst, run => run, manchester => manchester, divisor => divisor, swo => swo,
              time => count(47 downto 0),
              out_valid => rx_valid, out_ready => '1', out_data => rx_data, out_time => rx_time, flush => flush, error => error,
              overrun => overrun);

  collector_i : entity work.event_collector
    generic map (BYTE_PACK => BYTE_PACK)
    port map (clk => clk, rst => rst, time_count => count(47 downto 0), events_enable => events_enable,
              ev_valid => '0', host_kind => x"01", ev_parity => '0', ev_request => "0000", ev_ack => "000",
              ev_data => (others => '0'), ev_addr => (others => '0'), ev_time => (others => '0'),
              inj_written => '0', inj_transfers => (others => '0'), inj_value => (others => '0'),
              inj_addr => (others => '0'), inj_time => (others => '0'),
              smp_valid => '0', smp_kind => x"00", smp_word => (others => '0'),
              uart_rx_fire => '0', uart_rx_data => x"00", uart_rx_time => (others => '0'),
              uart_tx_fire => '0', uart_tx_data => x"00",
              swo_fire => rx_valid, swo_data => rx_data, swo_time => rx_time,
              aux_valid => '0', aux_ch => 0, aux_value => (others => '0'), aux_src_time => (others => '0'),
              obs_valid => '0', obs_sync => '0', obs_data => (others => '0'), obs_addr => (others => '0'),
              obs_time => (others => '0'),
              sync_valid => '0', sync_local => (others => '0'), sync_parent => (others => '0'),
              cfg_word => (others => '0'),
              event_valid => event_valid, event_data => event_data, byte_lost => byte_lost);
end architecture;
