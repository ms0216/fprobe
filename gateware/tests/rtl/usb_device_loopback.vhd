-- テスト専用: usb_device の制御チャンネル・CMSIS-DAP・UART をそれぞれループバックし、ストリームに連番を流す。
-- usb_reset の時間定数はシミュレーション用に短縮（HS アイドルによるサスペンド判定は起こらない長さ）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity usb_device_loopback is
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    dir        : in  std_logic;
    nxt        : in  std_logic;
    data_i     : in  std_logic_vector(7 downto 0);
    data_o     : out std_logic_vector(7 downto 0);
    data_oe    : out std_logic;
    stp        : out std_logic;
    stream_on  : in  std_logic;
    swo_valid  : in  std_logic := '0';       -- SWO はテストベンチが直接流す
    swo_ready  : out std_logic;
    swo_data   : in  std_logic_vector(7 downto 0) := x"00";
    swo_flush  : in  std_logic := '0';
    high_speed : out std_logic;
    configured : out std_logic;
    line_rate  : out unsigned(31 downto 0);
    serial_dsr : in  std_logic := '1';       -- CDC-ACM の SERIAL_STATE（DCD は 1 に固定）
    uart_rts   : out std_logic;
    uart_brk   : out std_logic
  );
end entity;

architecture rtl of usb_device_loopback is
  signal c_valid, c_ready, c_last : std_logic;
  signal c_data : std_logic_vector(7 downto 0);
  signal d_valid, d_ready, d_last : std_logic;
  signal d_data : std_logic_vector(7 downto 0);
  signal u_valid, u_ready : std_logic;
  signal u_data : std_logic_vector(7 downto 0);
  signal s_ready : std_logic;
  signal counter : unsigned(7 downto 0) := (others => '0');
begin
  device_i : entity work.usb_device
    generic map (STARTUP_STP_CYCLES => 8, T_DETACH => 50, T_RESET_SE0 => 20, T_CHIRP => 100, T_LINE_STABLE => 10,
                 T_HOST_CHIRP_TIMEOUT => 2000, T_HS_IDLE => 5_000_000, T_SUSPEND_SAMPLE => 50,
                 T_FS_IDLE => 5_000_000, FLUSH_CYCLES => 50)
    port map (clk => clk, rst => rst,
              ulpi_dir => dir, ulpi_nxt => nxt, ulpi_data_i => data_i, ulpi_data_o => data_o,
              ulpi_data_oe => data_oe, ulpi_stp => stp,
              phy_ready => open, high_speed => high_speed, configured => configured,
              suspended => open, vbus_present => open, line_state => open, address => open,
              ctrl_out_valid => c_valid, ctrl_out_ready => c_ready, ctrl_out_data => c_data,
              ctrl_out_last => c_last,
              ctrl_in_valid => c_valid, ctrl_in_ready => c_ready, ctrl_in_data => c_data,
              ctrl_in_last => c_last,
              dap_out_valid => d_valid, dap_out_ready => d_ready, dap_out_data => d_data,
              dap_out_last => d_last,
              dap_in_valid => d_valid, dap_in_ready => d_ready, dap_in_data => d_data,
              dap_in_last => d_last,
              swo_in_valid => swo_valid, swo_in_ready => swo_ready, swo_in_data => swo_data,
              swo_flush => swo_flush,
              stream_in_valid => stream_on, stream_in_ready => s_ready,
              stream_in_data => std_logic_vector(counter), stream_in_last => '0',
              uart_out_valid => u_valid, uart_out_ready => u_ready, uart_out_data => u_data,
              uart_in_valid => u_valid, uart_in_ready => u_ready, uart_in_data => u_data,
              line_rate => line_rate, line_stop_bits => open, line_parity => open,
              line_data_bits => open, line_changed => open, dtr => open, rts => uart_rts,
              brk => uart_brk, serial_dcd => '1', serial_dsr => serial_dsr);

  process (clk)
  begin
    if rising_edge(clk) then
      if stream_on = '1' and s_ready = '1' then
        counter <= counter + 1;
      end if;
    end if;
  end process;
end architecture;
