-- usb_device（usb ドメイン）と fprobe_core（sys ドメイン）の間のクロックの乗せ換えをまとめたもの。ボード非依存。
--
--   CONTROL（EP1）・CMSIS-DAP（EP2）の要求・応答: async_fifo（データ 8 ビット + メッセージ終わり last）。
--   CDC-ACM（EP4）の両方向: async_fifo。ボーレートは cdc_value で sys へ。CTS の状態は usb へ同期化（SERIAL_STATE の DSR）。
--   SWO（EP3）・拡張ストリーム（EP6）: sys → usb の async_fifo。SWO の受信の開始の窓（swo_flush）は usb へ同期化して
--   FIFO の残りを捨てる（B10）。窓（sys で 32 周期）は usb の 2 段の同期化と、FIFO の書き込みポインタの同期化より十分長い。
--   窓の間は swo_receiver が書かないので、窓の終わりの捨てで前の受信のバイトはすべて消える。
--   CMSIS-DAP の 1 パケットの大きさ（High-Speed 512 / Full-Speed 64）: high_speed を sys へ同期化して選ぶ。速度は
--   リセット・ネゴシエーションの間しか変わらない。
-- ポート名は、usb 側が usb_device の、sys 側が fprobe_core のポート名に合わせてある（u_ / s_ の接頭辞）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity usb_core_cdc is
  generic (
    MSG_FIFO_DEPTH    : positive := 512;    -- CONTROL・CMSIS-DAP の要求・応答（2 の冪）
    UART_FIFO_DEPTH   : positive := 512;    -- CDC-ACM（UART ブリッジ）の両方向
    STREAM_FIFO_DEPTH : positive := 2048;   -- SWO・拡張ストリーム（sys → usb）
    HS_PACKET_SIZE    : positive := 512;
    FS_PACKET_SIZE    : positive := 64
  );
  port (
    usb_clk : in std_logic;
    usb_rst : in std_logic;
    sys_clk : in std_logic;
    sys_rst : in std_logic;

    -- usb 側（usb_device）
    u_high_speed     : in  std_logic;
    u_ctrl_out_valid : in  std_logic;
    u_ctrl_out_ready : out std_logic;
    u_ctrl_out_data  : in  std_logic_vector(7 downto 0);
    u_ctrl_out_last  : in  std_logic;
    u_ctrl_in_valid  : out std_logic;
    u_ctrl_in_ready  : in  std_logic;
    u_ctrl_in_data   : out std_logic_vector(7 downto 0);
    u_ctrl_in_last   : out std_logic;
    u_dap_out_valid  : in  std_logic;
    u_dap_out_ready  : out std_logic;
    u_dap_out_data   : in  std_logic_vector(7 downto 0);
    u_dap_out_last   : in  std_logic;
    u_dap_in_valid   : out std_logic;
    u_dap_in_ready   : in  std_logic;
    u_dap_in_data    : out std_logic_vector(7 downto 0);
    u_dap_in_last    : out std_logic;
    u_swo_in_valid   : out std_logic;
    u_swo_in_ready   : in  std_logic;
    u_swo_in_data    : out std_logic_vector(7 downto 0);
    u_swo_flush      : out std_logic;
    u_stream_in_valid : out std_logic;
    u_stream_in_ready : in  std_logic;
    u_stream_in_data  : out std_logic_vector(7 downto 0);
    u_uart_out_valid : in  std_logic;
    u_uart_out_ready : out std_logic;
    u_uart_out_data  : in  std_logic_vector(7 downto 0);
    u_uart_in_valid  : out std_logic;
    u_uart_in_ready  : in  std_logic;
    u_uart_in_data   : out std_logic_vector(7 downto 0);
    u_line_rate      : in  unsigned(31 downto 0);
    u_line_changed   : in  std_logic;
    u_serial_dsr     : out std_logic;           -- CTS の状態（CDC-ACM に CTS のビットは無いので DSR で知らせる）

    -- sys 側（fprobe_core）
    s_packet_size    : out natural range FS_PACKET_SIZE to HS_PACKET_SIZE;
    s_ctrl_in_valid  : out std_logic;
    s_ctrl_in_ready  : in  std_logic;
    s_ctrl_in_data   : out std_logic_vector(7 downto 0);
    s_ctrl_in_last   : out std_logic;
    s_ctrl_out_valid : in  std_logic;
    s_ctrl_out_ready : out std_logic;
    s_ctrl_out_data  : in  std_logic_vector(7 downto 0);
    s_ctrl_out_last  : in  std_logic;
    s_dap_in_valid   : out std_logic;
    s_dap_in_ready   : in  std_logic;
    s_dap_in_data    : out std_logic_vector(7 downto 0);
    s_dap_in_last    : out std_logic;
    s_dap_out_valid  : in  std_logic;
    s_dap_out_ready  : out std_logic;
    s_dap_out_data   : in  std_logic_vector(7 downto 0);
    s_dap_out_last   : in  std_logic;
    s_swo_out_valid  : in  std_logic;
    s_swo_out_ready  : out std_logic;
    s_swo_out_data   : in  std_logic_vector(7 downto 0);
    s_swo_flush      : in  std_logic;
    s_stream_out_valid : in  std_logic;
    s_stream_out_ready : out std_logic;
    s_stream_out_data  : in  std_logic_vector(7 downto 0);
    s_uart_in_valid  : out std_logic;
    s_uart_in_ready  : in  std_logic;
    s_uart_in_data   : out std_logic_vector(7 downto 0);
    s_uart_out_valid : in  std_logic;
    s_uart_out_ready : out std_logic;
    s_uart_out_data  : in  std_logic_vector(7 downto 0);
    s_uart_baud      : out unsigned(31 downto 0);
    s_uart_baud_load : out std_logic;
    s_uart_cts       : in  std_logic
  );
