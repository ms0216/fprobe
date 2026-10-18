-- Tang Primer 20K Dock 用 USB 試験デザイン（M2）。USB-OTG ポート（USB3317）を PC につなぐ。
--
--   usb ドメイン: ulpi_clk（USB3317 の 60 MHz CLKOUT）。usb_device を動かす（I/O タイミングは ulpi_link が担う）。
--   sys ドメイン: clk27 → rPLL → sys_clk（100 MHz）。
--   * 制御チャンネル（EP1）: usb → async_fifo → sys（usbtest_responder）→ async_fifo → usb
--     メッセージ先頭バイト 0x00 は折り返し、それ以外は破棄（OUT スループット計測用）。クロック乗せ換えの実機検証も兼ねる
--   * CMSIS-DAP の EP（EP2）: usb ドメインで直接折り返す（パケット単位、ZLP 無し）
--   * UART チャンネル（EP4）: usb ドメインで直接折り返す
--   * ストリーム（EP6）: usb ドメインの連番生成（IN スループット計測用、常に送出可能）
--   LED（アクティブロー）: led(0)=PHY 準備完了, led(1)=High-Speed, led(2)=構成済み, led(3)=サスペンド,
--                          led(4)=sys 心拍, led(5)=消灯
--   診断: 0.5 秒ごとに BL702 の仮想 COM（115200 baud）へ状態を 1 行報告する（sys ドメイン）。
--     "usb clk=01 rdy=01 vbus=01 ls=01 hs=01 cfg=01 sus=00 adr=07"
--     clk は ULPI クロックが動いているか。各値は 2 段 FF で同期化した表示用の値（多ビットの adr は
--     変化の瞬間に崩れうるが、診断表示なので許容する）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.primer20k_pkg.all;
use work.text_report_pkg.all;

entity primer20k_usbtest_top is
  port (
    clk27     : in    std_logic;
    ulpi_clk  : in    std_logic;
    ulpi_dir  : in    std_logic;
    ulpi_nxt  : in    std_logic;
    ulpi_stp  : out   std_logic;
    ulpi_data : inout std_logic_vector(7 downto 0);
    ulpi_rst  : out   std_logic;
    uart_tx   : out   std_logic;
    led       : out   std_logic_vector(5 downto 0)
  );
end entity;

architecture rtl of primer20k_usbtest_top is
  signal usb_clk, usb_rst, phy_released : std_logic;
  signal sys_clk, sys_rst, pll_lock     : std_logic;
  signal data_o  : std_logic_vector(7 downto 0);
  signal data_oe : std_logic;
  signal phy_ready, high_speed, configured, suspended, vbus_present : std_logic;
  signal line_state : std_logic_vector(1 downto 0);
  signal address    : std_logic_vector(6 downto 0);

  -- 制御チャンネル（usb ⇄ sys）
  signal c_out_valid, c_out_ready, c_out_last, c_in_valid, c_in_ready, c_in_last : std_logic;
  signal c_out_data, c_in_data : std_logic_vector(7 downto 0);
  signal s_valid, s_ready, r_valid, r_ready : std_logic;
  signal s_data, r_data : std_logic_vector(8 downto 0);
  signal back_data : std_logic_vector(8 downto 0);
  -- CMSIS-DAP の EP（usb ドメインで折り返し）
  signal d_valid, d_ready, d_last : std_logic;
  signal d_data : std_logic_vector(7 downto 0);
  -- UART チャンネル（usb ドメインで折り返し）
  signal u_valid, u_ready : std_logic;
  signal u_data : std_logic_vector(7 downto 0);
  -- ストリーム
  signal stream_ready : std_logic;
  signal counter      : unsigned(7 downto 0) := (others => '0');
  signal heartbeat    : unsigned(25 downto 0) := (others => '0');
  -- 診断報告（sys ドメイン）
  constant STATUS : template_t :=
    lit("usb clk=") & hex(0) & lit(" rdy=") & hex(1) & lit(" vbus=") & hex(2) & lit(" ls=") & hex(3) &
    lit(" hs=") & hex(4) & lit(" cfg=") & hex(5) & lit(" sus=") & hex(6) & lit(" adr=") & hex(7) &
    lit(CR & LF);
  constant STATUS_BITS : natural := 15;   -- 同期化する usb ドメインの状態ビット数
  signal usb_alive    : std_logic := '0';
  signal status_usb   : std_logic_vector(STATUS_BITS - 1 downto 0);
  signal status_s1    : std_logic_vector(STATUS_BITS - 1 downto 0) := (others => '0');
  signal status_sys   : std_logic_vector(STATUS_BITS - 1 downto 0) := (others => '0');
  signal alive_prev   : std_logic := '0';
  signal alive_seen   : std_logic := '0';
  signal clk_ok       : std_logic := '0';
  signal fields       : fields_t;
  signal report_start : std_logic := '0';
  signal tx_valid, tx_ready : std_logic;
  signal tx_byte      : std_logic_vector(7 downto 0);
