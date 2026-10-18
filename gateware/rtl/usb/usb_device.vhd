-- fprobe の USB 2.0 デバイス（ボード非依存、ULPI クロックドメインで動く）。
-- 設計: docs/superpowers/specs/2026-10-15-m2-usb-hs-device-design.md
--
-- 構成（エンドポイント番号は usb_descriptors_pkg の生成定数）:
--   EP0              制御（usb_control_ep + CDC クラス要求ハンドラ）
--   EP_DAP OUT/IN    CMSIS-DAP v2 のコマンド / 応答（dap_out / dap_in。1 パケット = 1 コマンド、応答に ZLP は付けない）
--   EP_SWO IN        CMSIS-DAP の SWO のストリーム（swo_in。バイト列で、パケットの区切りに意味はない）
--   EP_CONTROL OUT/IN  fprobe 拡張の制御チャンネル（ctrl_out / ctrl_in。USB パケット境界を last で示す）
--   EP_STREAM IN       fprobe 拡張のストリーム（stream_in）
--   EP_UART_DATA OUT/IN  CDC-ACM のデータ（uart_out / uart_in）
--   EP_UART_NOTIFY IN  CDC-ACM の通知（SERIAL_STATE。usb_cdc_notify: serial_dcd / serial_dsr が変わるたびに送る）
-- バルクの最大パケットサイズは HS で 512、FS で 64（実行時に切り替わる）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.usb_types_pkg.all;
use work.usb_timing_pkg.all;
use work.usb_descriptors_pkg.all;

entity usb_device is
  generic (
    STARTUP_STP_CYCLES   : positive := 64;
    T_DETACH             : natural := work.usb_timing_pkg.T_DETACH;
    T_RESET_SE0          : positive := work.usb_timing_pkg.T_RESET_SE0;
    T_CHIRP              : positive := work.usb_timing_pkg.T_CHIRP;
    T_LINE_STABLE        : positive := work.usb_timing_pkg.T_LINE_STABLE;
    T_HOST_CHIRP_TIMEOUT : positive := work.usb_timing_pkg.T_HOST_CHIRP_TIMEOUT;
    T_HS_IDLE            : positive := work.usb_timing_pkg.T_HS_IDLE;
    T_SUSPEND_SAMPLE     : positive := work.usb_timing_pkg.T_SUSPEND_SAMPLE;
    T_FS_IDLE            : positive := work.usb_timing_pkg.T_FS_IDLE;
    CTRL_DEPTH           : positive := 1024;     -- バイト（2 の冪）
    STREAM_DEPTH         : positive := 2048;
    UART_DEPTH           : positive := 1024;
    DAP_DEPTH            : positive := 2048;     -- 応答 4 つ（Packet Count）を溜められること
    SWO_DEPTH            : positive := 2048;
    FLUSH_CYCLES         : positive := 600;      -- IN の小休止判定（60 MHz で 10 us）
    CLK_HZ               : positive := 60_000_000   -- clk の周波数（CDC-ACM のブレークの長さの ms を数える）
  );
  port (
    clk             : in  std_logic;
    rst             : in  std_logic;
    -- ULPI
    ulpi_dir        : in  std_logic;
    ulpi_nxt        : in  std_logic;
    ulpi_data_i     : in  std_logic_vector(7 downto 0);
    ulpi_data_o     : out std_logic_vector(7 downto 0);
    ulpi_data_oe    : out std_logic;
    ulpi_stp        : out std_logic;
    -- 状態
    phy_ready       : out std_logic;
    high_speed      : out std_logic;
    configured      : out std_logic;
    suspended       : out std_logic;
    vbus_present    : out std_logic;
    line_state      : out std_logic_vector(1 downto 0);
    address         : out std_logic_vector(6 downto 0);
    -- fprobe 拡張: 制御チャンネル
    ctrl_out_valid  : out std_logic;
    ctrl_out_ready  : in  std_logic;
    ctrl_out_data   : out std_logic_vector(7 downto 0);
    ctrl_out_last   : out std_logic;
    ctrl_in_valid   : in  std_logic;
    ctrl_in_ready   : out std_logic;
    ctrl_in_data    : in  std_logic_vector(7 downto 0);
    ctrl_in_last    : in  std_logic;
    -- CMSIS-DAP
    dap_out_valid   : out std_logic;
    dap_out_ready   : in  std_logic;
    dap_out_data    : out std_logic_vector(7 downto 0);
    dap_out_last    : out std_logic;
    dap_in_valid    : in  std_logic;
    dap_in_ready    : out std_logic;
    dap_in_data     : in  std_logic_vector(7 downto 0);
    dap_in_last     : in  std_logic;
    swo_in_valid    : in  std_logic;
    swo_in_ready    : out std_logic;
    swo_in_data     : in  std_logic_vector(7 downto 0);
    -- SWO の残りを捨てる要求（B10: 受信の開始）。1 の間と、その後の最初のトランザクションの合間まで swo_in を受けず、
    -- 合間ごとに SWO のバッファを空にする（送り途中・ACK 待ちのパケットは壊さない）
    swo_flush       : in  std_logic := '0';
    -- fprobe 拡張: ストリーム
    stream_in_valid : in  std_logic;
    stream_in_ready : out std_logic;
    stream_in_data  : in  std_logic_vector(7 downto 0);
    stream_in_last  : in  std_logic;
    -- CDC-ACM
    uart_out_valid  : out std_logic;
    uart_out_ready  : in  std_logic;
    uart_out_data   : out std_logic_vector(7 downto 0);
    uart_in_valid   : in  std_logic;
    uart_in_ready   : out std_logic;
    uart_in_data    : in  std_logic_vector(7 downto 0);
    line_rate       : out unsigned(31 downto 0);
    line_stop_bits  : out unsigned(7 downto 0);
    line_parity     : out unsigned(7 downto 0);
    line_data_bits  : out unsigned(7 downto 0);
    line_changed    : out std_logic;
    dtr             : out std_logic;
    rts             : out std_logic;
    brk             : out std_logic;                 -- ブレークを送る（SEND_BREAK。長さは中で数える）
    serial_dcd      : in  std_logic := '1';          -- SERIAL_STATE で知らせる状態（このクロックのドメインで）
    serial_dsr      : in  std_logic := '1'
  );
