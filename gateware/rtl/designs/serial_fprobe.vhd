-- SerialLink の fprobe（ボード非依存）: PC とは UART 1 本（BL702 等の USB-UART）。仕様: docs/superpowers/specs/2026-10-17-m11-serial-link-design.md
--
--   sys ドメインだけ: serial_link と fprobe_core を直結する（USB 版のようなクロックの乗り換えは無い）。外部メモリは使わず、
--   ロジアナは内蔵 BSRAM。fprobe_core は USB 版と同じもの（構成はデザインのケイパビリティ work.design_caps_pkg）。
--   デバッグポートのピンは pmod（8 本。機能の割り当ては generic の DBG_*）。gpio が有効なピンは gpio、そうでなければ機能、
--   どちらでもなければ高インピーダンス。
--   LED（アクティブロー）: led(0)=PLL ロック, led(4)=sys 心拍, それ以外は消灯
-- ボードのトップ（primer20k_fprobe_serial_top・nano9k_fprobe_top）はクロック（PLL）とピンをつなぐだけ。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity serial_fprobe is
  generic (
    SYS_CLK_HZ  : positive;
    SERIAL_BAUD : positive;
    -- pmod のピンの番号（ボードのパッケージの DBG_*）
    DBG_SWCLK   : natural;
    DBG_TDI     : natural;
    DBG_NRESET  : natural;
    DBG_SWDIO   : natural;
    DBG_UART_RX : natural;
    DBG_SWO     : natural;
    DBG_UART_TX : natural
  );
  port (
    sys_clk  : in    std_logic;
    pll_lock : in    std_logic;                       -- sys_clk の PLL のロック（0 の間はリセット）
    uart_tx  : out   std_logic;                       -- PC の COM ポートへ
    uart_rx  : in    std_logic;
    pmod     : inout std_logic_vector(7 downto 0);
    aux_rx   : in    std_logic := '1';                -- 補助マイコンの UART（ケイパビリティ AUX の構成だけ）
    led      : out   std_logic_vector(5 downto 0)
  );
end entity;

architecture rtl of serial_fprobe is
  constant LINK_PACKET_SIZE : positive := 512;   -- fprobe_core の packet_size（USB の High-Speed と同じ 512 バイト）
  signal sys_rst : std_logic;
  -- serial_link（PC 側）⇄ fprobe_core
  signal req_valid, req_ready, req_last, rsp_valid, rsp_ready, rsp_last : std_logic;
  signal req_data, rsp_data : std_logic_vector(7 downto 0);
  signal dreq_valid, dreq_ready, dreq_last, drsp_valid, drsp_ready, drsp_last : std_logic;
  signal dreq_data, drsp_data : std_logic_vector(7 downto 0);
  signal swo_valid, swo_ready, uo_valid, uo_ready, ui_valid, ui_ready, st_valid, st_ready : std_logic;
  signal swo_data, uo_data, ui_data, st_data : std_logic_vector(7 downto 0);
  signal line_rate : unsigned(31 downto 0);
  signal line_changed : std_logic;
  -- デバッグポート
  signal swclk_o, swclk_oe, swdio_o, swdio_oe, nreset_low, tdi_o, tdi_oe, target_tx, target_tx_oe : std_logic;
  signal gpio_drive, gpio_enable, func_o, func_oe : std_logic_vector(7 downto 0);
  signal heartbeat : unsigned(HEARTBEAT_BITS - 1 downto 0) := (others => '0');
