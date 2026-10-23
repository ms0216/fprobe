-- Tang Primer 20K Dock 用の fprobe 製品デザイン。USB-OTG ポート（USB3317）を PC につなぐ。
--
--   usb ドメイン: ulpi_clk（USB3317 の 60 MHz CLKOUT）。usb_device を動かす。
--   sys ドメイン: clk27 → rPLL → sys_clk（100 MHz）。fprobe_core（CSR・ケイパビリティ・タイムベース）を動かす。
--   CONTROL チャンネル（EP1）は async_fifo で両ドメインをつなぐ（データ 8 ビット + メッセージ終わり）。
--   CMSIS-DAP（EP2）も async_fifo で sys ドメインの fprobe_core（dap_processor）へ。
--   デバッグポートのピンは J12（pmod1、割り当ては primer20k_pkg の DBG_*）。gpio ブロックで出力を有効にした
--   ピンは gpio が優先する（配線確認・ビットバング用）。続いてクロストリガの出力の動作（候補 XOUT_PIN_CHOICES から選んだ端子。claim のとき。
--   その端子の機能の出力は止め、機能の入力には休止の 1 を入れる）、最後に機能（pin_select）。
--   CDC-ACM（EP4）は async_fifo で fprobe_core の UART ブリッジへ。ボーレートは cdc_value で sys ドメインへ渡す。
--   ロジアナの専用の入力 la_in（J14 の 7〜12 番、U22）: 構成の LA_INPUTS 本を fprobe_core へ（0 なら使わない）。
--   COM ポートの RTS・ブレークはそのまま fprobe_core へ（中で同期化）。CTS の状態は usb ドメインへ同期化して SERIAL_STATE
--   の DSR で知らせる（CDC-ACM に CTS のビットは無い）。UART の RTS/CTS の端子（U52）は候補 UART_FLOW_PIN_CHOICES から
--   CSR uart.flow で選んだときだけ取る: RTS の端子はその機能の出力の代わりに RTS を出し、CTS の端子は機能の出力を止めて読む
--   （どちらも機能の層。gpio・クロストリガの出力がその端子を取っていればそちらが優先）。
--   ストリーム EP（EP6）: ロジアナの深いバッファの読み出し（sys の stream_out → async_fifo → usb）。
--   mem ドメイン: DDR3（primer20k_ddr3、Gowin の DDR3 IP）のユーザー側クロック mem_clk（約 99.6 MHz）。
--     メモリクロック ddr3_memory_clk（約 398 MHz）は clk27 → メモリ用 rPLL。
--   LED（アクティブロー）: led(0)=PHY 準備完了, led(1)=High-Speed, led(2)=構成済み, led(3)=サスペンド,
--                          led(4)=sys 心拍, led(5)=消灯
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.primer20k_pkg.all;

entity primer20k_fprobe_top is
  port (
    clk27     : in    std_logic;
    ulpi_clk  : in    std_logic;
    ulpi_dir  : in    std_logic;
    ulpi_nxt  : in    std_logic;
    ulpi_stp  : out   std_logic;
    ulpi_data : inout std_logic_vector(7 downto 0);
    ulpi_rst  : out   std_logic;
    pmod1     : inout std_logic_vector(7 downto 0);   -- 拡張ヘッダ J12
    aux_rx      : in    std_logic;                      -- 補助マイコン（Pico）の UART（J14 の 5 番）
    la_in       : in    std_logic_vector(LA_INPUT_PINS - 1 downto 0);   -- ロジアナの専用の入力（J14 の 7〜12 番）
    sync_in     : in    std_logic;                      -- ボードの間の時刻の同期（J6 の 1 番。親から）
    sync_out    : out   std_logic;                      -- 同（J6 の 2 番。子へ。同期の無い構成では Z）
    led       : out   std_logic_vector(5 downto 0);
    -- DDR3
    ddr_addr    : out   std_logic_vector(13 downto 0);
    ddr_ba      : out   std_logic_vector(2 downto 0);
    ddr_cs_n    : out   std_logic;
    ddr_ras_n   : out   std_logic;
    ddr_cas_n   : out   std_logic;
    ddr_we_n    : out   std_logic;
    ddr_ck      : out   std_logic;
    ddr_ck_n    : out   std_logic;
    ddr_cke     : out   std_logic;
    ddr_odt     : out   std_logic;
    ddr_reset_n : out   std_logic;
    ddr_dm      : out   std_logic_vector(1 downto 0);
    ddr_dq      : inout std_logic_vector(15 downto 0);
    ddr_dqs     : inout std_logic_vector(1 downto 0);
    ddr_dqs_n   : inout std_logic_vector(1 downto 0)
  );