end entity;

architecture rtl of usb_device is
  -- ulpi_link
  signal link_ready, vbus_valid, rx_active, rx_valid, rx_error : std_logic;
  signal line : std_logic_vector(1 downto 0);
  signal rx_data : std_logic_vector(7 downto 0);
  signal tx_valid, tx_ready, tx_last, tx_aborted, chirp_k : std_logic;
  signal tx_data : std_logic_vector(7 downto 0);
  signal reg_req, reg_we, reg_done : std_logic;
  signal reg_addr : std_logic_vector(5 downto 0);
  signal reg_wdata : std_logic_vector(7 downto 0);
  -- usb_reset
  signal bus_reset, hs : std_logic;
  -- パケット層
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
  -- エンドポイント配列
  signal out_wr_valid, out_commit, out_discard : ep_mask_t;
  signal out_space : ep_mask_t;
  signal out_wr_data : std_logic_vector(7 downto 0);
  signal out_setup : std_logic;
  signal in_pkt_ready, in_pkt_empty, in_pl_valid, in_pl_last : ep_mask_t;
  signal in_pl_ready, in_ack, in_retry : ep_mask_t;
  signal in_pl_data : ep_bytes_t;
  signal halt_out, halt_in, toggle_reset_out, toggle_reset_in : ep_mask_t;
  signal ep_flush, ep_rst : ep_mask_t;
  signal protocol_idle, swo_pending, swo_clear, swo_rst, swo_accept_valid, swo_accept_ready : std_logic;
  signal device_address : std_logic_vector(6 downto 0);
  signal config_value : std_logic_vector(7 downto 0);
  signal bulk_size : natural range 1 to MPS_BULK_HS;
  -- 要求ハンドラ
  signal req_valid, req_ack, req_stall, req_out_valid, req_out_done : std_logic;
  signal req_type, req_request, req_out_data : std_logic_vector(7 downto 0);
  signal req_value, req_index, req_length : std_logic_vector(15 downto 0);
  signal req_in_data : std_logic_vector(63 downto 0);
  signal req_in_length : unsigned(3 downto 0);