end entity;

architecture rtl of usb_core_cdc is
  constant MSG_WIDTH : positive := 9;       -- データ 8 ビット + メッセージ終わり（last）
  -- メッセージ（data & last）を 1 語にしたもの
  signal u_ctrl_in, u_dap_in, s_ctrl_in, s_dap_in : std_logic_vector(MSG_WIDTH - 1 downto 0);
  signal baud : std_logic_vector(31 downto 0);
  signal hs_s : std_logic;
  signal swo_flush_u : std_logic;                 -- u_swo_flush（出力ポート）の内部の値（出力ポートを内部で読まない）
begin
  -- CONTROL
  ctrl_request_fifo_i : entity work.async_fifo
    generic map (WIDTH => MSG_WIDTH, DEPTH => MSG_FIFO_DEPTH)
    port map (in_clk => usb_clk, in_rst => usb_rst, in_valid => u_ctrl_out_valid, in_ready => u_ctrl_out_ready,
              in_data => u_ctrl_out_last & u_ctrl_out_data,
              out_clk => sys_clk, out_rst => sys_rst, out_valid => s_ctrl_in_valid, out_ready => s_ctrl_in_ready,
              out_data => s_ctrl_in);
  s_ctrl_in_data <= s_ctrl_in(7 downto 0);
  s_ctrl_in_last <= s_ctrl_in(8);

  ctrl_response_fifo_i : entity work.async_fifo
    generic map (WIDTH => MSG_WIDTH, DEPTH => MSG_FIFO_DEPTH)
    port map (in_clk => sys_clk, in_rst => sys_rst, in_valid => s_ctrl_out_valid, in_ready => s_ctrl_out_ready,
              in_data => s_ctrl_out_last & s_ctrl_out_data,
              out_clk => usb_clk, out_rst => usb_rst, out_valid => u_ctrl_in_valid, out_ready => u_ctrl_in_ready,
              out_data => u_ctrl_in);
  u_ctrl_in_data <= u_ctrl_in(7 downto 0);
  u_ctrl_in_last <= u_ctrl_in(8);

  -- CMSIS-DAP
  dap_request_fifo_i : entity work.async_fifo
    generic map (WIDTH => MSG_WIDTH, DEPTH => MSG_FIFO_DEPTH)
    port map (in_clk => usb_clk, in_rst => usb_rst, in_valid => u_dap_out_valid, in_ready => u_dap_out_ready,
              in_data => u_dap_out_last & u_dap_out_data,
              out_clk => sys_clk, out_rst => sys_rst, out_valid => s_dap_in_valid, out_ready => s_dap_in_ready,
              out_data => s_dap_in);
  s_dap_in_data <= s_dap_in(7 downto 0);
  s_dap_in_last <= s_dap_in(8);

  dap_response_fifo_i : entity work.async_fifo
    generic map (WIDTH => MSG_WIDTH, DEPTH => MSG_FIFO_DEPTH)
    port map (in_clk => sys_clk, in_rst => sys_rst, in_valid => s_dap_out_valid, in_ready => s_dap_out_ready,
              in_data => s_dap_out_last & s_dap_out_data,
              out_clk => usb_clk, out_rst => usb_rst, out_valid => u_dap_in_valid, out_ready => u_dap_in_ready,
              out_data => u_dap_in);
  u_dap_in_data <= u_dap_in(7 downto 0);
  u_dap_in_last <= u_dap_in(8);

  hs_sync_i : entity work.sync_bit
    port map (clk => sys_clk, d => u_high_speed, q => hs_s);
  s_packet_size <= HS_PACKET_SIZE when hs_s = '1' else FS_PACKET_SIZE;

  -- CDC-ACM（UART ブリッジ）
  uart_to_sys_i : entity work.async_fifo
    generic map (WIDTH => 8, DEPTH => UART_FIFO_DEPTH)
    port map (in_clk => usb_clk, in_rst => usb_rst, in_valid => u_uart_out_valid, in_ready => u_uart_out_ready,
              in_data => u_uart_out_data,
              out_clk => sys_clk, out_rst => sys_rst, out_valid => s_uart_in_valid, out_ready => s_uart_in_ready,
              out_data => s_uart_in_data);

  uart_to_usb_i : entity work.async_fifo
    generic map (WIDTH => 8, DEPTH => UART_FIFO_DEPTH)
    port map (in_clk => sys_clk, in_rst => sys_rst, in_valid => s_uart_out_valid, in_ready => s_uart_out_ready,
              in_data => s_uart_out_data,
              out_clk => usb_clk, out_rst => usb_rst, out_valid => u_uart_in_valid, out_ready => u_uart_in_ready,
              out_data => u_uart_in_data);

  baud_i : entity work.cdc_value
    generic map (WIDTH => 32)
    port map (src_clk => usb_clk, src_load => u_line_changed, src_value => std_logic_vector(u_line_rate),
              dst_clk => sys_clk, dst_value => baud, dst_changed => s_uart_baud_load);
  s_uart_baud <= unsigned(baud);

  -- CTS の状態を usb ドメインへ（ゆっくり変わる 1 ビット）
  cts_sync_i : entity work.sync_bit
    generic map (INIT => '1')
    port map (clk => usb_clk, d => s_uart_cts, q => u_serial_dsr);

  -- SWO（sys → usb）
  swo_to_usb_i : entity work.async_fifo
    generic map (WIDTH => 8, DEPTH => STREAM_FIFO_DEPTH)
    port map (in_clk => sys_clk, in_rst => sys_rst, in_valid => s_swo_out_valid, in_ready => s_swo_out_ready,
              in_data => s_swo_out_data,
              out_clk => usb_clk, out_rst => usb_rst, out_valid => u_swo_in_valid, out_ready => u_swo_in_ready,
              out_data => u_swo_in_data, out_flush => swo_flush_u);

  swo_flush_sync_i : entity work.sync_bit
    port map (clk => usb_clk, d => s_swo_flush, q => swo_flush_u);
  u_swo_flush <= swo_flush_u;

  -- 拡張ストリーム（sys → usb）
  stream_to_usb_i : entity work.async_fifo
    generic map (WIDTH => 8, DEPTH => STREAM_FIFO_DEPTH)
    port map (in_clk => sys_clk, in_rst => sys_rst, in_valid => s_stream_out_valid, in_ready => s_stream_out_ready,
              in_data => s_stream_out_data,
              out_clk => usb_clk, out_rst => usb_rst, out_valid => u_stream_in_valid, out_ready => u_stream_in_ready,
              out_data => u_stream_in_data);
end architecture;