begin
  usb_clk <= ulpi_clk;

  phy_reset_i : entity work.primer20k_ulpi_reset
    port map (clk27 => clk27, resetb => ulpi_rst, released => phy_released);
  usb_reset_i : entity work.reset_sync
    port map (clk => usb_clk, arst_in => not phy_released, rst => usb_rst);

  pll_i : entity work.gowin_rpll
    generic map (FCLKIN_MHZ => "27", DEVICE => "GW2A-18C",
                 IDIV_SEL => PLL_IDIV_SEL, FBDIV_SEL => PLL_FBDIV_SEL, ODIV_SEL => PLL_ODIV_SEL)
    port map (clkin => clk27, clkout => sys_clk, lock => pll_lock);
  sys_reset_i : entity work.reset_sync
    port map (clk => sys_clk, arst_in => not pll_lock, rst => sys_rst);

  ulpi_data <= data_o when data_oe = '1' else (others => 'Z');

  device_i : entity work.usb_device
    port map (clk => usb_clk, rst => usb_rst,
              ulpi_dir => ulpi_dir, ulpi_nxt => ulpi_nxt, ulpi_data_i => ulpi_data,
              ulpi_data_o => data_o, ulpi_data_oe => data_oe, ulpi_stp => ulpi_stp,
              phy_ready => phy_ready, high_speed => high_speed, configured => configured,
              suspended => suspended, vbus_present => vbus_present, line_state => line_state,
              address => address,
              ctrl_out_valid => c_out_valid, ctrl_out_ready => c_out_ready,
              ctrl_out_data => c_out_data, ctrl_out_last => c_out_last,
              ctrl_in_valid => c_in_valid, ctrl_in_ready => c_in_ready,
              ctrl_in_data => c_in_data, ctrl_in_last => c_in_last,
              dap_out_valid => d_valid, dap_out_ready => d_ready, dap_out_data => d_data,
              dap_out_last => d_last,
              dap_in_valid => d_valid, dap_in_ready => d_ready, dap_in_data => d_data,
              dap_in_last => d_last,
              swo_in_valid => '0', swo_in_ready => open, swo_in_data => x"00",
              stream_in_valid => '1', stream_in_ready => stream_ready,
              stream_in_data => std_logic_vector(counter), stream_in_last => '0',
              uart_out_valid => u_valid, uart_out_ready => u_ready, uart_out_data => u_data,
              uart_in_valid => u_valid, uart_in_ready => u_ready, uart_in_data => u_data,
              line_rate => open, line_stop_bits => open, line_parity => open,
              line_data_bits => open, line_changed => open, dtr => open, rts => open);

  -- 制御チャンネルを sys ドメインまで往復させる（データ 8 ビット + last）
  to_sys_i : entity work.async_fifo
    generic map (WIDTH => 9, DEPTH => 512)
    port map (in_clk => usb_clk, in_rst => usb_rst, in_valid => c_out_valid,
              in_ready => c_out_ready, in_data => c_out_last & c_out_data,
              out_clk => sys_clk, out_rst => sys_rst, out_valid => s_valid, out_ready => s_ready,
              out_data => s_data);

  responder_i : entity work.usbtest_responder
    port map (clk => sys_clk, rst => sys_rst, in_valid => s_valid, in_ready => s_ready,
              in_data => s_data, out_valid => r_valid, out_ready => r_ready, out_data => r_data);

  to_usb_i : entity work.async_fifo
    generic map (WIDTH => 9, DEPTH => 512)
    port map (in_clk => sys_clk, in_rst => sys_rst, in_valid => r_valid, in_ready => r_ready,
              in_data => r_data,
              out_clk => usb_clk, out_rst => usb_rst, out_valid => c_in_valid,
              out_ready => c_in_ready, out_data => back_data);
  c_in_data <= back_data(7 downto 0);
  c_in_last <= back_data(8);

  process (usb_clk)
  begin
    if rising_edge(usb_clk) then
      usb_alive <= not usb_alive;
      if stream_ready = '1' then
        counter <= counter + 1;
      end if;
    end if;
  end process;

  status_usb <= usb_alive & phy_ready & vbus_present & line_state & high_speed & configured
                & suspended & address;

  -- 診断報告: 状態を sys ドメインへ 2 段 FF で取り込み、0.5 秒ごとに報告する
  process (sys_clk)
  begin
    if rising_edge(sys_clk) then
      status_s1    <= status_usb;
      status_sys   <= status_s1;
      heartbeat    <= heartbeat + 1;
      report_start <= '0';
      alive_prev   <= status_sys(14);
      if status_sys(14) /= alive_prev then
        alive_seen <= '1';
      end if;
      if heartbeat(24 downto 0) = 0 then
        clk_ok     <= alive_seen;
        alive_seen <= '0';
        report_start <= '1';
      end if;
    end if;
  end process;

  fields(0) <= "0000000" & clk_ok;
  -- status_sys: 14 = usb クロック動作, 13 = PHY 準備, 12 = VBUS, 11..10 = ラインステート,
  --             9 = HS, 8 = 構成済み, 7 = サスペンド, 6..0 = アドレス
  fields(1) <= "0000000" & status_sys(13);
  fields(2) <= "0000000" & status_sys(12);
  fields(3) <= "000000" & status_sys(11 downto 10);
  fields(4) <= "0000000" & status_sys(9);
  fields(5) <= "0000000" & status_sys(8);
  fields(6) <= "0000000" & status_sys(7);
  fields(7) <= '0' & status_sys(6 downto 0);
  unused_fields_g : for i in 8 to MAX_FIELDS - 1 generate                  -- 使わない欄も明示的に駆動する
    fields(i) <= (others => '0');
  end generate;

  report_i : entity work.text_report
    generic map (TEMPLATE => STATUS)
    port map (clk => sys_clk, rst => sys_rst, start => report_start, fields => fields, busy => open,
              out_valid => tx_valid, out_ready => tx_ready, out_data => tx_byte);

  uart_i : entity work.uart_tx
    port map (clk => sys_clk, rst => sys_rst,
              divisor => to_unsigned((SYS_CLK_HZ + 57_600) / 115_200, 16),
              in_valid => tx_valid, in_ready => tx_ready, in_data => tx_byte, tx => uart_tx,
              busy => open);

  led(0) <= not phy_ready;
  led(1) <= not high_speed;
  led(2) <= not configured;
  led(3) <= not suspended;
  led(4) <= not heartbeat(heartbeat'high);
  led(5) <= '1';
end architecture;