end entity;

architecture rtl of primer20k_fprobe_top is
  signal usb_clk, usb_rst, phy_released : std_logic;
  signal sys_clk, sys_rst, pll_lock     : std_logic;
  signal sync_o : std_logic;                                         -- ボードの間の時刻の同期（コアの出力）
  -- DDR3（mem ドメイン）
  signal ddr3_memory_clk, mem_pll_lock, mem_clk, mem_rst, mem_ready : std_logic;
  signal mem_cmd_valid, mem_cmd_ready, mem_cmd_write, mem_rd_valid : std_logic;
  signal mem_cmd_addr : unsigned(22 downto 0);
  signal mem_cmd_data, mem_rd_data : std_logic_vector(127 downto 0);
  signal data_o  : std_logic_vector(7 downto 0);
  signal data_oe : std_logic;
  signal phy_ready, high_speed, configured, suspended : std_logic;

  -- CONTROL チャンネル（usb 側: エンドポイント、sys 側: fprobe_core）
  signal u_req_valid, u_req_ready, u_req_last, u_rsp_valid, u_rsp_ready : std_logic;
  signal u_req_data : std_logic_vector(7 downto 0);
  signal u_rsp      : std_logic_vector(8 downto 0);
  signal s_req_valid, s_req_ready, s_rsp_valid, s_rsp_ready, s_rsp_last : std_logic;
  signal s_req      : std_logic_vector(8 downto 0);
  signal s_rsp_data : std_logic_vector(7 downto 0);

  signal gpio_drive, gpio_enable : std_logic_vector(7 downto 0);
  -- CMSIS-DAP（usb ⇄ sys）
  signal ud_req_valid, ud_req_ready, ud_req_last, ud_rsp_valid, ud_rsp_ready : std_logic;
  signal ud_req_data : std_logic_vector(7 downto 0);
  signal ud_rsp      : std_logic_vector(8 downto 0);
  signal sd_req_valid, sd_req_ready, sd_rsp_valid, sd_rsp_ready, sd_rsp_last : std_logic;
  signal sd_req      : std_logic_vector(8 downto 0);
  signal sd_rsp_data : std_logic_vector(7 downto 0);
  signal hs_s1, hs_s : std_logic := '0';
  signal packet_size : natural range 64 to 512;
  -- デバッグポート
  signal swclk_o, swclk_oe, swdio_o, swdio_oe, nreset_low, tdi_o, tdi_oe : std_logic;
  -- UART（usb ⇄ sys）
  signal uu_out_valid, uu_out_ready, uu_in_valid, uu_in_ready : std_logic;
  signal uu_out_data, uu_in_data : std_logic_vector(7 downto 0);
  signal su_in_valid, su_in_ready, su_out_valid, su_out_ready : std_logic;
  signal su_in_data, su_out_data : std_logic_vector(7 downto 0);
  signal line_rate : unsigned(31 downto 0);
  signal line_changed, baud_load : std_logic;
  signal baud : std_logic_vector(31 downto 0);
  signal uart_tx, uart_tx_oe : std_logic;
  -- UART の制御線（U52）
  signal u_host_rts, u_host_break, uart_cts, uart_cts_n, uart_rts_n, rts_claim, cts_claim : std_logic;
  signal rts_pin, cts_pin : std_logic_vector(1 downto 0);
  signal cts_u1, cts_u : std_logic := '1';
  -- SWO（sys → usb）
  signal ss_valid, ss_ready, us_valid, us_ready : std_logic;
  signal ss_flush, us_flush : std_logic;          -- 受信の開始で残りを捨てる（B10。sys の窓を usb へ同期化）
  signal ss_data, us_data : std_logic_vector(7 downto 0);
  -- 拡張ストリーム（sys → usb）
  signal xs_valid, xs_ready, xu_valid, xu_ready : std_logic;
  signal xs_data, xu_data : std_logic_vector(7 downto 0);
  signal func_o, func_oe : std_logic_vector(7 downto 0);
  signal pin_o, pin_oe, claimed : std_logic_vector(7 downto 0);
  signal xout_o, xout_oe, xout_claim : std_logic_vector(1 downto 0);
  signal xout_pin : std_logic_vector(3 downto 0);
  signal heartbeat : unsigned(25 downto 0) := (others => '0');
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
              suspended => suspended, vbus_present => open, line_state => open, address => open,
              ctrl_out_valid => u_req_valid, ctrl_out_ready => u_req_ready,
              ctrl_out_data => u_req_data, ctrl_out_last => u_req_last,
              ctrl_in_valid => u_rsp_valid, ctrl_in_ready => u_rsp_ready,
              ctrl_in_data => u_rsp(7 downto 0), ctrl_in_last => u_rsp(8),
              dap_out_valid => ud_req_valid, dap_out_ready => ud_req_ready, dap_out_data => ud_req_data,
              dap_out_last => ud_req_last,
              dap_in_valid => ud_rsp_valid, dap_in_ready => ud_rsp_ready, dap_in_data => ud_rsp(7 downto 0),
              dap_in_last => ud_rsp(8),
              swo_in_valid => us_valid, swo_in_ready => us_ready, swo_in_data => us_data, swo_flush => us_flush,
              stream_in_valid => xu_valid, stream_in_ready => xu_ready,
              stream_in_data => xu_data, stream_in_last => '0',
              uart_out_valid => uu_out_valid, uart_out_ready => uu_out_ready, uart_out_data => uu_out_data,
              uart_in_valid => uu_in_valid, uart_in_ready => uu_in_ready, uart_in_data => uu_in_data,
              line_rate => line_rate, line_stop_bits => open, line_parity => open,
              line_data_bits => open, line_changed => line_changed, dtr => open, rts => u_host_rts,
              brk => u_host_break, serial_dcd => '1', serial_dsr => cts_u);

  -- CTS の状態を usb ドメインへ（ゆっくり変わる 1 ビット）
  process (usb_clk)
  begin
    if rising_edge(usb_clk) then
      cts_u1 <= uart_cts;
      cts_u  <= cts_u1;
    end if;
  end process;

  uart_to_sys_i : entity work.async_fifo
    generic map (WIDTH => 8, DEPTH => 512)
    port map (in_clk => usb_clk, in_rst => usb_rst, in_valid => uu_out_valid, in_ready => uu_out_ready,
              in_data => uu_out_data,
              out_clk => sys_clk, out_rst => sys_rst, out_valid => su_in_valid, out_ready => su_in_ready,
              out_data => su_in_data);

  uart_to_usb_i : entity work.async_fifo
    generic map (WIDTH => 8, DEPTH => 512)
    port map (in_clk => sys_clk, in_rst => sys_rst, in_valid => su_out_valid, in_ready => su_out_ready,
              in_data => su_out_data,
              out_clk => usb_clk, out_rst => usb_rst, out_valid => uu_in_valid, out_ready => uu_in_ready,
              out_data => uu_in_data);

  swo_to_usb_i : entity work.async_fifo
    generic map (WIDTH => 8, DEPTH => 2048)
    port map (in_clk => sys_clk, in_rst => sys_rst, in_valid => ss_valid, in_ready => ss_ready,
              in_data => ss_data,
              out_clk => usb_clk, out_rst => usb_rst, out_valid => us_valid, out_ready => us_ready,
              out_data => us_data, out_flush => us_flush);

  -- 窓（sys で 32 周期）は usb の 2 段の同期化と、FIFO の書き込みポインタの同期化より十分長い。窓の間は
  -- swo_receiver が書かないので、窓の終わりの捨てで前の受信のバイトはすべて消える
  swo_flush_sync_i : entity work.sync_bit
    port map (clk => usb_clk, d => ss_flush, q => us_flush);

  stream_to_usb_i : entity work.async_fifo
    generic map (WIDTH => 8, DEPTH => 2048)
    port map (in_clk => sys_clk, in_rst => sys_rst, in_valid => xs_valid, in_ready => xs_ready,
              in_data => xs_data,
              out_clk => usb_clk, out_rst => usb_rst, out_valid => xu_valid, out_ready => xu_ready,
              out_data => xu_data);

  baud_i : entity work.cdc_value
    generic map (WIDTH => 32)
    port map (src_clk => usb_clk, src_load => line_changed, src_value => std_logic_vector(line_rate),
              dst_clk => sys_clk, dst_value => baud, dst_changed => baud_load);

  request_fifo_i : entity work.async_fifo
    generic map (WIDTH => 9, DEPTH => 512)
    port map (in_clk => usb_clk, in_rst => usb_rst, in_valid => u_req_valid, in_ready => u_req_ready,
              in_data => u_req_last & u_req_data,
              out_clk => sys_clk, out_rst => sys_rst, out_valid => s_req_valid, out_ready => s_req_ready,
              out_data => s_req);

  dap_request_fifo_i : entity work.async_fifo
    generic map (WIDTH => 9, DEPTH => 512)
    port map (in_clk => usb_clk, in_rst => usb_rst, in_valid => ud_req_valid, in_ready => ud_req_ready,
              in_data => ud_req_last & ud_req_data,
              out_clk => sys_clk, out_rst => sys_rst, out_valid => sd_req_valid, out_ready => sd_req_ready,
              out_data => sd_req);

  dap_response_fifo_i : entity work.async_fifo
    generic map (WIDTH => 9, DEPTH => 512)
    port map (in_clk => sys_clk, in_rst => sys_rst, in_valid => sd_rsp_valid, in_ready => sd_rsp_ready,
              in_data => sd_rsp_last & sd_rsp_data,
              out_clk => usb_clk, out_rst => usb_rst, out_valid => ud_rsp_valid, out_ready => ud_rsp_ready,
              out_data => ud_rsp);

  -- パケットサイズ（HS 512 / FS 64）。速度はリセット・ネゴシエーションの間しか変わらない
  process (sys_clk)
  begin
    if rising_edge(sys_clk) then
      hs_s1 <= high_speed;
      hs_s  <= hs_s1;
    end if;
  end process;
  packet_size <= 512 when hs_s = '1' else 64;

  mem_pll_i : entity work.gowin_rpll
    generic map (FCLKIN_MHZ => "27", DEVICE => "GW2A-18C", IDIV_SEL => MEM_PLL_IDIV_SEL,
                 FBDIV_SEL => MEM_PLL_FBDIV_SEL, ODIV_SEL => MEM_PLL_ODIV_SEL)
    port map (clkin => clk27, clkout => ddr3_memory_clk, lock => mem_pll_lock);

  ddr3_i : entity work.primer20k_ddr3
    port map (clk27 => clk27, memory_clk => ddr3_memory_clk, pll_lock => mem_pll_lock, rst => not pll_lock,
              mem_clk => mem_clk, mem_rst => mem_rst, mem_ready => mem_ready,
              cmd_valid => mem_cmd_valid, cmd_ready => mem_cmd_ready, cmd_write => mem_cmd_write,
              cmd_addr => mem_cmd_addr, cmd_data => mem_cmd_data, rd_valid => mem_rd_valid, rd_data => mem_rd_data,
              ddr_addr => ddr_addr, ddr_ba => ddr_ba, ddr_cs_n => ddr_cs_n, ddr_ras_n => ddr_ras_n,
              ddr_cas_n => ddr_cas_n, ddr_we_n => ddr_we_n, ddr_ck => ddr_ck, ddr_ck_n => ddr_ck_n,
              ddr_cke => ddr_cke, ddr_odt => ddr_odt, ddr_reset_n => ddr_reset_n, ddr_dm => ddr_dm,
              ddr_dq => ddr_dq, ddr_dqs => ddr_dqs, ddr_dqs_n => ddr_dqs_n);

  core_i : entity work.fprobe_core
    generic map (CAPS => work.design_caps_pkg.CAPS_ROM, CLK_HZ => SYS_CLK_HZ, MEM_ADDR_BITS => 23,
                 MEM_TEST => work.design_caps_pkg.MEM_TEST,
                 XTRIG => work.design_caps_pkg.XTRIG, TRIG_SEQ => work.design_caps_pkg.TRIG_SEQ,
                 SAMPLER => work.design_caps_pkg.SAMPLER, SAMPLER_MATCH => work.design_caps_pkg.SAMPLER_MATCH,
                 HAS_JTAG => work.design_caps_pkg.HAS_JTAG, INJECTOR => work.design_caps_pkg.INJECTOR,
                 HAS_SWO_MANCHESTER => work.design_caps_pkg.SWO_MANCHESTER,
                 UART_FLOW => work.design_caps_pkg.UART_FLOW,
                 LA_FILTER => work.design_caps_pkg.LA_FILTER, LA_PULSE => work.design_caps_pkg.LA_PULSE,
                 LA_STREAM_EVENTS => work.design_caps_pkg.LA_STREAM_EVENTS,
                 AUX => work.design_caps_pkg.AUX, SYNC => work.design_caps_pkg.SYNC, XOUT_COUNT => work.design_caps_pkg.XOUT_COUNT,
                 LA_INPUT_COUNT => work.design_caps_pkg.LA_INPUTS,
                 LA_INTERNAL => false)        -- ロジアナは DDR3 の深いバッファ（内蔵 BSRAM 版は重複）
    port map (clk => sys_clk, rst => sys_rst,
              ctrl_in_valid => s_req_valid, ctrl_in_ready => s_req_ready,
              ctrl_in_data => s_req(7 downto 0), ctrl_in_last => s_req(8),
              ctrl_out_valid => s_rsp_valid, ctrl_out_ready => s_rsp_ready,
              ctrl_out_data => s_rsp_data, ctrl_out_last => s_rsp_last,
              packet_size => packet_size,
              dap_in_valid => sd_req_valid, dap_in_ready => sd_req_ready, dap_in_data => sd_req(7 downto 0),
              dap_in_last => sd_req(8), dap_out_valid => sd_rsp_valid, dap_out_ready => sd_rsp_ready,
              dap_out_data => sd_rsp_data, dap_out_last => sd_rsp_last,
              swclk_o => swclk_o, swclk_oe => swclk_oe, swdio_o => swdio_o, swdio_oe => swdio_oe,
              swdio_i => pmod1(DBG_SWDIO), nreset_low => nreset_low, nreset_i => pmod1(DBG_NRESET),
              tdi_o => tdi_o, tdi_oe => tdi_oe, tdo_i => pmod1(DBG_SWO) or claimed(DBG_SWO),
              swo_i => pmod1(DBG_SWO) or claimed(DBG_SWO), swo_out_valid => ss_valid, swo_out_ready => ss_ready,
              swo_out_data => ss_data, swo_flush => ss_flush,
              uart_baud => unsigned(baud), uart_baud_load => baud_load,
              uart_in_valid => su_in_valid, uart_in_ready => su_in_ready, uart_in_data => su_in_data,
              uart_out_valid => su_out_valid, uart_out_ready => su_out_ready, uart_out_data => su_out_data,
              uart_rx => pmod1(DBG_UART_RX) or claimed(DBG_UART_RX), uart_tx => uart_tx,
              uart_tx_oe => uart_tx_oe,
              uart_host_rts => u_host_rts, uart_host_break => u_host_break, uart_cts_n => uart_cts_n,
              uart_rts_n => uart_rts_n, uart_rts_claim => rts_claim, uart_rts_pin => rts_pin,
              uart_cts_claim => cts_claim, uart_cts_pin => cts_pin, uart_cts => uart_cts,
              la_pins => pmod1,
              la_inputs => la_in(maximum(work.design_caps_pkg.LA_INPUTS, 1) - 1 downto 0),
              gpio_drive => gpio_drive, gpio_enable => gpio_enable, gpio_level => pmod1,
              xout_o => xout_o, xout_oe => xout_oe, xout_claim => xout_claim, xout_pin => xout_pin,
              mem_clk => mem_clk, mem_rst => mem_rst, mem_ready => mem_ready,
              mem_cmd_valid => mem_cmd_valid, mem_cmd_ready => mem_cmd_ready, mem_cmd_write => mem_cmd_write,
              mem_cmd_addr => mem_cmd_addr, mem_cmd_data => mem_cmd_data, mem_rd_valid => mem_rd_valid,
              mem_rd_data => mem_rd_data,
              stream_out_valid => xs_valid, stream_out_ready => xs_ready, stream_out_data => xs_data,
              aux_rx => aux_rx, sync_in => sync_in, sync_out => sync_o);
  sync_out <= sync_o when work.design_caps_pkg.SYNC else 'Z';

  -- デバッグポートの機能ピン（それ以外のピンは入力のまま）
  process (all)
  begin
    func_o  <= (others => '0');
    func_oe <= (others => '0');
    func_o(DBG_SWCLK)   <= swclk_o;
    func_oe(DBG_SWCLK)  <= swclk_oe;
    func_o(DBG_SWDIO)   <= swdio_o;
    func_oe(DBG_SWDIO)  <= swdio_oe;
    func_o(DBG_NRESET)  <= '0';
    func_oe(DBG_NRESET) <= nreset_low;
    func_o(DBG_TDI)      <= tdi_o;
    func_oe(DBG_TDI)     <= tdi_oe;
    func_o(DBG_UART_TX)  <= uart_tx;
    func_oe(DBG_UART_TX) <= uart_tx_oe;    -- ホストが UART に最初の 1 バイトを送るまで駆動しない（B29）
    -- UART の RTS/CTS が取った端子（候補に無い番号は何もしない）
    for j in UART_FLOW_PIN_CHOICES'range loop
      if cts_claim = '1' and to_integer(unsigned(cts_pin)) = j then
        func_oe(UART_FLOW_PIN_CHOICES(j)) <= '0';
      end if;
      if rts_claim = '1' and to_integer(unsigned(rts_pin)) = j then
        func_o(UART_FLOW_PIN_CHOICES(j))  <= uart_rts_n;
        func_oe(UART_FLOW_PIN_CHOICES(j)) <= '1';
      end if;
    end loop;
  end process;

  -- CTS の線（アクティブ Low）。取っていなければ出ているとみなす（'0'）
  process (all)
  begin
    uart_cts_n <= '0';
    for j in UART_FLOW_PIN_CHOICES'range loop
      if cts_claim = '1' and to_integer(unsigned(cts_pin)) = j then
        uart_cts_n <= pmod1(UART_FLOW_PIN_CHOICES(j));
      end if;
    end loop;
  end process;

  pin_select_i : entity work.pin_select
    generic map (PINS => 8, ACTIONS => 2, PIN_CHOICES => (XOUT_PIN_CHOICES(0), XOUT_PIN_CHOICES(1), XOUT_PIN_CHOICES(2), XOUT_PIN_CHOICES(3)))
    port map (gpio_drive => gpio_drive, gpio_enable => gpio_enable, func_o => func_o, func_oe => func_oe,
              xout_o => xout_o, xout_oe => xout_oe, xout_claim => xout_claim, xout_pin => xout_pin,
              pin_o => pin_o, pin_oe => pin_oe, claimed => claimed);

  pins : for i in pmod1'range generate
    pmod1(i) <= pin_o(i) when pin_oe(i) = '1' else 'Z';
  end generate;

  response_fifo_i : entity work.async_fifo
    generic map (WIDTH => 9, DEPTH => 512)
    port map (in_clk => sys_clk, in_rst => sys_rst, in_valid => s_rsp_valid, in_ready => s_rsp_ready,
              in_data => s_rsp_last & s_rsp_data,
              out_clk => usb_clk, out_rst => usb_rst, out_valid => u_rsp_valid, out_ready => u_rsp_ready,
              out_data => u_rsp);

  process (sys_clk)
  begin
    if rising_edge(sys_clk) then
      heartbeat <= heartbeat + 1;
    end if;
  end process;

  led(0) <= not phy_ready;
  led(1) <= not high_speed;
  led(2) <= not configured;
  led(3) <= not suspended;
  led(4) <= not heartbeat(heartbeat'high);
  led(5) <= not mem_ready;                                   -- DDR3 の較正が済んだら点灯
end architecture;