begin
  bulk_size <= MPS_BULK_HS when hs = '1' else MPS_BULK_FS;

  link_i : entity work.ulpi_link
    generic map (STARTUP_STP_CYCLES => STARTUP_STP_CYCLES)
    port map (clk => clk, rst => rst, dir => ulpi_dir, nxt => ulpi_nxt, data_i => ulpi_data_i,
              data_o => ulpi_data_o, data_oe => ulpi_data_oe, stp => ulpi_stp,
              phy_ready => link_ready, line_state => line, vbus_valid => vbus_valid,
              rx_active => rx_active, rx_error => rx_error, rx_valid => rx_valid, rx_data => rx_data,
              tx_valid => tx_valid, tx_ready => tx_ready, tx_data => tx_data, tx_last => tx_last,
              tx_aborted => tx_aborted, chirp_k => chirp_k,
              reg_req => reg_req, reg_we => reg_we, reg_addr => reg_addr, reg_wdata => reg_wdata,
              reg_done => reg_done, reg_rdata => open);

  reset_i : entity work.usb_reset
    generic map (T_DETACH => T_DETACH, T_RESET_SE0 => T_RESET_SE0, T_CHIRP => T_CHIRP, T_LINE_STABLE => T_LINE_STABLE,
                 T_HOST_CHIRP_TIMEOUT => T_HOST_CHIRP_TIMEOUT, T_HS_IDLE => T_HS_IDLE,
                 T_SUSPEND_SAMPLE => T_SUSPEND_SAMPLE, T_FS_IDLE => T_FS_IDLE)
    port map (clk => clk, rst => rst, link_ready => link_ready, line_state => line,
              vbus_valid => vbus_valid, reg_req => reg_req, reg_we => reg_we, reg_addr => reg_addr,
              reg_wdata => reg_wdata, reg_done => reg_done, chirp_k => chirp_k,
              bus_reset => bus_reset, high_speed => hs, suspended => suspended);

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
    generic map (OUT_EPS => OUT_ENDPOINTS, IN_EPS => IN_ENDPOINTS)
    port map (clk => clk, rst => rst, high_speed => hs, bus_reset => bus_reset,
              address => device_address,
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
              toggle_reset_out => toggle_reset_out, toggle_reset_in => toggle_reset_in,
              idle => protocol_idle);

  -- EP0
  control_i : entity work.usb_control_ep
    port map (clk => clk, rst => rst, high_speed => hs, bus_reset => bus_reset,
              wr_valid => out_wr_valid(0), wr_data => out_wr_data, commit => out_commit(0),
              discard => out_discard(0), setup => out_setup, space_for_packet => out_space(0),
              pkt_ready => in_pkt_ready(0), pkt_empty => in_pkt_empty(0),
              pl_valid => in_pl_valid(0), pl_ready => in_pl_ready(0), pl_data => in_pl_data(0),
              pl_last => in_pl_last(0), ack => in_ack(0), retry => in_retry(0),
              address => device_address, config_value => config_value,
              halt_out => halt_out, halt_in => halt_in,
              toggle_reset_out => toggle_reset_out, toggle_reset_in => toggle_reset_in,
              ep_flush => ep_flush,
              req_valid => req_valid, req_type => req_type, req_request => req_request,
              req_value => req_value, req_index => req_index, req_length => req_length,
              req_ack => req_ack, req_stall => req_stall, req_in_data => req_in_data,
              req_in_length => req_in_length, req_out_valid => req_out_valid,
              req_out_data => req_out_data, req_out_done => req_out_done);

  cdc_requests_i : entity work.usb_cdc_acm_requests
    generic map (COMM_INTERFACE => CDC_COMM_INTERFACE, CLK_HZ => CLK_HZ)
    port map (clk => clk, rst => rst,
              req_valid => req_valid, req_type => req_type, req_request => req_request,
              req_value => req_value, req_index => req_index, req_length => req_length,
              req_ack => req_ack, req_stall => req_stall, req_in_data => req_in_data,
              req_in_length => req_in_length, req_out_valid => req_out_valid,
              req_out_data => req_out_data, req_out_done => req_out_done,
              line_rate => line_rate, line_stop_bits => line_stop_bits, line_parity => line_parity,
              line_data_bits => line_data_bits, line_changed => line_changed, dtr => dtr, rts => rts,
              brk => brk);

  -- エンドポイントのバッファはバスリセットと再構成（SET_CONFIGURATION / SET_INTERFACE）で空にする
  ep_rst <= (others => '1') when rst = '1' or bus_reset = '1' else ep_flush;

  -- fprobe 拡張: 制御チャンネル
  ctrl_out_i : entity work.usb_ep_out
    generic map (DEPTH => CTRL_DEPTH, MPS => MPS_BULK_HS)
    port map (clk => clk, rst => ep_rst(EP_CONTROL), packet_size => bulk_size,
              wr_valid => out_wr_valid(EP_CONTROL), wr_data => out_wr_data,
              commit => out_commit(EP_CONTROL), discard => out_discard(EP_CONTROL),
              space_for_packet => out_space(EP_CONTROL),
              out_valid => ctrl_out_valid, out_ready => ctrl_out_ready, out_data => ctrl_out_data,
              out_last => ctrl_out_last);

  ctrl_in_i : entity work.usb_ep_in
    generic map (DEPTH => CTRL_DEPTH, MPS => MPS_BULK_HS, FLUSH_CYCLES => FLUSH_CYCLES)
    port map (clk => clk, rst => ep_rst(EP_CONTROL), packet_size => bulk_size,
              in_valid => ctrl_in_valid, in_ready => ctrl_in_ready, in_data => ctrl_in_data,
              in_last => ctrl_in_last,
              pkt_ready => in_pkt_ready(EP_CONTROL), pkt_empty => in_pkt_empty(EP_CONTROL),
              pl_valid => in_pl_valid(EP_CONTROL), pl_ready => in_pl_ready(EP_CONTROL),
              pl_data => in_pl_data(EP_CONTROL), pl_last => in_pl_last(EP_CONTROL),
              ack => in_ack(EP_CONTROL), retry => in_retry(EP_CONTROL));

  -- CMSIS-DAP
  dap_out_i : entity work.usb_ep_out
    generic map (DEPTH => DAP_DEPTH, MPS => MPS_BULK_HS)
    port map (clk => clk, rst => ep_rst(EP_DAP), packet_size => bulk_size,
              wr_valid => out_wr_valid(EP_DAP), wr_data => out_wr_data,
              commit => out_commit(EP_DAP), discard => out_discard(EP_DAP),
              space_for_packet => out_space(EP_DAP),
              out_valid => dap_out_valid, out_ready => dap_out_ready, out_data => dap_out_data,
              out_last => dap_out_last);

  dap_in_i : entity work.usb_ep_in
    generic map (DEPTH => DAP_DEPTH, MPS => MPS_BULK_HS, FLUSH_CYCLES => FLUSH_CYCLES, ZLP => false)
    port map (clk => clk, rst => ep_rst(EP_DAP), packet_size => bulk_size,
              in_valid => dap_in_valid, in_ready => dap_in_ready, in_data => dap_in_data,
              in_last => dap_in_last,
              pkt_ready => in_pkt_ready(EP_DAP), pkt_empty => in_pkt_empty(EP_DAP),
              pl_valid => in_pl_valid(EP_DAP), pl_ready => in_pl_ready(EP_DAP),
              pl_data => in_pl_data(EP_DAP), pl_last => in_pl_last(EP_DAP),
              ack => in_ack(EP_DAP), retry => in_retry(EP_DAP));

  -- SWO の残りを捨てる（B10）: 要求の間とその後の最初の合間まで新しいバイトを受けず、トランザクションの合間に
  -- バッファを空にする。空にした後の最初のパケットは、前に ACK を落とされたパケットと同じトグルで出ることがあり、
  -- そのときホストは再送とみなして捨てる（受信の開始が 1 パケット分遅れたのと同じ）
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' or bus_reset = '1' then
        swo_pending <= '0';
      elsif swo_flush = '1' then
        swo_pending <= '1';
      elsif swo_clear = '1' then
        swo_pending <= '0';
      end if;
    end if;
  end process;
  swo_clear        <= swo_pending and protocol_idle;
  swo_rst          <= ep_rst(EP_SWO) or swo_clear;
  swo_accept_valid <= swo_in_valid and not swo_pending;
  swo_in_ready     <= swo_accept_ready and not swo_pending;

  swo_in_i : entity work.usb_ep_in
    generic map (DEPTH => SWO_DEPTH, MPS => MPS_BULK_HS, FLUSH_CYCLES => FLUSH_CYCLES)
    port map (clk => clk, rst => swo_rst, packet_size => bulk_size,
              in_valid => swo_accept_valid, in_ready => swo_accept_ready, in_data => swo_in_data,
              in_last => '0',
              pkt_ready => in_pkt_ready(EP_SWO), pkt_empty => in_pkt_empty(EP_SWO),
              pl_valid => in_pl_valid(EP_SWO), pl_ready => in_pl_ready(EP_SWO),
              pl_data => in_pl_data(EP_SWO), pl_last => in_pl_last(EP_SWO),
              ack => in_ack(EP_SWO), retry => in_retry(EP_SWO));

  -- fprobe 拡張: ストリーム
  stream_in_i : entity work.usb_ep_in
    generic map (DEPTH => STREAM_DEPTH, MPS => MPS_BULK_HS, FLUSH_CYCLES => FLUSH_CYCLES)
    port map (clk => clk, rst => ep_rst(EP_STREAM), packet_size => bulk_size,
              in_valid => stream_in_valid, in_ready => stream_in_ready, in_data => stream_in_data,
              in_last => stream_in_last,
              pkt_ready => in_pkt_ready(EP_STREAM), pkt_empty => in_pkt_empty(EP_STREAM),
              pl_valid => in_pl_valid(EP_STREAM), pl_ready => in_pl_ready(EP_STREAM),
              pl_data => in_pl_data(EP_STREAM), pl_last => in_pl_last(EP_STREAM),
              ack => in_ack(EP_STREAM), retry => in_retry(EP_STREAM));

  -- CDC-ACM データ
  uart_out_i : entity work.usb_ep_out
    generic map (DEPTH => UART_DEPTH, MPS => MPS_BULK_HS)
    port map (clk => clk, rst => ep_rst(EP_UART_DATA), packet_size => bulk_size,
              wr_valid => out_wr_valid(EP_UART_DATA), wr_data => out_wr_data,
              commit => out_commit(EP_UART_DATA), discard => out_discard(EP_UART_DATA),
              space_for_packet => out_space(EP_UART_DATA),
              out_valid => uart_out_valid, out_ready => uart_out_ready, out_data => uart_out_data,
              out_last => open);

  uart_in_i : entity work.usb_ep_in
    generic map (DEPTH => UART_DEPTH, MPS => MPS_BULK_HS, FLUSH_CYCLES => FLUSH_CYCLES)
    port map (clk => clk, rst => ep_rst(EP_UART_DATA), packet_size => bulk_size,
              in_valid => uart_in_valid, in_ready => uart_in_ready, in_data => uart_in_data,
              in_last => '0',
              pkt_ready => in_pkt_ready(EP_UART_DATA), pkt_empty => in_pkt_empty(EP_UART_DATA),
              pl_valid => in_pl_valid(EP_UART_DATA), pl_ready => in_pl_ready(EP_UART_DATA),
              pl_data => in_pl_data(EP_UART_DATA), pl_last => in_pl_last(EP_UART_DATA),
              ack => in_ack(EP_UART_DATA), retry => in_retry(EP_UART_DATA));

  -- CDC-ACM の通知
  uart_notify_i : entity work.usb_cdc_notify
    generic map (COMM_INTERFACE => CDC_COMM_INTERFACE)
    port map (clk => clk, rst => ep_rst(EP_UART_NOTIFY), dcd => serial_dcd, dsr => serial_dsr,
              pkt_ready => in_pkt_ready(EP_UART_NOTIFY), pkt_empty => in_pkt_empty(EP_UART_NOTIFY),
              pl_valid => in_pl_valid(EP_UART_NOTIFY), pl_ready => in_pl_ready(EP_UART_NOTIFY),
              pl_data => in_pl_data(EP_UART_NOTIFY), pl_last => in_pl_last(EP_UART_NOTIFY),
              ack => in_ack(EP_UART_NOTIFY), retry => in_retry(EP_UART_NOTIFY));

  -- 使わないエンドポイントは「送るものが無い・受け取れない」を明示的に返す（常に NAK）。
  -- 宣言の初期値は合成で無視され、駆動されない配線は実機で値が定まらない
  unused_g : for ep in 0 to NUM_ENDPOINTS - 1 generate
    no_out_g : if ep /= 0 and ep /= EP_CONTROL and ep /= EP_DAP and ep /= EP_UART_DATA generate
      out_space(ep) <= '0';
    end generate;
    no_in_g : if ep /= 0 and ep /= EP_CONTROL and ep /= EP_DAP and ep /= EP_SWO and ep /= EP_STREAM
                 and ep /= EP_UART_DATA and ep /= EP_UART_NOTIFY generate
      in_pkt_ready(ep) <= '0';
      in_pkt_empty(ep) <= '0';
      in_pl_valid(ep)  <= '0';
      in_pl_last(ep)   <= '0';
      in_pl_data(ep)   <= (others => '0');
    end generate;
  end generate;

  phy_ready  <= link_ready;
  high_speed <= hs;
  configured <= '1' when config_value /= x"00" else '0';
  vbus_present <= vbus_valid;
  line_state <= line;
  address    <= device_address;
end architecture;