begin
  sys_reset_i : entity work.reset_sync
    port map (clk => sys_clk, arst_in => not pll_lock, rst => sys_rst);

  link_i : entity work.serial_link
    generic map (CLK_HZ => SYS_CLK_HZ, BAUD => SERIAL_BAUD)
    port map (clk => sys_clk, rst => sys_rst, rx => uart_rx, tx => uart_tx, bad_frames => open,
              ctrl_out_valid => req_valid, ctrl_out_ready => req_ready, ctrl_out_data => req_data,
              ctrl_out_last => req_last,
              ctrl_in_valid => rsp_valid, ctrl_in_ready => rsp_ready, ctrl_in_data => rsp_data, ctrl_in_last => rsp_last,
              dap_out_valid => dreq_valid, dap_out_ready => dreq_ready, dap_out_data => dreq_data,
              dap_out_last => dreq_last,
              dap_in_valid => drsp_valid, dap_in_ready => drsp_ready, dap_in_data => drsp_data, dap_in_last => drsp_last,
              swo_in_valid => swo_valid, swo_in_ready => swo_ready, swo_in_data => swo_data,
              stream_in_valid => st_valid, stream_in_ready => st_ready, stream_in_data => st_data,
              uart_out_valid => uo_valid, uart_out_ready => uo_ready, uart_out_data => uo_data,
              uart_in_valid => ui_valid, uart_in_ready => ui_ready, uart_in_data => ui_data,
              line_rate => line_rate, line_changed => line_changed);

  core_i : entity work.fprobe_core
    -- 構成の機能はデザインの生成パッケージから全部渡す（ケイパビリティと回路を一致させる。tests/test_core_generics.py）
    generic map (CAPS => work.design_caps_pkg.CAPS_ROM, MEM_ADDR_BITS => work.design_caps_pkg.MEM_ADDR_BITS,
                 MEM_TEST => work.design_caps_pkg.MEM_TEST, XTRIG => work.design_caps_pkg.XTRIG,
                 TRIG_SEQ => work.design_caps_pkg.TRIG_SEQ, SAMPLER => work.design_caps_pkg.SAMPLER,
                 SAMPLER_MATCH => work.design_caps_pkg.SAMPLER_MATCH, SAMPLER_CHECK_MAX => work.design_caps_pkg.SAMPLER_CHECK_MAX,
                 HAS_JTAG => work.design_caps_pkg.HAS_JTAG, INJECTOR => work.design_caps_pkg.INJECTOR,
                 HAS_SWO_MANCHESTER => work.design_caps_pkg.SWO_MANCHESTER, UART_FLOW => work.design_caps_pkg.UART_FLOW,
                 AUX => work.design_caps_pkg.AUX, SYNC => work.design_caps_pkg.SYNC,
                 OBS_PORT => work.design_caps_pkg.OBS_PORT, XOUT_COUNT => work.design_caps_pkg.XOUT_COUNT,
                 LA_INPUT_COUNT => work.design_caps_pkg.LA_INPUTS, LA_FILTER => work.design_caps_pkg.LA_FILTER,
                 LA_PULSE => work.design_caps_pkg.LA_PULSE, LA_STREAM_EVENTS => work.design_caps_pkg.LA_STREAM_EVENTS,
                 LA_STREAM_GAPS => work.design_caps_pkg.LA_STREAM_GAPS, LA_RLE_TOKENS => work.design_caps_pkg.LA_RLE_TOKENS, LA_RLE_REPEAT => work.design_caps_pkg.LA_RLE_REPEAT, EVENT_RING_BITS => work.design_caps_pkg.EVENT_RING_BITS,
                 EVENT_BYTE_PACK => work.design_caps_pkg.EVENT_BYTE_PACK, CAP_RATIO => work.design_caps_pkg.CAP_RATIO,
                 LA_TESTGEN => work.design_caps_pkg.LA_TESTGEN, LA_LANES => work.design_caps_pkg.LA_LANES,
                 LA_LANE_ADDR_BITS => work.design_caps_pkg.LA_LANE_ADDR_BITS, LA_LANE_IN_AGE => work.design_caps_pkg.LA_LANE_IN_AGE,
                 -- ボードで決まるもの
                 CLK_HZ => SYS_CLK_HZ, LA_INTERNAL => true)
    port map (clk => sys_clk, rst => sys_rst,
              ctrl_in_valid => req_valid, ctrl_in_ready => req_ready, ctrl_in_data => req_data,
              ctrl_in_last => req_last,
              ctrl_out_valid => rsp_valid, ctrl_out_ready => rsp_ready, ctrl_out_data => rsp_data,
              ctrl_out_last => rsp_last,
              packet_size => LINK_PACKET_SIZE,
              dap_in_valid => dreq_valid, dap_in_ready => dreq_ready, dap_in_data => dreq_data, dap_in_last => dreq_last,
              dap_out_valid => drsp_valid, dap_out_ready => drsp_ready, dap_out_data => drsp_data,
              dap_out_last => drsp_last,
              swclk_o => swclk_o, swclk_oe => swclk_oe, swdio_o => swdio_o, swdio_oe => swdio_oe,
              swdio_i => pmod(DBG_SWDIO), nreset_low => nreset_low, nreset_i => pmod(DBG_NRESET),
              tdi_o => tdi_o, tdi_oe => tdi_oe, tdo_i => pmod(DBG_SWO),
              swo_i => pmod(DBG_SWO), swo_out_valid => swo_valid, swo_out_ready => swo_ready, swo_out_data => swo_data,
              uart_baud => line_rate, uart_baud_load => line_changed,
              uart_in_valid => uo_valid, uart_in_ready => uo_ready, uart_in_data => uo_data,
              uart_out_valid => ui_valid, uart_out_ready => ui_ready, uart_out_data => ui_data,
              uart_rx => pmod(DBG_UART_RX), uart_tx => target_tx, uart_tx_oe => target_tx_oe, la_pins => pmod,
              gpio_drive => gpio_drive, gpio_enable => gpio_enable, gpio_level => pmod,
              mem_cmd_valid => open, mem_cmd_write => open, mem_cmd_addr => open, mem_cmd_data => open,
              stream_out_valid => st_valid, stream_out_ready => st_ready, stream_out_data => st_data,
              aux_rx => aux_rx);

  -- デバッグポートの機能ピン（USB 版と同じ）
  pin_func_i : entity work.debug_pin_func
    generic map (DBG_SWCLK => DBG_SWCLK, DBG_TDI => DBG_TDI, DBG_NRESET => DBG_NRESET, DBG_SWDIO => DBG_SWDIO,
                 DBG_UART_TX => DBG_UART_TX)
    port map (swclk_o => swclk_o, swclk_oe => swclk_oe, swdio_o => swdio_o, swdio_oe => swdio_oe, nreset_low => nreset_low,
              tdi_o => tdi_o, tdi_oe => tdi_oe, uart_tx => target_tx, uart_tx_oe => target_tx_oe,
              func_o => func_o, func_oe => func_oe, uart_cts_n => open);

  pins : for i in pmod'range generate
    pmod(i) <= gpio_drive(i) when gpio_enable(i) = '1' else
               func_o(i)     when func_oe(i) = '1' else 'Z';
  end generate;

  process (sys_clk)
  begin
    if rising_edge(sys_clk) then
      heartbeat <= heartbeat + 1;
    end if;
  end process;

  led <= (0 => not pll_lock, 4 => not heartbeat(heartbeat'high), others => '1');
end architecture;
