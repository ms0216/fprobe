-- テスト専用: ulpi_link + パケット層 + usb_protocol + usb_control_ep（EP0）+ EP1 のバッファ
-- （usb_device と同様に ep_flush で空にする）。
-- 要求ハンドラの I/F はテストベンチが演じる。HS 動作は端子で与える。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.usb_types_pkg.all;

entity usb_control_harness is
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    dir           : in  std_logic;
    nxt           : in  std_logic;
    data_i        : in  std_logic_vector(7 downto 0);
    data_o        : out std_logic_vector(7 downto 0);
    data_oe       : out std_logic;
    stp           : out std_logic;
    high_speed    : in  std_logic;
    address       : out std_logic_vector(6 downto 0);
    config_value  : out std_logic_vector(7 downto 0);
    -- 要求ハンドラ
    req_valid     : out std_logic;
    req_type      : out std_logic_vector(7 downto 0);
    req_request   : out std_logic_vector(7 downto 0);
    req_value     : out std_logic_vector(15 downto 0);
    req_index     : out std_logic_vector(15 downto 0);
    req_length    : out std_logic_vector(15 downto 0);
    req_ack       : in  std_logic;
    req_stall     : in  std_logic;
    req_in_data   : in  std_logic_vector(63 downto 0);
    req_in_length : in  unsigned(3 downto 0);
    req_out_valid : out std_logic;
    req_out_data  : out std_logic_vector(7 downto 0);
    req_out_done  : out std_logic;
    -- EP1 のユーザー側
    ep1_out_valid : out std_logic;
    ep1_out_ready : in  std_logic;
    ep1_out_data  : out std_logic_vector(7 downto 0);
    ep1_out_last  : out std_logic;
    ep1_in_valid  : in  std_logic;
    ep1_in_ready  : out std_logic;
    ep1_in_data   : in  std_logic_vector(7 downto 0);
    ep1_in_last   : in  std_logic
  );
end entity;

architecture rtl of usb_control_harness is
  constant EPS : ep_mask_t := (0 => '1', 1 => '1', others => '0');

  signal rx_active, rx_valid, rx_error : std_logic;
  signal rx_data : std_logic_vector(7 downto 0);
  signal tx_valid, tx_ready, tx_last, tx_aborted : std_logic;
  signal tx_data : std_logic_vector(7 downto 0);
  signal token_valid, data_start, data_valid, data_end, data_ok, handshake_valid : std_logic;
  signal token_pid, data_pid, handshake_pid : pid_t;
  signal token_addr : std_logic_vector(6 downto 0);
  signal token_endp : std_logic_vector(3 downto 0);
  signal data_byte : std_logic_vector(7 downto 0);
  signal data_length : unsigned(10 downto 0);
  signal hs_send, data_send, data_empty, tx_done, tx_pkt_aborted : std_logic;
  signal hs_pid, tx_data_pid : pid_t;
  signal pl_valid, pl_ready, pl_last : std_logic;
  signal pl_data : std_logic_vector(7 downto 0);

  signal out_wr_valid, out_commit, out_discard, out_space : ep_mask_t := (others => '0');
  signal out_wr_data : std_logic_vector(7 downto 0);
  signal out_setup : std_logic;
  signal in_pkt_ready, in_pkt_empty, in_pl_valid, in_pl_last, in_pl_ready, in_ack, in_retry
    : ep_mask_t := (others => '0');
  signal in_pl_data : ep_bytes_t := (others => (others => '0'));
  signal halt_out, halt_in, toggles_out, toggles_in, flush : ep_mask_t;
  signal ep1_rst : std_logic;
  signal addr : std_logic_vector(6 downto 0);
begin
  link_i : entity work.ulpi_link
    generic map (STARTUP_STP_CYCLES => 8)
    port map (clk => clk, rst => rst, dir => dir, nxt => nxt, data_i => data_i,
              data_o => data_o, data_oe => data_oe, stp => stp,
              phy_ready => open, line_state => open, vbus_valid => open,
              rx_active => rx_active, rx_error => rx_error, rx_valid => rx_valid, rx_data => rx_data,
              tx_valid => tx_valid, tx_ready => tx_ready, tx_data => tx_data, tx_last => tx_last,
              tx_aborted => tx_aborted, chirp_k => '0',
              reg_req => '0', reg_we => '0', reg_addr => (others => '0'),
              reg_wdata => (others => '0'), reg_done => open, reg_rdata => open);

  rx_i : entity work.usb_rx_packet
    port map (clk => clk, rst => rst, rx_active => rx_active, rx_valid => rx_valid,
              rx_data => rx_data, rx_error => rx_error,
              token_valid => token_valid, token_pid => token_pid, token_addr => token_addr,
              token_endp => token_endp, sof_valid => open, sof_frame => open,
              data_start => data_start, data_pid => data_pid, data_valid => data_valid,
              data_byte => data_byte, data_end => data_end, data_ok => data_ok,
              data_length => data_length, handshake_valid => handshake_valid,
              handshake_pid => handshake_pid, packet_end => open);

  tx_i : entity work.usb_tx_packet
    port map (clk => clk, rst => rst, hs_send => hs_send, hs_pid => hs_pid,
              data_send => data_send, data_pid => tx_data_pid, data_empty => data_empty,
              busy => open, done => tx_done, aborted => tx_pkt_aborted,
              pl_valid => pl_valid, pl_ready => pl_ready, pl_data => pl_data, pl_last => pl_last,
              tx_valid => tx_valid, tx_ready => tx_ready, tx_data => tx_data, tx_last => tx_last,
              tx_aborted => tx_aborted);

  protocol_i : entity work.usb_protocol
    generic map (OUT_EPS => EPS, IN_EPS => EPS)
    port map (clk => clk, rst => rst, high_speed => high_speed, bus_reset => '0', address => addr,
              token_valid => token_valid, token_pid => token_pid, token_addr => token_addr,
              token_endp => token_endp, data_start => data_start, data_valid => data_valid,
              data_byte => data_byte, data_end => data_end, data_ok => data_ok,
              data_pid => data_pid, data_length => data_length,
              handshake_valid => handshake_valid, handshake_pid => handshake_pid,
              hs_send => hs_send, hs_pid => hs_pid, data_send => data_send,
              tx_data_pid => tx_data_pid, data_empty => data_empty, tx_done => tx_done,
              tx_aborted => tx_pkt_aborted, pl_valid => pl_valid, pl_ready => pl_ready,
              pl_data => pl_data, pl_last => pl_last,
              out_wr_valid => out_wr_valid, out_wr_data => out_wr_data, out_commit => out_commit,
              out_discard => out_discard, out_setup => out_setup, out_space => out_space,
              in_pkt_ready => in_pkt_ready, in_pkt_empty => in_pkt_empty,
              in_pl_valid => in_pl_valid, in_pl_data => in_pl_data, in_pl_last => in_pl_last,
              in_pl_ready => in_pl_ready, in_ack => in_ack, in_retry => in_retry,
              out_halt => halt_out, in_halt => halt_in,
              toggle_reset_out => toggles_out, toggle_reset_in => toggles_in);

  control_i : entity work.usb_control_ep
    port map (clk => clk, rst => rst, high_speed => high_speed, bus_reset => '0',
              wr_valid => out_wr_valid(0), wr_data => out_wr_data, commit => out_commit(0),
              discard => out_discard(0), setup => out_setup, space_for_packet => out_space(0),
              pkt_ready => in_pkt_ready(0), pkt_empty => in_pkt_empty(0),
              pl_valid => in_pl_valid(0), pl_ready => in_pl_ready(0), pl_data => in_pl_data(0),
              pl_last => in_pl_last(0), ack => in_ack(0), retry => in_retry(0),
              address => addr, config_value => config_value,
              halt_out => halt_out, halt_in => halt_in,
              toggle_reset_out => toggles_out, toggle_reset_in => toggles_in, ep_flush => flush,
              req_valid => req_valid, req_type => req_type, req_request => req_request,
              req_value => req_value, req_index => req_index, req_length => req_length,
              req_ack => req_ack, req_stall => req_stall, req_in_data => req_in_data,
              req_in_length => req_in_length, req_out_valid => req_out_valid,
              req_out_data => req_out_data, req_out_done => req_out_done);
  address <= addr;

  ep1_rst <= rst or flush(1);

  ep1_out_i : entity work.usb_ep_out
    generic map (DEPTH => 1024, MPS => MPS_BULK_HS)
    port map (clk => clk, rst => ep1_rst, packet_size => MPS_BULK_HS,
              wr_valid => out_wr_valid(1), wr_data => out_wr_data,
              commit => out_commit(1), discard => out_discard(1), space_for_packet => out_space(1),
              out_valid => ep1_out_valid, out_ready => ep1_out_ready, out_data => ep1_out_data,
              out_last => ep1_out_last);

  ep1_in_i : entity work.usb_ep_in
    generic map (DEPTH => 1024, MPS => MPS_BULK_HS, FLUSH_CYCLES => 16)
    port map (clk => clk, rst => ep1_rst, packet_size => MPS_BULK_HS,
              in_valid => ep1_in_valid, in_ready => ep1_in_ready,
              in_data => ep1_in_data, in_last => ep1_in_last,
              pkt_ready => in_pkt_ready(1), pkt_empty => in_pkt_empty(1),
              pl_valid => in_pl_valid(1), pl_ready => in_pl_ready(1), pl_data => in_pl_data(1),
              pl_last => in_pl_last(1), ack => in_ack(1), retry => in_retry(1));
end architecture;
