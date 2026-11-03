-- fprobe のコア（ボード非依存、sys ドメイン）。CONTROL チャンネルのストリームを受け、CSR 空間を提供する。
-- 仕様: docs/superpowers/specs/2026-10-15-m3-control-plane-design.md §2
--
--   CONTROL 要求 → control_plane → csr_decoder ┬ csr_system（識別・疎通確認）
--                  ← 応答                      ├ csr_timebase ⇄ timebase（time_lo を読むと上位を保持）
--                                              ├ csr_gpio（拡張ヘッダの汎用入出力）
--                                              └ caps_rom（ケイパビリティ）
--   DAP コマンド → dap_processor → swd_injector → dap_sampler → debug_port（swd_phy / jtag_phy）→ SWCLK/TCK、SWDIO/TMS、TDI、nRESET
--                ← 応答            ↑ dap_monitor（ホストの転送を覗く: SELECT の控え、番地一致）
--   SWO ピン → swo_receiver（設定は DAP の SWO コマンド）→ swo_out（USB の SWO エンドポイントへ）
--   UART（ホスト側ストリーム）⇄ uart_bridge ⇄ ターゲットの UART（状態は csr_uart）。TX はホストが最初の 1 バイトを
--     送るまで駆動しない（uart_tx_oe。B29）
--   ロジアナ: la_capture（J12 のピン + 内部信号 + 専用の入力）。設定は csr_la、バッファは la_mem 窓
--     ch の並び: 0〜LA_PIN_COUNT-1 = 拡張ヘッダのピン、続いて内部の信号 2 本（SWDIO を駆動中・DAP の処理中）、
--     続いて専用の入力 LA_INPUT_COUNT 本（U22。デバッグの線と兼用しない入力）、残りは 0。ケイパビリティ LA_LAYOUT と一致
--     ピンと専用の入力は la_filter（グリッチフィルタ）を通してからロジアナ・多段トリガ・la_pulse（パルス幅のトリガ）へ（U21）
--   外部メモリ（csr_mem）: mem_test_sys（メモリ試験。メモリは mem_port で、mem_clk のドメイン）
--   ロジアナの深いバッファ（csr_la_deep）: la_deep_buffer（外部メモリへ記録し、読み出しは stream_out = USB のストリーム）
--   DAP のイベント記録（csr_events）: dap_monitor の転送ごとのイベントと割り込み書き込みを、時刻を付けて同じ経路で記録
--   クロストリガ（csr_xtrig）: 源（ロジアナのトリガ・番地一致・ホストの指令）→ 動作（ロジアナのトリガ・
--     割り込み書き込み = 既定はターゲット停止）
--   クロストリガの動作「出力の端子」（csr_xtrig_out）: xtrig_out（端子の並び・源 → 脈・保持・条件の間）→ xout_*
--     （端子への割り当てと、機能との切り替えはボードのトップ。docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md）
-- ボードのトップはクロック・リセット・ホストリンク（USB 等）とクロック乗せ換えの FIFO を用意してつなぐ。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;
use work.csr_map_pkg.all;
use work.timing_pkg.all;
use work.util_pkg.all;

entity fprobe_core is
  generic (
    CAPS   : csr_word_array_t;        -- ケイパビリティ ROM の内容（デザインごとの生成パッケージ）
    CLK_HZ : positive;                -- clk の周波数
    LA_PIN_COUNT : positive := 8;          -- ロジアナに入れる外部ピンの数（チャンネル 0 から）
    LA_INPUT_COUNT : natural := 0;         -- ロジアナの専用の入力の数（内部の信号 2 本の後の ch。ケイパビリティ
                                           -- LA_LAYOUT と一致させる。U22）
    MEM_ADDR_BITS : natural := 0;          -- 外部メモリの拍（16 バイト）の番地のビット数。0 = 外部メモリ無し
    LA_INTERNAL   : boolean := true;       -- 内蔵 BSRAM のロジアナ（外部メモリのあるボードでは深いバッファが上位互換）
    MEM_TEST      : boolean := false;      -- メモリ試験回路（基板立ち上げ用。深いバッファ・イベント記録の代わりに
                                           -- 外部メモリを使う。ケイパビリティ MEM_TEST と一致させる）
    -- 小さい FPGA 向けに外せる機能（ケイパビリティ FEATURES と一致させる。外した CSR の窓はバスエラーで答える）
    XTRIG         : boolean := true;       -- クロストリガ（割り込み書き込み・番地一致の CSR・源と動作の対応）
    TRIG_SEQ      : boolean := true;       -- 多段トリガ
    -- ボードの構成で付く機能
    AUX           : boolean := false;      -- 補助マイコン（Pico）の計測値の受け口（aux_rx、CSR aux、イベント 6）
    SAMPLER       : boolean := false;      -- DAP サンプラ（F4: 変数のライブ観測。CSR sampler、イベント 7）
    SAMPLER_MATCH : boolean := false;      -- サンプラの値の一致 → クロストリガの源 bit5（R21。CSR sampler_match）
    OBS_PORT      : boolean := false;      -- 対象の FPGA の観測（obs_jtag_engine、obs_* の口、CSR obs、イベント 8・9）
    SYNC          : boolean := false;      -- ボードの間の時刻の同期（sync_link、sync_in / sync_out、CSR sync、イベント 10）
    -- Primer の構成（B22: 標準 = JTAG なし、JTAG の構成 = サンプラ・故障注入なし）で外す機能。ケイパビリティ DEBUG と一致させる
    HAS_JTAG      : boolean := true;       -- JTAG の PHY と経路（無ければ DAP_Connect の JTAG は失敗）
    HAS_SWO_MANCHESTER : boolean := true;  -- SWO の Manchester 方式の受信（U51。ケイパビリティ DEBUG と DAP_Info に出す）
    UART_FLOW     : boolean := false;      -- UART の RTS/CTS（U52。ケイパビリティ UART_FLOW。端子はトップが候補から選ぶ）
    XOUT_COUNT    : natural range 0 to 2 := 0;   -- クロストリガの出力の動作の数（ケイパビリティ OUTPUTS。0 = 無し）
    -- ロジアナの前段（U21。ケイパビリティ FEATURES の LA_FILTER / LA_PULSE と一致させる。無ければ CSR の設定は
    -- 読み書きできるが何もしない）
    LA_FILTER     : boolean := true;       -- ピンのグリッチフィルタ（la_filter）
    LA_PULSE      : boolean := true;       -- パルス幅のトリガ（la_pulse）
    LA_STREAM_EVENTS : boolean := false;   -- 深いバッファのストリームにイベントも見出し付きで流す（R1。ケイパビリティ
                                           -- FEATURES の LA_STREAM_EVENTS と一致させる。外部メモリのある構成で）
    LA_STREAM_GAPS : boolean := false;     -- 深いバッファのストリームで溢れても止めず欠けの区間を記録して続けられる（FEATURES の
                                           -- LA_STREAM_GAPS と一致させる。LA_STREAM_EVENTS が要る。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）
    -- 外部メモリの無い構成のイベント記録の件数（2 のべき。0 = 無し。外部メモリのある構成では使わない）
    EVENT_RING_BITS : natural := 0;
    -- 試験用: イベントの書いた数を 2**32 のこの数だけ手前から数える（0 = 0 から。2**32 を超える試験を速くするため）
    EVENT_COUNT_START_BELOW_4G : natural := 0;
    MEM_SAME_CLOCK : boolean := false;     -- mem_clk = clk（PYNQ）。深いバッファの受け渡しに同期化を使わない
    -- リセットの扇出の分割（タイミングの余裕のため。false = 全部 rst をそのまま使う = 従来と同じ回路。true = 回路の塊ごと
    -- に rst を 2 周期遅らせた別のレジスタを使う: 解除もアサートも全塊で同じだけ 2 周期遅れる。reset_fanout）
    RESET_SPLIT   : boolean := false;
    INJECTOR      : boolean := true;       -- 故障注入 = 割り込み書き込み（swd_injector。XTRIG の一部。無ければ
                                           -- xtrig の inject_status.result = 3、route.to_inject は何もしない）
    CAP_RATIO     : natural := 1;          -- 内蔵ロジアナの取り込みのクロック cap_clk / clk（整数比。1 = cap_clk を使わず今と同じ回路。
                                           -- 2 以上 = ロジアナ一式を cap_clk の領域で動かす: la_cap_domain。LA_INTERNAL のときだけ）
    LA_TESTGEN    : boolean := false;      -- 試験用の方形波（la_testgen。CSR la_testgen）。刻むクロックは CAP_RATIO > 1 なら cap_clk、そうでなければ clk
    -- 内蔵ロジアナの 1 語（sys の 1 周期）のサンプルの数（docs/superpowers/specs/2026-11-01-la-resolution-design.md の段 1）。
    -- 1 = 今の回路。2 以上 = 専用の入力をボードが端子で直並列変換して la_input_lanes に渡し、la_lane_capture で記録する
    -- （LA_INTERNAL のときだけ。拡張ヘッダのピンは sys で 2 段同期化し、内部の信号とともに全レーンに同じ値を並べる）。
    -- グリッチフィルタ・パルス幅のトリガ・la.rate は無い（LA_FILTER・LA_PULSE は false。ケイパビリティと一致させる）
    LA_LANES      : positive := 1;
    LA_LANE_ADDR_BITS : positive := 14;    -- その内蔵バッファの語の番号のビット数（深さ = 2 ** これ）
    LA_LANE_IN_AGE : natural := 0;         -- la_input_lanes の最も新しいレーンの遅れ（細かいサンプルの数。ボードの直並列変換で決まる。
                                           -- la_lane_capture の IN_AGE。時刻の式の latency に入る）
    -- LA_LANES > 1 のとき、拡張ヘッダのピンもボードの直並列変換のレーン（la_pin_lanes。la_input_lanes と同じ遅れ）で記録する。
    -- false ならピンは sys で 2 段同期化して全レーンに並べる。ケイパビリティ la_fine_pins（LA_FINE）と一致させる
    LA_FINE_PINS  : boolean := false;
    EVENT_BYTE_PACK : boolean := false;    -- UART・SWO のバイトのイベントを詰めた形（種類 13〜15）で出す（event_collector の BYTE_PACK。
                                           -- ケイパビリティ FEATURES の EVENT_BYTE_PACK と一致させる）
    -- DAP サンプラの JTAG の確認の間隔 K の上限（dap_sampler の CHECK_MAX。溜める語の数。CSR sampler.check.max で申告する）
    SAMPLER_CHECK_MAX : positive range 1 to 64 := 1
  );
  port (
    clk            : in  std_logic;
    cap_clk        : in  std_logic := '0';                -- CAP_RATIO >= 2 のとき、clk と同じ MMCM から割った clk の CAP_RATIO 倍のクロック
    rst            : in  std_logic;
    -- CONTROL チャンネル（ctrl_in: 要求、ctrl_out: 応答。last はメッセージの終わり）
    ctrl_in_valid  : in  std_logic;
    ctrl_in_ready  : out std_logic;
    ctrl_in_data   : in  std_logic_vector(7 downto 0);
    ctrl_in_last   : in  std_logic;
    ctrl_out_valid : out std_logic;
    ctrl_out_ready : in  std_logic;
    ctrl_out_data  : out std_logic_vector(7 downto 0);
    ctrl_out_last  : out std_logic;
    -- CMSIS-DAP（dap_in: コマンド、dap_out: 応答。last はパケットの終わり）
    packet_size    : in  natural range 64 to 512;    -- ホストリンクの現在のパケットサイズ
    dap_in_valid   : in  std_logic;
    dap_in_ready   : out std_logic;
    dap_in_data    : in  std_logic_vector(7 downto 0);
    dap_in_last    : in  std_logic;
    dap_out_valid  : out std_logic;
    dap_out_ready  : in  std_logic;
    dap_out_data   : out std_logic_vector(7 downto 0);
    dap_out_last   : out std_logic;
    -- デバッグポートのピン
    swclk_o        : out std_logic;
    swclk_oe       : out std_logic;
    swdio_o        : out std_logic;
    swdio_oe       : out std_logic;
    -- SWDIO / TMS の、その周期の前半の値（ODDR で出すボード用: 前半 swdio_o_lead・後半 swdio_o。JTAG の half_period = 1 で
    -- TMS を TCK の立ち下がりの半周期後に変える。jtag_phy の先頭のコメント）。ODDR を使わないボードはつながない
    swdio_o_lead   : out std_logic;
    swdio_i        : in  std_logic;
    tdi_o          : out std_logic;
    tdi_oe         : out std_logic;
    tdo_i          : in  std_logic;                  -- JTAG の TDO（SWO と同じピンでよい）
    nreset_low     : out std_logic;                  -- '1' で Low を駆動（オープンドレイン）
    nreset_i       : in  std_logic;
    swo_i          : in  std_logic;
    -- SWO のバイト列（ホストリンクの SWO チャンネルへ）
    swo_out_valid  : out std_logic;
    swo_out_ready  : in  std_logic;
    swo_out_data   : out std_logic_vector(7 downto 0);
    -- SWO の受信の開始の窓（B10。swo_receiver の FLUSH_CYCLES 周期の 1）: ホストリンクはこの間に SWO の
    -- バッファに残った前の受信のバイトを捨てる（捨てないリンクはつながなくてよい）
    swo_flush      : out std_logic;
    -- UART ブリッジ（host_in: ホスト → ターゲット、host_out: ターゲット → ホスト）
    uart_baud      : in  unsigned(31 downto 0);
    uart_baud_load : in  std_logic;
    uart_in_valid  : in  std_logic;
    uart_in_ready  : out std_logic;
    uart_in_data   : in  std_logic_vector(7 downto 0);
    uart_out_valid : out std_logic;
    uart_out_ready : in  std_logic;
    uart_out_data  : out std_logic_vector(7 downto 0);
    uart_rx        : in  std_logic;
    uart_tx        : out std_logic;
    -- TX を駆動してよい（B29）。リセット後は 0（高インピーダンス）で、ブリッジがホストから最初の 1 バイトを
    -- 受け取ったら以後 1（UART の休止は High なので、使い始めたら駆動し続ける）。頼まれるまでターゲットの線を駆動しない
    uart_tx_oe     : out std_logic;
    -- UART の制御線（U52。docs/superpowers/specs/2026-10-19-u51-u52-swo-uart-design.md §4）。
    -- host_rts / host_break: COM ポートの RTS とブレーク（CDC-ACM の要求。別のクロックのドメインでよい: 中で同期化する）。
    -- 線の RTS / CTS はアクティブ Low（rts_n / cts_n）。rts_claim・cts_claim = その線を端子の候補 rts_pin・cts_pin に
    -- 割り当てる（CSR uart.flow。トップがその端子の機能の出力を止め、RTS を出す / CTS を読む。claim でない CTS は '0'）。
    -- cts = CTS の状態（出ている = 1。CDC-ACM の SERIAL_STATE で知らせる）。UART_FLOW = false なら claim は 0
    uart_host_rts   : in  std_logic := '0';
    uart_host_break : in  std_logic := '0';
    uart_cts_n      : in  std_logic := '0';
    uart_rts_n      : out std_logic;
    uart_rts_claim  : out std_logic;
    uart_rts_pin    : out std_logic_vector(1 downto 0);
    uart_cts_claim  : out std_logic;
    uart_cts_pin    : out std_logic_vector(1 downto 0);
    uart_cts        : out std_logic;
    -- ロジアナの外部入力（ピンの値、非同期でよい）
    la_pins        : in  std_logic_vector(LA_PIN_COUNT - 1 downto 0);
    -- ロジアナの専用の入力（非同期でよい。LA_INPUT_COUNT = 0 なら使わない）
    la_inputs      : in  std_logic_vector(maximum(LA_INPUT_COUNT, 1) - 1 downto 0) := (others => '0');
    -- 専用の入力の細かいサンプル（LA_LANES > 1 のとき。la_inputs の代わり）: レーン k（その周期の k 番目、k = 0 が最も古い）の
    -- 入力 i = la_input_lanes(k × LA_INPUT_COUNT + i)。clk に同期した値（ボードの直並列変換の出力）
    la_input_lanes : in  std_logic_vector(maximum(LA_LANES * LA_INPUT_COUNT, 1) - 1 downto 0) := (others => '0');
    -- 拡張ヘッダのピンの細かいサンプル（LA_FINE_PINS のとき）: レーン k のピン i = la_pin_lanes(k × LA_PIN_COUNT + i)。
    -- la_pins（端子の値そのもの。多段トリガ・gpio 等の sys の経路）は別に要る
    la_pin_lanes   : in  std_logic_vector(LA_LANES * LA_PIN_COUNT - 1 downto 0) := (others => '0');
    -- 拡張ヘッダの汎用入出力（CSR の gpio ブロック）
    gpio_drive     : out std_logic_vector(7 downto 0);
    gpio_enable    : out std_logic_vector(7 downto 0);
    gpio_level     : in  std_logic_vector(7 downto 0);
    -- クロストリガの出力の動作 k（XOUT_COUNT 以上は 0）。pin = 動かす端子の候補の番号（2 ビットずつ。端子への対応は
    -- トップの pin_select）、claim = 端子を出力に使う（トップがその端子の機能の出力を止め、機能の入力に 1 を入れる）。
    -- oe = 1 の間だけ o を駆動する
    xout_o         : out std_logic_vector(1 downto 0);
    xout_oe        : out std_logic_vector(1 downto 0);
    xout_claim     : out std_logic_vector(1 downto 0);
    xout_pin       : out std_logic_vector(3 downto 0);
    -- 外部メモリ（mem_port。mem_clk のドメイン。MEM_ADDR_BITS = 0 なら使わない）
    mem_clk        : in  std_logic := '0';
    mem_rst        : in  std_logic := '1';
    mem_ready      : in  std_logic := '0';
    mem_cmd_valid  : out std_logic;
    mem_cmd_ready  : in  std_logic := '0';
    mem_cmd_write  : out std_logic;
    mem_cmd_addr   : out unsigned(maximum(MEM_ADDR_BITS, 1) - 1 downto 0);
    mem_cmd_data   : out std_logic_vector(127 downto 0);
    mem_rd_valid   : in  std_logic := '0';
    mem_rd_data    : in  std_logic_vector(127 downto 0) := (others => '0');
    mem_wr_pending : in  unsigned(3 downto 0) := (others => '0');   -- 応答の来ていない書きの数（axi_mem_port）。PS 直読みで使う
    -- 拡張ストリーム（ロジアナの深いバッファの読み出し。バイト列）
    stream_out_valid : out std_logic;
    stream_out_ready : in  std_logic := '1';
    stream_out_data  : out std_logic_vector(7 downto 0);
    -- PS が DDR のリングを直接読む流れのレジスタ（la_ps_port。PYNQ の窓 zynq_frame_link の ps_bus_* とつなぐ。
    -- docs/superpowers/specs/2026-11-03-ps-stream-position-design.md）。深いバッファの無い構成は読みが 0、書きは無視
    ps_bus_rd        : in  std_logic := '0';
    ps_bus_wr        : in  std_logic := '0';
    ps_bus_addr      : in  unsigned(7 downto 0) := (others => '0');
    ps_bus_wdata     : in  std_logic_vector(31 downto 0) := (others => '0');
    ps_bus_rdata     : out std_logic_vector(31 downto 0);
    -- 補助マイコン（AUX のときだけ使う）
    aux_rx           : in  std_logic := '1';
    -- 対象の FPGA の観測の JTAG（OBS_PORT のときだけ使う。obs_oe = 1 の間だけ駆動する）
    obs_tck          : out std_logic;
    obs_tms          : out std_logic;
    obs_tdi          : out std_logic;
    obs_tdo          : in  std_logic := '0';
    obs_oe           : out std_logic;
    -- ボードの間の時刻の同期（SYNC のときだけ使う）
    sync_in          : in  std_logic := '0';
    sync_out         : out std_logic;
    -- 外部ピン入力の限界を測る試験用の方形波（LA_TESTGEN の構成だけ。CSR la_testgen。ほかは 0・0 = 駆動しない）
    testgen_out      : out std_logic;
    testgen_oe       : out std_logic
  );
end entity;

architecture rtl of fprobe_core is
  -- 塊ごとのリセット: 0 = バス・CSR、1 = DAP・デバッグポート・サンプラ、2 = SWO・UART・補助・同期・観測、3 = ロジアナ・
  -- トリガ・クロストリガ、4 = イベント記録・外部メモリ
  signal rst_g : std_logic_vector(0 to 4);
  signal wait_retry : unsigned(15 downto 0);                -- DAP_TransferConfigure の WAIT の再試行（サンプラ・割り込み書き込みも）
  signal bus_req, bus_we, bus_ack, bus_err : std_logic;
  signal bus_addr  : csr_addr_t;
  signal bus_wdata, bus_rdata : csr_word_t;
  -- CSR の窓へ配る番地・書き込み値・書き込みの印（csr_decoder が要求の最初の周期にレジスタに取った写し。2026-10-29）
  signal c_we : std_logic;
  signal c_addr : csr_addr_t;
  signal c_wdata : csr_word_t;
  signal s_req, s_ack, s_err : std_logic_vector(CSR_WINDOWS'range) := (others => '0');
  signal s_rdata : csr_word_array_t(CSR_WINDOWS'range) := (others => (others => '0'));

  signal time_count : unsigned(63 downto 0);
  signal time_lo    : std_logic_vector(31 downto 0);
  signal time_read  : std_logic;
  signal time_hi    : std_logic_vector(31 downto 0);
  signal level_s1, level_s : std_logic_vector(7 downto 0) := (others => '0');

  signal swd_enable, swd_data_phase, swd_cmd_valid, swd_cmd_ready, swd_rsp_valid, swd_rsp_parity : std_logic;
  signal swd_half_period : unsigned(15 downto 0);
  signal swd_turnaround  : natural range 1 to 4;
  signal swd_idle_cycles : natural range 0 to 255;
  signal swd_sample_delay : std_logic_vector(2 downto 0);
  signal swd_cmd_cond, phy_cmd_cond, port_cmd_cond : std_logic;   -- 前の転送が OK のときだけ受ける（dap_link の先出し）
  signal jtag_sample_delay : std_logic_vector(2 downto 0);
  signal swd_cmd_op      : std_logic_vector(1 downto 0);
  signal swd_cmd_count   : natural range 1 to 64;
  signal swd_cmd_request : std_logic_vector(3 downto 0);
  signal swd_cmd_data, swd_rsp_data : std_logic_vector(63 downto 0);
  signal swd_rsp_time    : unsigned(63 downto 0);          -- 応答の転送の線上の点（debug_port。time-accuracy R5）
  signal swd_rsp_ack     : std_logic_vector(2 downto 0);
  signal uart_baud_now, uart_frame_errors, uart_overruns : unsigned(31 downto 0);
  -- ロジアナ（幅と深さは CSR の生成元 fprobe_map.py の LA_WIDTH / LA_DEPTH と同じ）
  constant LA_WIDTH : positive := 16;
  constant LA_ADDR_BITS : positive := 13;
  signal dap_busy, swdio_oe_i : std_logic;
  signal uart_out_time, swo_out_time : unsigned(47 downto 0);   -- 受けたバイトの開始ビットの辺の時刻（R3）
  signal la_probes : std_logic_vector(LA_WIDTH - 1 downto 0);
  signal la_run, la_capturing, la_triggered, la_done : std_logic;
  signal la_rate : std_logic_vector(15 downto 0);
  signal la_pretrigger, la_start, la_trigger_index : std_logic_vector(31 downto 0);
  signal la_mask, la_value, la_rise, la_fall : std_logic_vector(LA_WIDTH - 1 downto 0);
  -- 外部の入力（拡張ヘッダのピン、続いて専用の入力。U22）とグリッチフィルタ・パルス幅（U21）
  constant EXT_COUNT : positive := LA_PIN_COUNT + LA_INPUT_COUNT;
  signal ext_raw : std_logic_vector(EXT_COUNT - 1 downto 0);
  signal filter_channels : std_logic_vector(LA_WIDTH - 1 downto 0);     -- ビット n = ch n
  signal filter_cycles : std_logic_vector(7 downto 0);
  signal pulse_mode : std_logic_vector(1 downto 0);
  signal pulse_level : std_logic;
  signal pulse_channel : std_logic_vector(3 downto 0);
  signal pulse_min, pulse_max : std_logic_vector(31 downto 0);
  signal pw_enable, pw_event : std_logic;
  signal la_time : unsigned(63 downto 0);
  constant USE_LANES : boolean := LA_LANES > 1 and LA_INTERNAL;       -- 1 周期に LA_LANES サンプル（la_lane_capture）
  constant USE_CAP : boolean := CAP_RATIO > 1 and LA_INTERNAL and not USE_LANES;   -- ロジアナ一式を cap_clk の領域で動かす
  -- 深いバッファを 1 周期に LA_LANES サンプルで記録する（la_deep_buffer の LANES。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md）
  constant USE_DEEP_LANES : boolean := LA_LANES > 1 and not LA_INTERNAL and MEM_ADDR_BITS > 0 and not MEM_TEST;
  -- 外部トリガ（la_ext）の揃え（sys の la_capture・深いバッファの la_front）: la_probes はグリッチフィルタの段を通った後なので、その段数を
  -- la_front の揃えに足す（la_front の注。外部トリガが 1 の周期に端子が変わったピンのサンプルにトリガが付く。時刻の正しさ S1b の N3）
  constant LA_EXT_DELAY : natural := LA_FRONT_EXT_ALIGN + LA_FILTER_DELAY * boolean'pos(LA_FILTER and not USE_CAP);
  function deep_lanes_value return positive is
  begin
    if USE_DEEP_LANES then
      return LA_LANES;
    end if;
    return 1;
  end function;
  constant DEEP_LANES : positive := deep_lanes_value;
  constant LANE_WIDTH : positive := LA_LANES * LA_WIDTH;
  -- la_cap.info: 比（細かいサンプルの数 / sys の周期）とレーンの数
  function info_ratio_value return natural is
  begin
    if USE_LANES or USE_DEEP_LANES then
      return LA_LANES;
    end if;
    return CAP_RATIO;
  end function;
  constant LANES_OR_RATIO : natural := info_ratio_value;
  constant INFO_LANES : natural := LA_LANES * boolean'pos(USE_LANES or USE_DEEP_LANES) + 1 - boolean'pos(USE_LANES or USE_DEEP_LANES);
  signal la_lane_data : std_logic_vector(LANE_WIDTH - 1 downto 0);
  signal la_lane_rd_addr : unsigned(LA_LANE_ADDR_BITS - 1 downto 0);
  signal la_lane_rd_data : std_logic_vector(LANE_WIDTH - 1 downto 0);
  signal la_phase : unsigned(3 downto 0);
  signal la_selftest : std_logic := '0';                                     -- la_cap.control.selftest
  signal tg_en, tg_edge : std_logic := '0';                                  -- la_testgen.control
  signal tg_period, tg_high : std_logic_vector(31 downto 0) := (others => '0');
  signal tg_q, tg_oe : std_logic := '0';
  signal selftest_tog : std_logic := '0';
  signal sys_bit_b : std_logic;                      -- sys の周期ごとに反転する信号、cap の ch LA_PIN_COUNT + 1 に入れる信号
  signal la_latency : unsigned(7 downto 0);                             -- トリガの位相（la_cap_domain。使わない構成は 0）
  signal la_rd_addr : unsigned(LA_ADDR_BITS - 1 downto 0);
  signal la_rd_data : std_logic_vector(LA_WIDTH - 1 downto 0);
  signal la_start_u, la_trigger_u : unsigned(31 downto 0);
  signal swo_run, swo_error, swo_overrun, swo_manchester : std_logic;
  -- UART の制御線（U52）
  signal host_rts_s, host_break_s, flow_auto, flow_rts_claim, flow_cts_claim, uart_rts_n_i, uart_cts_i : std_logic;
  signal flow_rts_pin, flow_cts_pin : std_logic_vector(1 downto 0);
  signal jtag : std_logic;
  signal jtag_dr_before, jtag_dr_after : natural range 0 to 7;
  signal jtag_ir_before, jtag_ir_after : natural range 0 to 63;
  signal jtag_ir_len : natural range 1 to 32;
  signal swo_divisor : unsigned(15 downto 0);
  -- 外部メモリ
  signal mem_ready_s, mem_busy, mem_done, mem_test_start, mem_in_reset, mem_tick : std_logic := '0';
  -- ロジアナの深いバッファ
  signal deep_run, deep_rle, deep_read, deep_capturing, deep_triggered, deep_done, deep_overrun : std_logic := '0';
  signal deep_stream : std_logic := '0';
  signal deep_ps_read : std_logic := '0';
  signal deep_pack : std_logic_vector(2 downto 0) := "000";
  signal deep_step : std_logic_vector(1 downto 0) := "00";
  signal deep_first_ch : std_logic_vector(3 downto 0) := "0000";
  signal deep_gaps, deep_gap_open : std_logic := '0';
  signal deep_gap_count, deep_gap_last : std_logic_vector(31 downto 0) := (others => '0');
  -- 深いバッファのリングの使用量・起点の時刻・符号器の形（la_deep.la_written・ring_*・start_time・info）
  signal deep_la_written, deep_unread, deep_peak : std_logic_vector(31 downto 0) := (others => '0');
  signal deep_start_time : unsigned(63 downto 0) := (others => '0');
  signal deep_start_valid : std_logic := '0';
  signal deep_latency : unsigned(7 downto 0) := (others => '0');
  signal deep_read_ps : std_logic := '0';
  signal deep_stream_beats : std_logic_vector(31 downto 0) := (others => '0');
  signal deep_reading, deep_triggered_prev : std_logic := '0';
  signal deep_slot : std_logic_vector(2 downto 0) := (others => '0');
  signal deep_post, deep_first, deep_count : std_logic_vector(31 downto 0);
  signal deep_beat, deep_written : std_logic_vector(31 downto 0) := (others => '0');
  signal deep_time : unsigned(63 downto 0) := (others => '0');
  signal deep_region : std_logic := '0';
  signal deep_ring_beats : std_logic_vector(31 downto 0) := (others => '0');
  -- イベント記録
  signal events_enable : std_logic := '0';
  signal events_written : std_logic_vector(63 downto 0) := (others => '0');            -- 書いた数（64 ビット）
  signal events_written_hi : std_logic_vector(31 downto 0) := (others => '0');         -- 上位。written を読んだ時点の値を保持
  signal events_lost, events_capacity : std_logic_vector(31 downto 0) := (others => '0');
  signal event_valid : std_logic := '0';
  signal event_data : std_logic_vector(127 downto 0) := (others => '0');
  signal byte_lost : unsigned(31 downto 0) := (others => '0');
  -- 補助マイコンの計測値（イベント 6 として 1 件保持）
  signal aux_valid : std_logic := '0';
  signal aux_ch : natural range 0 to 3;
  signal aux_value : unsigned(13 downto 0);
  signal aux_time  : unsigned(47 downto 0);
  -- 対象の FPGA の観測（イベント 8・9 として 1 件保持）
  signal obs_valid, obs_sync : std_logic := '0';
  signal obs_data, obs_addr : std_logic_vector(31 downto 0) := (others => '0');
  signal obs_time : std_logic_vector(47 downto 0) := (others => '0');
  -- ボードの間の時刻の同期の組（イベント 10 として 1 件保持）
  signal sync_valid : std_logic := '0';
  signal sync_local : std_logic_vector(47 downto 0) := (others => '0');
  signal sync_parent : std_logic_vector(63 downto 0) := (others => '0');
  signal mem_base, mem_count, mem_seed : std_logic_vector(31 downto 0);
  signal mem_errors, mem_first, mem_wc, mem_rc : std_logic_vector(31 downto 0) := (others => '0');
  -- 割り込み・モニタ・クロストリガ
  signal phy_cmd_valid, phy_cmd_ready, dap_rsp_valid, dap_fire : std_logic;
  signal phy_cmd_op : std_logic_vector(1 downto 0);
  signal phy_cmd_count : natural range 1 to 64;
  signal phy_cmd_request : std_logic_vector(3 downto 0);
  signal phy_cmd_data : std_logic_vector(63 downto 0);
  signal select_shadow : std_logic_vector(31 downto 0);
  signal csw_shadow, tar_shadow : std_logic_vector(31 downto 0);
  signal shadow_valid : std_logic;
  signal shadow_ap : std_logic_vector(7 downto 0);
  -- DAP サンプラ（swd_injector と debug_port の間）
  signal port_cmd_valid, port_cmd_ready, up_rsp_valid, up_busy : std_logic;
  signal port_cmd_op : std_logic_vector(1 downto 0);
  signal port_cmd_count : natural range 1 to 64;
  signal port_cmd_request : std_logic_vector(3 downto 0);
  signal port_cmd_data : std_logic_vector(63 downto 0);
  signal smp_valid, smp_busy, smp_run : std_logic := '0';
  signal smp_held, smp_handed : std_logic;                  -- サンプラが線を持っている・直接渡した線の SELECT・CSW が分かっている（swd_injector へ）
  signal smp_addr, smp_data : std_logic_vector(31 downto 0) := (others => '0');
  signal smp_ack, smp_last_ack : std_logic_vector(2 downto 0) := (others => '0');
  signal smp_index : unsigned(6 downto 0) := (others => '0');     -- 標本の番号（か所 × 16 + 語）
  signal smp_time : unsigned(47 downto 0) := (others => '0');     -- 標本の時刻（読みに行った転送の線上の点。time-accuracy R6）
  signal smp_rec_valid : std_logic := '0';                        -- サンプラのイベントの件（種類 7・17・18）
  signal smp_rec_kind : std_logic_vector(7 downto 0) := (others => '0');
  signal smp_rec_word : std_logic_vector(119 downto 0) := (others => '0');
  signal inj_ap : std_logic_vector(7 downto 0);
  signal inj_addr, inj_value : std_logic_vector(31 downto 0);
  signal inj_busy, inj_written : std_logic;
  signal inj_held : std_logic := '0';                  -- 割り込み書き込みが PHY を借りている（サンプラの直接の受け渡し）
  signal inj_result : std_logic_vector(1 downto 0);
  signal inj_ack : std_logic_vector(2 downto 0);
  signal inj_count : unsigned(15 downto 0);
  signal inj_transfers : unsigned(7 downto 0);         -- 停止の書き込みまでの転送数（イベント 11。R20）
  signal inj_words : unsigned(3 downto 0);             -- 最後の要求で受理された語の数
  signal inj_w_addr, inj_w_value : std_logic_vector(31 downto 0);   -- 書いた語（イベント 11）
  signal inj_w_time : unsigned(63 downto 0);                        -- その語の DRW の線上の点
  signal inject_first_time : unsigned(63 downto 0) := (others => '0');
  signal match_addr, match_addr_mask, match_data, match_data_mask : std_logic_vector(31 downto 0);
  signal match_on_write, match_on_read, match : std_logic;
  signal ev_valid, ev_parity : std_logic;
  signal ev_request : std_logic_vector(3 downto 0);
  signal ev_ack : std_logic_vector(2 downto 0);
  signal ev_addr, ev_data : std_logic_vector(31 downto 0);
  signal ev_addr_known : std_logic;                      -- 0 = DRW でメモリの番地を追えなかった（種類 12）
  signal ev_time : unsigned(63 downto 0);                  -- その転送の線上の点（dap_monitor）
  signal dap_cfg_word : std_logic_vector(71 downto 0);     -- PHY の設定（イベント 16 の [119:48]。time-accuracy R7）
  signal host_kind : std_logic_vector(7 downto 0);         -- ホストの転送のイベントの種類（1 か 12）
  signal route_to_la, route_to_inject, sources : std_logic_vector(5 downto 0);
  signal smp_match : std_logic;                     -- サンプラの値の一致（クロストリガの源 bit5。R21）
  -- 始動の列（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md §3.3）: 成功で値の一致の門を開ける
  signal inj_kind_start, inj_done : std_logic;
  signal start_ok : std_logic := '0';                  -- 始動の列が成功で終わった周期
  signal start_first_time, start_last_time : unsigned(63 downto 0) := (others => '0');
  signal obs_match : std_logic;                     -- FPGA の観測の値の一致（クロストリガの源 bit4）
  -- 多段トリガ
  signal seq_enable, seq_fire, seq_fired : std_logic;
  signal seq_stages : std_logic_vector(2 downto 0);
  signal seq_stage : unsigned(1 downto 0);
  signal seq_a_mask, seq_a_value, seq_a_rise, seq_a_fall : std_logic_vector(15 downto 0);
  signal seq_b_mask, seq_b_value, seq_b_rise, seq_b_fall : std_logic_vector(15 downto 0);
  signal seq_cond0, seq_cond1, seq_cond2, seq_cond3 : std_logic_vector(3 downto 0);
  signal seq_count0, seq_count1, seq_count2, seq_count3 : std_logic_vector(15 downto 0);
  signal seq_within1, seq_within2, seq_within3 : std_logic_vector(31 downto 0);
  signal seq_time : unsigned(63 downto 0) := (others => '0');
  signal host_fire, la_triggered_prev, la_ext, inj_request : std_logic := '0';
  signal inj_trigger, inj_claim : std_logic;
  signal match_count : unsigned(31 downto 0) := (others => '0');
  signal match_time, inject_time : unsigned(63 downto 0) := (others => '0');
  -- 入力のレジスタスライス（ホストリンク側の FIFO と処理部の組み合わせ回路を切り離す）
  signal ctrl_s_valid, ctrl_s_ready, dap_s_valid, dap_s_ready : std_logic;
  signal ctrl_s, dap_s : std_logic_vector(8 downto 0);
  signal uart_s_valid, uart_s_ready : std_logic;
  signal uart_s : std_logic_vector(7 downto 0);
  signal uart_tx_used : std_logic;
  -- 共有の割り算器（DAP の SWJ_Clock・SWO のボーレートと、UART のボーレート）
  signal dap_div_req, dap_div_done, uart_div_req, uart_div_done : std_logic;
  signal dap_div_num, dap_div_den, uart_div_num, uart_div_den, div_quot : unsigned(31 downto 0);
begin
  split_reset_g : if RESET_SPLIT generate
    reset_fanout_i : entity work.reset_fanout
      generic map (COUNT => 5)
      port map (clk => clk, rst => rst, rst_out => rst_g);
  end generate;
  single_reset_g : if not RESET_SPLIT generate
    rst_g <= (others => rst);
  end generate;

  ctrl_slice_i : entity work.stream_slice
    generic map (WIDTH => 9)
    port map (clk => clk, rst => rst_g(0), in_valid => ctrl_in_valid, in_ready => ctrl_in_ready,
              in_data => ctrl_in_last & ctrl_in_data, out_valid => ctrl_s_valid, out_ready => ctrl_s_ready,
              out_data => ctrl_s);

  control_i : entity work.control_plane
    port map (clk => clk, rst => rst_g(0),
              in_valid => ctrl_s_valid, in_ready => ctrl_s_ready, in_data => ctrl_s(7 downto 0),
              in_last => ctrl_s(8), out_valid => ctrl_out_valid, out_ready => ctrl_out_ready,
              out_data => ctrl_out_data, out_last => ctrl_out_last,
              bus_req => bus_req, bus_we => bus_we, bus_addr => bus_addr, bus_wdata => bus_wdata,
              bus_ack => bus_ack, bus_err => bus_err, bus_rdata => bus_rdata);

  decoder_i : entity work.csr_decoder
    generic map (WINDOWS => CSR_WINDOWS)
    port map (clk => clk, rst => rst_g(0), bus_req => bus_req, bus_we => bus_we, bus_addr => bus_addr, bus_wdata => bus_wdata,
              bus_ack => bus_ack, bus_err => bus_err, bus_rdata => bus_rdata,
              s_we => c_we, s_addr => c_addr, s_wdata => c_wdata,
              s_req => s_req, s_ack => s_ack, s_err => s_err, s_rdata => s_rdata);

  system_i : entity work.csr_system
    port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_SYSTEM), bus_we => c_we,
              bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_SYSTEM),
              bus_err => s_err(CSR_WIN_SYSTEM), bus_rdata => s_rdata(CSR_WIN_SYSTEM),
              scratch => open);

  timebase_i : entity work.timebase
    port map (clk => clk, rst => rst_g(0), count => time_count, hold => time_read, held_hi => time_hi);

  time_lo <= std_logic_vector(time_count(31 downto 0));

  timebase_csr_i : entity work.csr_timebase
    port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_TIMEBASE), bus_we => c_we,
              bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_TIMEBASE),
              bus_err => s_err(CSR_WIN_TIMEBASE), bus_rdata => s_rdata(CSR_WIN_TIMEBASE),
              time_lo => time_lo, time_lo_read => time_read, time_hi => time_hi);

  gpio_i : entity work.csr_gpio
    port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_GPIO), bus_we => c_we,
              bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_GPIO),
              bus_err => s_err(CSR_WIN_GPIO), bus_rdata => s_rdata(CSR_WIN_GPIO),
              drive => gpio_drive, enable => gpio_enable, level => level_s, io => level_s);

  process (clk)
  begin
    if rising_edge(clk) then
      level_s1 <= gpio_level;
      level_s  <= level_s1;
    end if;
  end process;

  dap_slice_i : entity work.stream_slice
    generic map (WIDTH => 9)
    port map (clk => clk, rst => rst_g(1), in_valid => dap_in_valid, in_ready => dap_in_ready,
              in_data => dap_in_last & dap_in_data, out_valid => dap_s_valid, out_ready => dap_s_ready,
              out_data => dap_s);

  dap_i : entity work.dap_processor
    generic map (CLK_HZ => CLK_HZ, HAS_JTAG => HAS_JTAG, HAS_SWO_MANCHESTER => HAS_SWO_MANCHESTER,
                 SHARED_DIVIDER => true)
    port map (clk => clk, rst => rst_g(1), packet_size => packet_size,
              in_valid => dap_s_valid, in_ready => dap_s_ready, in_data => dap_s(7 downto 0),
              in_last => dap_s(8), out_valid => dap_out_valid, out_ready => dap_out_ready,
              out_data => dap_out_data, out_last => dap_out_last,
              swd_enable => swd_enable, swd_half_period => swd_half_period,
              swd_turnaround => swd_turnaround, swd_data_phase => swd_data_phase,
              swd_idle_cycles => swd_idle_cycles, swd_cmd_valid => swd_cmd_valid,
              swd_cmd_ready => swd_cmd_ready, swd_cmd_cond => swd_cmd_cond, swd_cmd_op => swd_cmd_op,
              swd_cmd_count => swd_cmd_count,
              swd_cmd_request => swd_cmd_request, swd_cmd_data => swd_cmd_data,
              swd_rsp_valid => dap_rsp_valid, swd_rsp_ack => swd_rsp_ack, swd_rsp_data => swd_rsp_data,
              swd_rsp_parity => swd_rsp_parity, nreset_low => nreset_low, nreset_in => nreset_i,
              wait_retry_out => wait_retry,
              jtag => jtag, jtag_dr_before => jtag_dr_before, jtag_dr_after => jtag_dr_after,
              jtag_ir_before => jtag_ir_before, jtag_ir_after => jtag_ir_after, jtag_ir_len => jtag_ir_len,
              swo_run => swo_run, swo_divisor => swo_divisor, swo_manchester => swo_manchester,
              swo_error => swo_error, swo_overrun => swo_overrun, busy => dap_busy,
              div_req => dap_div_req, div_num => dap_div_num, div_den => dap_div_den,
              div_done => dap_div_done, div_quot => div_quot);

  -- 割り算器は 1 つを DAP（優先）と UART ブリッジで共有する（どちらもまれ: SWJ_Clock・SWO・ボーレートの変更）
  divider_i : entity work.udiv_shared
    generic map (WIDTH => 32)
    port map (clk => clk, rst => rst_g(1),
              p0_req => dap_div_req, p0_num => dap_div_num, p0_den => dap_div_den, p0_done => dap_div_done,
              p1_req => uart_div_req, p1_num => uart_div_num, p1_den => uart_div_den, p1_done => uart_div_done,
              quotient => div_quot);

  swo_i_rx : entity work.swo_receiver
    generic map (HAS_MANCHESTER => HAS_SWO_MANCHESTER)
    port map (clk => clk, rst => rst_g(2), run => swo_run, manchester => swo_manchester, divisor => swo_divisor, swo => swo_i,
              time => time_count(47 downto 0),
              out_valid => swo_out_valid, out_ready => swo_out_ready, out_data => swo_out_data, out_time => swo_out_time,
              flush => swo_flush, error => swo_error, overrun => swo_overrun);

  xtrig_inject : if XTRIG and INJECTOR generate
    signal extra_f, index_f : std_logic_vector(2 downto 0);
    signal tbl_addr, tbl_value : std_logic_vector(31 downto 0);
    signal tbl_write : std_logic;
    signal st_first, st_last : std_logic_vector(2 downto 0);
    signal st_go, st_pending : std_logic;
    signal st_result : std_logic_vector(1 downto 0) := "00";
    signal st_words : unsigned(3 downto 0) := (others => '0');
    signal st_count : unsigned(15 downto 0) := (others => '0');
  begin
  inject_i : entity work.swd_injector
    port map (clk => clk, rst => rst_g(1), wait_retry => wait_retry, request => inj_request, enable => swd_enable, jtag => jtag, dap_busy => dap_busy,
              select_shadow => select_shadow, csw_shadow => csw_shadow, tar_shadow => tar_shadow,
              shadow_valid => shadow_valid, shadow_ap => shadow_ap, ap => inj_ap, addr => inj_addr, value => inj_value,
              busy => inj_busy, held => inj_held, written => inj_written, result => inj_result, last_ack => inj_ack,
              count => inj_count, transfers => inj_transfers, written_words => inj_words,
              written_addr => inj_w_addr, written_value => inj_w_value, written_time => inj_w_time,
              extra => to_integer(unsigned(extra_f)), tbl_write => tbl_write, tbl_index => to_integer(unsigned(index_f)),
              tbl_addr => tbl_addr, tbl_value => tbl_value,
              line_held => smp_held, line_handed => smp_handed,
              start_request => st_go, start_first => to_integer(unsigned(st_first)),
              start_last => to_integer(unsigned(st_last)), start_pending => st_pending, kind_start => inj_kind_start,
              done => inj_done,
              dap_cmd_valid => swd_cmd_valid, dap_cmd_ready => swd_cmd_ready, dap_cmd_cond => swd_cmd_cond,
              dap_cmd_op => swd_cmd_op,
              dap_cmd_count => swd_cmd_count, dap_cmd_req => swd_cmd_request, dap_cmd_data => swd_cmd_data,
              dap_rsp_valid => dap_rsp_valid,
              phy_cmd_valid => phy_cmd_valid, phy_cmd_ready => phy_cmd_ready, phy_cmd_cond => phy_cmd_cond,
              phy_cmd_op => phy_cmd_op,
              phy_cmd_count => phy_cmd_count, phy_cmd_req => phy_cmd_request, phy_cmd_data => phy_cmd_data,
              phy_rsp_valid => up_rsp_valid, phy_rsp_ack => swd_rsp_ack,
              phy_rsp_data => swd_rsp_data(31 downto 0), phy_rsp_par => swd_rsp_parity, phy_rsp_time => swd_rsp_time);
  inject_csr_i : entity work.csr_inject
    port map (clk => clk, rst => rst_g(3), bus_req => s_req(CSR_WIN_INJECT), bus_we => c_we, bus_addr => c_addr,
              bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_INJECT), bus_err => s_err(CSR_WIN_INJECT),
              bus_rdata => s_rdata(CSR_WIN_INJECT),
              control_extra => extra_f, entry => index_f, addr => tbl_addr, value => tbl_value, entry_write => tbl_write,
              status_written_words => std_logic_vector(inj_words),
              first_time_lo => std_logic_vector(inject_first_time(31 downto 0)),
              first_time_hi => std_logic_vector(inject_first_time(63 downto 32)),
              start_first => st_first, start_last => st_last, start_go => st_go,
              start_status_pending => st_pending, start_status_result => st_result,
              start_status_written_words => std_logic_vector(st_words), start_status_count => std_logic_vector(st_count),
              start_first_time_lo => std_logic_vector(start_first_time(31 downto 0)),
              start_first_time_hi => std_logic_vector(start_first_time(63 downto 32)),
              start_last_time_lo => std_logic_vector(start_last_time(31 downto 0)),
              start_last_time_hi => std_logic_vector(start_last_time(63 downto 32)));
  -- 始動の列の結果を残す（割り込み書き込みの result・written_words は次の要求で変わる）
  process (clk)
  begin
    if rising_edge(clk) then
      if rst_g(3) = '1' then
        st_result <= "00";
        st_words  <= (others => '0');
        st_count  <= (others => '0');
      elsif inj_done = '1' and inj_kind_start = '1' then
        st_result <= inj_result;
        st_words  <= inj_words;
        st_count  <= st_count + 1;
      end if;
    end if;
  end process;
  -- サンプラへ: 割り込み書き込みがこれから線を求める（源の発火の周期と、登録した要求の周期。その次の周期から
  -- swd_injector の busy が立つ）。サンプラは up_busy の間、次のか所を始めずに境界で待つので、一致した標本のか所を
  -- 読み終えた境界で線を渡す（stop-precision-design §3.2。値の一致は標本の 2 周期後、サンプラが次のか所を始めるのは
  -- 標本の 3 周期後なので、要求を登録する前の発火の周期から含める）
  inj_claim <= inj_trigger or inj_request;
  end generate;

  -- 故障注入が無い（クロストリガごと無いか、構成で外した）: DAP の命令はそのまま PHY へ。状態の result = 3（構成に無い）
  no_xtrig_inject : if not (XTRIG and INJECTOR) generate
    phy_cmd_valid   <= swd_cmd_valid;
    swd_cmd_ready   <= phy_cmd_ready;
    phy_cmd_cond    <= swd_cmd_cond;
    phy_cmd_op      <= swd_cmd_op;
    phy_cmd_count   <= swd_cmd_count;
    phy_cmd_request <= swd_cmd_request;
    phy_cmd_data    <= swd_cmd_data;
    dap_rsp_valid   <= up_rsp_valid;
    inj_busy        <= '0';
    inj_claim       <= '0';
    inj_held        <= '0';
    inj_written     <= '0';
    inj_result      <= "11";
    inj_ack         <= (others => '0');
    inj_count       <= (others => '0');
    inj_transfers   <= (others => '0');
    inj_words       <= (others => '0');
    inj_w_addr      <= (others => '0');
    inj_w_value     <= (others => '0');
    inj_w_time      <= (others => '0');
    inj_kind_start  <= '0';
    inj_done        <= '0';
    inject_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_INJECT), bus_ack => s_ack(CSR_WIN_INJECT),
                bus_err => s_err(CSR_WIN_INJECT), bus_rdata => s_rdata(CSR_WIN_INJECT));
  end generate;

  -- DAP サンプラ: 上流（dap_processor・swd_injector）の命令の境界で線を借り、周期的にメモリを読む
  sampler_g : if SAMPLER generate
    signal period : std_logic_vector(31 downto 0);
    signal ap : std_logic_vector(7 downto 0);
    signal tbl_write, tbl_enable : std_logic;
    signal jitter : std_logic_vector(3 downto 0);
    signal tbl_index : std_logic_vector(2 downto 0);
    signal tbl_addr : std_logic_vector(31 downto 0);
    signal tbl_words : std_logic_vector(3 downto 0);
    signal rounds, errors, overruns : unsigned(31 downto 0);
    signal check_words : std_logic_vector(6 downto 0);
  begin
    up_busy <= dap_busy or inj_busy or inj_claim;
    smp_i : entity work.dap_sampler
      generic map (CHECK_MAX => SAMPLER_CHECK_MAX)
      port map (clk => clk, rst => rst_g(1), wait_retry => wait_retry, run => smp_run, period => unsigned(period),
                jitter => to_integer(unsigned(jitter)), ap => ap,
                tbl_write => tbl_write, tbl_index => to_integer(unsigned(tbl_index)), tbl_addr => tbl_addr,
                tbl_enable => tbl_enable, tbl_words => tbl_words, check_words => to_integer(unsigned(check_words)),
                enable => swd_enable, jtag => jtag, up_busy => up_busy, line_handed => smp_handed,
                select_shadow => select_shadow, csw_shadow => csw_shadow, tar_shadow => tar_shadow,
                shadow_valid => shadow_valid, shadow_ap => shadow_ap,
                sample_valid => smp_valid, sample_addr => smp_addr, sample_data => smp_data, sample_ack => smp_ack,
                sample_index => smp_index, sample_time => smp_time,
                rec_valid => smp_rec_valid, rec_kind => smp_rec_kind, rec_word => smp_rec_word,
                busy => smp_busy, held => smp_held, rounds => rounds, errors => errors, overruns => overruns,
                restores => open, last_ack => smp_last_ack,     -- restores は試験用（数えない）
                up_injector_held => inj_held, up_injector_ap => inj_ap,
                up_cmd_valid => phy_cmd_valid, up_cmd_ready => phy_cmd_ready, up_cmd_cond => phy_cmd_cond,
                up_cmd_op => phy_cmd_op,
                up_cmd_count => phy_cmd_count, up_cmd_req => phy_cmd_request, up_cmd_data => phy_cmd_data,
                up_rsp_valid => up_rsp_valid,
                phy_cmd_valid => port_cmd_valid, phy_cmd_ready => port_cmd_ready, phy_cmd_cond => port_cmd_cond,
                phy_cmd_op => port_cmd_op,
                phy_cmd_count => port_cmd_count, phy_cmd_req => port_cmd_request, phy_cmd_data => port_cmd_data,
                phy_rsp_valid => swd_rsp_valid, phy_rsp_ack => swd_rsp_ack,
                phy_rsp_data => swd_rsp_data(31 downto 0), phy_rsp_par => swd_rsp_parity, phy_rsp_time => swd_rsp_time);
    csr_sampler_i : entity work.csr_sampler
      port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_SAMPLER), bus_we => c_we, bus_addr => c_addr,
                bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_SAMPLER), bus_err => s_err(CSR_WIN_SAMPLER),
                bus_rdata => s_rdata(CSR_WIN_SAMPLER),
                control_run => smp_run, control_ap => ap, control_jitter => jitter, period => period,
                slot_index => tbl_index, slot_addr => tbl_addr, slot_config_enable => tbl_enable,
                slot_config_words => tbl_words, slot_write => tbl_write,
                rounds => std_logic_vector(rounds), errors => std_logic_vector(errors),
                overruns => std_logic_vector(overruns), status_last_ack => smp_last_ack, status_busy => smp_busy,
                status_held => smp_held, check_words => check_words,
                check_max => std_logic_vector(to_unsigned(SAMPLER_CHECK_MAX, 7)));
  end generate;

  -- サンプラの値の一致（R21）: サンプラの標本が条件に入った瞬間 → クロストリガの源 bit5
  smatch_g : if SAMPLER and SAMPLER_MATCH generate
    signal m_addr, m_addr_mask, m_mask, m_value : std_logic_vector(31 downto 0);
    signal m_fire_index : unsigned(6 downto 0);
    signal m_enable, m_once, m_matched, m_any : std_logic;
    signal m_seen, m_count : unsigned(31 downto 0);
    signal m_time, m_prev : unsigned(47 downto 0);
    signal m_unmatched : std_logic_vector(127 downto 0);
    signal m_after, m_first_sample, m_fire_first, m_started : std_logic;
  begin
    smp_match_i : entity work.sampler_match
      port map (clk => clk, rst => rst_g(1), enable => m_enable,
                match_addr => m_addr, match_addr_mask => m_addr_mask, match_mask => m_mask, match_value => m_value,
                once => m_once, after_start => m_after, first_sample => m_first_sample,
                start => start_ok, start_time => start_first_time(47 downto 0),
                sample_valid => smp_valid, sample_addr => smp_addr, sample_data => smp_data, sample_ack => smp_ack,
                sample_index => smp_index, sample_time => smp_time,
                fire => smp_match, matched => m_matched, seen_any => m_any, seen => m_seen, count => m_count,
                fire_time => m_time, prev_time => m_prev, fire_index => m_fire_index, unmatched_seen => m_unmatched,
                fire_first => m_fire_first, started => m_started);
    csr_smatch_i : entity work.csr_sampler_match
      port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_SAMPLER_MATCH), bus_we => c_we, bus_addr => c_addr,
                bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_SAMPLER_MATCH), bus_err => s_err(CSR_WIN_SAMPLER_MATCH),
                bus_rdata => s_rdata(CSR_WIN_SAMPLER_MATCH),
                addr => m_addr, mask => m_mask, value => m_value, control_enable => m_enable, control_once => m_once,
                control_after_start => m_after, control_first_sample => m_first_sample, control_started => m_started,
                fire_first => m_fire_first,
                control_matched => m_matched, control_seen_any => m_any,
                seen => std_logic_vector(m_seen), count => std_logic_vector(m_count),
                time_lo => std_logic_vector(m_time(31 downto 0)), time_hi => x"0000" & std_logic_vector(m_time(47 downto 32)),
                prev_lo => std_logic_vector(m_prev(31 downto 0)), prev_hi => x"0000" & std_logic_vector(m_prev(47 downto 32)),
                addr_mask => m_addr_mask, fire_index => std_logic_vector(m_fire_index),
                unmatched0 => m_unmatched(31 downto 0), unmatched1 => m_unmatched(63 downto 32),
                unmatched2 => m_unmatched(95 downto 64), unmatched3 => m_unmatched(127 downto 96));
  end generate;

  no_smatch_g : if not (SAMPLER and SAMPLER_MATCH) generate
    smp_match <= '0';
    smatch_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_SAMPLER_MATCH), bus_ack => s_ack(CSR_WIN_SAMPLER_MATCH),
                bus_err => s_err(CSR_WIN_SAMPLER_MATCH), bus_rdata => s_rdata(CSR_WIN_SAMPLER_MATCH));
  end generate;

  -- DAP サンプラが無い: 上流の命令はそのまま PHY へ、サンプラの窓は読むとエラー
  no_sampler_g : if not SAMPLER generate
    port_cmd_valid   <= phy_cmd_valid;
    phy_cmd_ready    <= port_cmd_ready;
    port_cmd_cond    <= phy_cmd_cond;
    port_cmd_op      <= phy_cmd_op;
    port_cmd_count   <= phy_cmd_count;
    port_cmd_request <= phy_cmd_request;
    port_cmd_data    <= phy_cmd_data;
    up_rsp_valid     <= swd_rsp_valid;
    up_busy          <= dap_busy or inj_busy;
    smp_run <= '0'; smp_valid <= '0'; smp_busy <= '0'; smp_held <= '0'; smp_handed <= '0';
    smp_addr <= (others => '0'); smp_data <= (others => '0'); smp_ack <= (others => '0'); smp_last_ack <= (others => '0');
    smp_index <= (others => '0'); smp_time <= (others => '0');
    smp_rec_valid <= '0'; smp_rec_kind <= (others => '0'); smp_rec_word <= (others => '0');
    sampler_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_SAMPLER), bus_ack => s_ack(CSR_WIN_SAMPLER),
                bus_err => s_err(CSR_WIN_SAMPLER), bus_rdata => s_rdata(CSR_WIN_SAMPLER));
  end generate;

  dap_fire <= swd_cmd_valid and swd_cmd_ready;
  host_kind <= x"01" when ev_addr_known = '1' else x"0C";
  monitor_i : entity work.dap_monitor
    port map (clk => clk, rst => rst_g(1), enable => swd_enable, cmd_fire => dap_fire, cmd_op => swd_cmd_op, cmd_req => swd_cmd_request,
              cmd_data => swd_cmd_data(31 downto 0), rsp_valid => dap_rsp_valid, rsp_ack => swd_rsp_ack,
              rsp_data => swd_rsp_data(31 downto 0), rsp_parity => swd_rsp_parity,
              match_addr => match_addr, addr_mask => match_addr_mask, match_data => match_data,
              data_mask => match_data_mask, on_write => match_on_write, on_read => match_on_read,
              select_shadow => select_shadow, csw_shadow => csw_shadow, tar_shadow => tar_shadow,
              shadow_valid => shadow_valid, shadow_ap => shadow_ap, match => match, ev_valid => ev_valid, ev_request => ev_request,
              ev_ack => ev_ack, ev_parity => ev_parity, ev_addr => ev_addr, ev_addr_known => ev_addr_known,
              ev_data => ev_data, rsp_time => swd_rsp_time, ev_time => ev_time);

  port_i : entity work.debug_port
    generic map (HAS_JTAG => HAS_JTAG)
    port map (clk => clk, rst => rst_g(1), enable => swd_enable, jtag => jtag, half_period => swd_half_period,
              turnaround => swd_turnaround, data_phase => swd_data_phase, idle_cycles => swd_idle_cycles,
              sample_delay => to_integer(unsigned(swd_sample_delay)),
              jtag_sample_delay => to_integer(unsigned(jtag_sample_delay)),
              jtag_dr_before => jtag_dr_before, jtag_dr_after => jtag_dr_after,
              jtag_ir_before => jtag_ir_before, jtag_ir_after => jtag_ir_after, jtag_ir_len => jtag_ir_len,
              now => time_count,
              cmd_valid => port_cmd_valid, cmd_ready => port_cmd_ready, cmd_cond => port_cmd_cond, cmd_op => port_cmd_op,
              cmd_count => port_cmd_count, cmd_request => port_cmd_request, cmd_data => port_cmd_data,
              rsp_valid => swd_rsp_valid, rsp_ack => swd_rsp_ack, rsp_data => swd_rsp_data,
              rsp_parity_error => swd_rsp_parity, rsp_time => swd_rsp_time, swclk_tck => swclk_o, swclk_tck_oe => swclk_oe,
              swdio_tms_o => swdio_o, swdio_tms_oe => swdio_oe_i, swdio_tms_lead => swdio_o_lead, swdio_i => swdio_i,
              tdi => tdi_o, tdi_oe => tdi_oe, tdo => tdo_i);
  swdio_oe <= swdio_oe_i;

  uart_slice_i : entity work.stream_slice
    generic map (WIDTH => 8)
    port map (clk => clk, rst => rst_g(2), in_valid => uart_in_valid, in_ready => uart_in_ready, in_data => uart_in_data,
              out_valid => uart_s_valid, out_ready => uart_s_ready, out_data => uart_s);

  host_rts_sync_i : entity work.sync_bit
    port map (clk => clk, d => uart_host_rts, q => host_rts_s);
  host_break_sync_i : entity work.sync_bit
    port map (clk => clk, d => uart_host_break, q => host_break_s);

  uart_i : entity work.uart_bridge
    generic map (CLK_HZ => CLK_HZ, SHARED_DIVIDER => true, FLOW => UART_FLOW)
    port map (clk => clk, rst => rst_g(2), baud => uart_baud, baud_load => uart_baud_load,
              in_valid => uart_s_valid, in_ready => uart_s_ready, in_data => uart_s,
              out_valid => uart_out_valid, out_ready => uart_out_ready, out_data => uart_out_data,
              out_time => uart_out_time, time => time_count(47 downto 0),
              rx => uart_rx, tx => uart_tx, brk => host_break_s, flow_auto => flow_auto, rts_host => host_rts_s,
              cts_n => uart_cts_n, rts_n => uart_rts_n_i, cts => uart_cts_i, current_baud => uart_baud_now,
              frame_errors => uart_frame_errors, overruns => uart_overruns,
              div_req => uart_div_req, div_num => uart_div_num, div_den => uart_div_den,
              div_done => uart_div_done, div_quot => div_quot);

  uart_rts_n <= uart_rts_n_i;
  uart_cts   <= uart_cts_i;
  -- 端子の割り当ては回路のある構成だけ（無い構成の CSR の設定は何もしない）
  uart_rts_claim <= flow_rts_claim when UART_FLOW else '0';
  uart_cts_claim <= flow_cts_claim when UART_FLOW else '0';
  uart_rts_pin   <= flow_rts_pin;
  uart_cts_pin   <= flow_cts_pin;

  -- B29: ブリッジへの入力のストリームで最初の 1 バイトが受け取られた周期から TX を駆動する（ブレークも駆動の
  -- 頼み: 線を Low にするには駆動が要る）。リセットで止める
  uart_tx_enable : process (clk)
  begin
    if rising_edge(clk) then
      if rst_g(2) = '1' then
        uart_tx_used <= '0';
      elsif (uart_s_valid = '1' and uart_s_ready = '1') or host_break_s = '1' then
        uart_tx_used <= '1';
      end if;
    end if;
  end process;
  uart_tx_oe <= uart_tx_used;

  uart_csr_i : entity work.csr_uart
    port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_UART), bus_we => c_we,
              bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_UART),
              bus_err => s_err(CSR_WIN_UART), bus_rdata => s_rdata(CSR_WIN_UART),
              baud => std_logic_vector(uart_baud_now), frame_errors => std_logic_vector(uart_frame_errors),
              overruns => std_logic_vector(uart_overruns),
              flow_auto => flow_auto, flow_rts_claim => flow_rts_claim, flow_rts_pin => flow_rts_pin,
              flow_cts_claim => flow_cts_claim, flow_cts_pin => flow_cts_pin,
              lines_cts => uart_cts_i, lines_rts => not uart_rts_n_i, lines_host_rts => host_rts_s,
              lines_host_break => host_break_s);

  -- 外部の入力: [LA_PIN_COUNT-1:0] = 拡張ヘッダのピン、その上 = 専用の入力
  assert LA_PIN_COUNT + 2 + LA_INPUT_COUNT <= LA_WIDTH
    report "fprobe_core: LA_PIN_COUNT + 2 + LA_INPUT_COUNT exceeds LA_WIDTH" severity failure;
  assert not (USE_LANES or USE_DEEP_LANES) or (not LA_FILTER and not LA_PULSE and LA_LANES mod 2 = 0 and LA_LANES <= 8)
    report "fprobe_core: LA_LANES > 1 needs LA_FILTER = LA_PULSE = false and an even LA_LANES <= 8" severity failure;
  -- sys の構成のパルス幅のトリガは同期化した値（グリッチフィルタの出力）を見る。フィルタが無いと同期化していない値を 1 段で取る
  -- （メタ安定）。ケイパビリティの検査（fprobe_gw.caps）と同じ（時刻の正しさ S1b の N5）
  assert USE_CAP or LA_FILTER or not LA_PULSE
    report "fprobe_core: LA_PULSE needs LA_FILTER (the pulse-width trigger reads the synchronized probes)" severity failure;
  ext_raw(LA_PIN_COUNT - 1 downto 0)  <= la_pins;
  ext_inputs_g : if LA_INPUT_COUNT > 0 generate
    -- 1 周期に複数のサンプルの構成: 多段トリガ（sys）には、その周期の最も新しいレーンを渡す
    ext_raw(EXT_COUNT - 1 downto LA_PIN_COUNT)  <= la_inputs when not USE_LANES else
      la_input_lanes(LA_LANES * LA_INPUT_COUNT - 1 downto (LA_LANES - 1) * LA_INPUT_COUNT);
  end generate;

  -- ロジアナの入力（la_probe_inputs）: 0..LA_PIN_COUNT-1 = 外部ピン、続いて SWDIO をプローブが駆動中、DAP 処理中、専用の入力、残りは 0。
  -- グリッチフィルタ（U21）: 外部の入力の ch のうち filter_channels のものは filter_cycles 周期より短い変化を捨てる
  -- （フィルタした ch だけは cycles - 1 周期遅れる）。フィルタの段（timing_pkg の LA_FILTER_DELAY）を通らない内部の 2 本は、
  -- 同じ段数のレジスタで遅らせて全 ch の時刻を揃える（R2。docs/superpowers/specs/2026-11-01-time-accuracy-design.md）。
  -- 取り込みの領域の構成（USE_CAP）では、フィルタと揃えは la_cap_domain の中にあり、ここは多段トリガ（sys）に渡す素の値
  probe_inputs_i : entity work.la_probe_inputs
    generic map (WIDTH => LA_WIDTH, LA_PIN_COUNT => LA_PIN_COUNT, LA_INPUT_COUNT => LA_INPUT_COUNT,
                 FILTER => LA_FILTER and not USE_CAP, INTERNAL_AGE => 0)
    port map (clk => clk, rst => rst_g(3), ext_raw => ext_raw, internal => dap_busy & swdio_oe_i,
              filter_channels => filter_channels, filter_cycles => unsigned(filter_cycles), probes => la_probes);

  -- パルス幅のトリガ（U21）: ロジアナのトリガの条件に加える（la_front の pw_enable / pw_event）
  pulse_g : if LA_PULSE and not USE_CAP generate
    pulse_i : entity work.la_pulse
      generic map (WIDTH => LA_WIDTH, COUNT_BITS => 32)
      port map (clk => clk, rst => rst_g(3), probes => la_probes, mode => pulse_mode,
                channel => unsigned(pulse_channel), level => pulse_level,
                min_cycles => unsigned(pulse_min), max_cycles => unsigned(pulse_max), event => pw_event);
    pw_enable <= pulse_mode(0) xor pulse_mode(1);           -- mode 1・2（3 は何もしない: la_pulse と同じ）
  end generate;
  no_pulse_g : if not (LA_PULSE and not USE_CAP) generate
    pw_enable <= '0';
    pw_event  <= '0';
  end generate;

  -- ロジアナ一式を cap_clk の領域で動かす（ピンのフィルタ・パルス幅・前段・記録。sys との受け渡しは la_cap_domain の中）
  cap_la : if USE_CAP generate
    la_cap_i : entity work.la_cap_domain
      generic map (WIDTH => LA_WIDTH, DEPTH => 2 ** LA_ADDR_BITS, LA_PIN_COUNT => LA_PIN_COUNT,
                   LA_INPUT_COUNT => LA_INPUT_COUNT, RATIO => CAP_RATIO, LA_FILTER => LA_FILTER, LA_PULSE => LA_PULSE)
      port map (clk => clk, cap_clk => cap_clk, rst => rst_g(3), ext_raw => ext_raw,
                sys_bit_a => swdio_oe_i, sys_bit_b => sys_bit_b, time => time_count, ext_trigger => la_ext,
                run => la_run, rate => unsigned(la_rate), pretrigger => unsigned(la_pretrigger),
                trig_mask => la_mask, trig_value => la_value, trig_rise => la_rise, trig_fall => la_fall,
                filter_channels => filter_channels, filter_cycles => unsigned(filter_cycles),
                pulse_mode => pulse_mode, pulse_level => pulse_level, pulse_channel => unsigned(pulse_channel),
                pulse_min => unsigned(pulse_min), pulse_max => unsigned(pulse_max),
                capturing => la_capturing, triggered => la_triggered, done => la_done,
                start_index => la_start_u, trigger_index => la_trigger_u, trigger_time => la_time,
                trigger_phase => la_phase, latency => la_latency, rd_addr => la_rd_addr, rd_data => la_rd_data);
    la_start         <= std_logic_vector(la_start_u);
    la_trigger_index <= std_logic_vector(la_trigger_u);

    la_mem_cap_i : entity work.la_mem_csr
      generic map (WIDTH => LA_WIDTH, ADDR_BITS => LA_ADDR_BITS)
      port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_LA_MEM), bus_addr => c_addr,
                bus_ack => s_ack(CSR_WIN_LA_MEM), bus_rdata => s_rdata(CSR_WIN_LA_MEM),
                rd_addr => la_rd_addr, rd_data => la_rd_data);
    s_err(CSR_WIN_LA_MEM) <= '0';
  end generate;
  -- 1 周期に LA_LANES サンプル（la_lane_inputs）: 専用の入力はボードの直並列変換のレーン。ピンは LA_FINE_PINS ならレーン、そうでなければ
  -- sys で同期化した値、内部の信号は sys の値を、レーンごとにそのサンプルの細かい辺の前の sys の周期の値にして並べる
  -- （内部の ch を細かい ch の時間軸に揃える。時刻の正しさ S1b の N2: docs/superpowers/specs/2026-11-01-time-accuracy-design.md）
  lane_data_g : if LA_LANES > 1 generate
    lane_inputs_i : entity work.la_lane_inputs
      generic map (WIDTH => LA_WIDTH, LANES => LA_LANES, LA_PIN_COUNT => LA_PIN_COUNT, LA_INPUT_COUNT => LA_INPUT_COUNT,
                   FINE_PINS => LA_FINE_PINS, IN_AGE => LA_LANE_IN_AGE)
      port map (clk => clk, pins => la_pins, pin_lanes => la_pin_lanes, internal => sys_bit_b & swdio_oe_i,
                input_lanes => la_input_lanes, lane_data => la_lane_data);
  end generate;
  no_lane_data_g : if LA_LANES = 1 generate
    la_lane_data <= (others => '0');
  end generate;

  lanes_la : if USE_LANES generate

    la_lane_i : entity work.la_lane_capture
      generic map (WIDTH => LA_WIDTH, LANES => LA_LANES, DEPTH => 2 ** LA_LANE_ADDR_BITS, IN_AGE => LA_LANE_IN_AGE)
      port map (clk => clk, rst => rst_g(3), lane_data => la_lane_data, time => time_count, ext_trigger => la_ext,
                run => la_run, pretrigger => unsigned(la_pretrigger),
                trig_mask => la_mask, trig_value => la_value, trig_rise => la_rise, trig_fall => la_fall,
                capturing => la_capturing, triggered => la_triggered, done => la_done,
                start_index => la_start_u, trigger_index => la_trigger_u, trigger_time => la_time,
                trigger_lane => la_phase, latency => la_latency, rd_addr => la_lane_rd_addr, rd_data => la_lane_rd_data);
    la_start         <= std_logic_vector(la_start_u);
    la_trigger_index <= std_logic_vector(la_trigger_u);

    la_lanes_mem_i : entity work.la_mem_csr
      generic map (WIDTH => LANE_WIDTH, ADDR_BITS => LA_LANE_ADDR_BITS, SLICES => LA_LANES / 2)
      port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_LA_LANES_MEM), bus_addr => c_addr,
                bus_ack => s_ack(CSR_WIN_LA_LANES_MEM), bus_rdata => s_rdata(CSR_WIN_LA_LANES_MEM),
                rd_addr => la_lane_rd_addr, rd_data => la_lane_rd_data);
    s_err(CSR_WIN_LA_LANES_MEM) <= '0';

    -- 1 サンプル 1 ワードの窓 la_mem は無い（読むとエラー。バッファは la_lanes_mem）
    la_mem_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_LA_MEM), bus_ack => s_ack(CSR_WIN_LA_MEM),
                bus_err => s_err(CSR_WIN_LA_MEM), bus_rdata => s_rdata(CSR_WIN_LA_MEM));
  end generate;
  no_lanes_la : if not USE_LANES generate
    la_lane_rd_addr <= (others => '0');
    la_lane_rd_data <= (others => '0');
    la_lanes_mem_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_LA_LANES_MEM), bus_ack => s_ack(CSR_WIN_LA_LANES_MEM),
                bus_err => s_err(CSR_WIN_LA_LANES_MEM), bus_rdata => s_rdata(CSR_WIN_LA_LANES_MEM));
  end generate;
  no_cap_la : if not USE_CAP and not USE_LANES generate
    la_phase <= (others => '0');
    -- 深いバッファを N 並列で記録する構成: la_cap.info.latency は深いバッファの式の遅れ（la_deep.info.latency と同じ）
    la_latency <= deep_latency when USE_DEEP_LANES else (others => '0');
  end generate;

  -- 試験用: sys の周期ごとに反転する信号（selftest = 1 のとき ch LA_PIN_COUNT + 1 に dap_busy の代わりに入れる）
  process (clk)
  begin
    if rising_edge(clk) then
      selftest_tog <= not selftest_tog;
    end if;
  end process;
  sys_bit_b <= selftest_tog when la_selftest = '1' else dap_busy;   -- 段を置かない（dap_busy と同じ時間関係）

  testgen_out <= tg_q;
  testgen_oe  <= tg_oe;

  -- 取り込みのクロックの比とトリガの位相（比 1 の構成には無い: 読むとエラー）
  la_cap_info : if CAP_RATIO > 1 or USE_LANES generate
    la_cap_csr_i : entity work.csr_la_cap
      port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_LA_CAP), bus_we => c_we,
                bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_LA_CAP),
                bus_err => s_err(CSR_WIN_LA_CAP), bus_rdata => s_rdata(CSR_WIN_LA_CAP),
                info_ratio => std_logic_vector(to_unsigned(LANES_OR_RATIO, 4)),
                info_lanes => std_logic_vector(to_unsigned(INFO_LANES, 4)),
                info_trigger_phase => std_logic_vector(la_phase),
                info_latency => std_logic_vector(la_latency),
                control_selftest => la_selftest);
  end generate;
  no_la_cap_info : if not (CAP_RATIO > 1 or USE_LANES) generate
    la_selftest <= '0';
    la_cap_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_LA_CAP), bus_ack => s_ack(CSR_WIN_LA_CAP),
                bus_err => s_err(CSR_WIN_LA_CAP), bus_rdata => s_rdata(CSR_WIN_LA_CAP));
  end generate;

  -- 試験用の方形波（ケイパビリティ la_testgen）。刻むクロックは、取り込みが速い構成（CAP_RATIO > 1）なら cap_clk、そうでなければ clk。
  -- クロックを式で切り替えず、generate で配線を選ぶ（clk と cap_clk が同じでも la_testgen の受け渡し cdc_value は値を正しく届ける）
  la_testgen_csr : if LA_TESTGEN generate
    la_testgen_csr_i : entity work.csr_la_testgen
      port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_LA_TESTGEN), bus_we => c_we,
                bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_LA_TESTGEN),
                bus_err => s_err(CSR_WIN_LA_TESTGEN), bus_rdata => s_rdata(CSR_WIN_LA_TESTGEN),
                control_enable => tg_en, control_edge => tg_edge, period => tg_period, high => tg_high);
  end generate;
  testgen_fast : if LA_TESTGEN and CAP_RATIO > 1 generate
    testgen_i : entity work.la_testgen
      port map (clk => clk, rst => rst_g(0), cap_clk => cap_clk, enable => tg_en, edge_sel => tg_edge,
                period => tg_period(15 downto 0), high => tg_high(15 downto 0), out_q => tg_q, out_oe => tg_oe);
  end generate;
  testgen_sys : if LA_TESTGEN and CAP_RATIO <= 1 generate
    testgen_i : entity work.la_testgen
      port map (clk => clk, rst => rst_g(0), cap_clk => clk, enable => tg_en, edge_sel => tg_edge,
                period => tg_period(15 downto 0), high => tg_high(15 downto 0), out_q => tg_q, out_oe => tg_oe);
  end generate;
  no_testgen : if not LA_TESTGEN generate
    tg_q      <= '0';
    tg_oe     <= '0';
    tg_en     <= '0';
    tg_edge   <= '0';
    tg_period <= (others => '0');
    tg_high   <= (others => '0');
    la_testgen_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_LA_TESTGEN), bus_ack => s_ack(CSR_WIN_LA_TESTGEN),
                bus_err => s_err(CSR_WIN_LA_TESTGEN), bus_rdata => s_rdata(CSR_WIN_LA_TESTGEN));
  end generate;

  internal_la : if LA_INTERNAL and not USE_CAP and not USE_LANES generate
  la_i : entity work.la_capture
    generic map (WIDTH => LA_WIDTH, DEPTH => 2 ** LA_ADDR_BITS, EXT_DELAY => LA_EXT_DELAY)
    port map (clk => clk, rst => rst_g(3), probes => la_probes, time => time_count, ext_trigger => la_ext,
              run => la_run, rate => unsigned(la_rate), pretrigger => unsigned(la_pretrigger),
              trig_mask => la_mask, trig_value => la_value, trig_rise => la_rise, trig_fall => la_fall,
              pw_enable => pw_enable, pw_event => pw_event,
              capturing => la_capturing, triggered => la_triggered, done => la_done,
              start_index => la_start_u, trigger_index => la_trigger_u, trigger_time => la_time,
              rd_addr => la_rd_addr, rd_data => la_rd_data);
  la_start         <= std_logic_vector(la_start_u);
  la_trigger_index <= std_logic_vector(la_trigger_u);

  la_mem_i : entity work.la_mem_csr
    generic map (WIDTH => LA_WIDTH, ADDR_BITS => LA_ADDR_BITS)
    port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_LA_MEM), bus_addr => c_addr,
              bus_ack => s_ack(CSR_WIN_LA_MEM), bus_rdata => s_rdata(CSR_WIN_LA_MEM),
              rd_addr => la_rd_addr, rd_data => la_rd_data);
  s_err(CSR_WIN_LA_MEM) <= '0';                            -- バッファの窓は範囲外も 0 で答える（エラーにしない）
  end generate;

  -- 内蔵ロジアナが無い: 状態は 0、バッファの窓は読むとエラー（ケイパビリティの LA_DEPTH = 0）
  no_internal_la : if not LA_INTERNAL generate
    signal ack : std_logic := '0';
  begin
    la_capturing     <= '0';
    la_triggered     <= '0';
    la_done          <= '0';
    la_start         <= (others => '0');
    la_trigger_index <= (others => '0');
    la_time          <= (others => '0');
    process (clk)
    begin
      if rising_edge(clk) then
        ack <= s_req(CSR_WIN_LA_MEM) and not ack;
      end if;
    end process;
    s_ack(CSR_WIN_LA_MEM)   <= ack;
    s_err(CSR_WIN_LA_MEM)   <= ack;                          -- 応答の周期だけ（csr_bus_pkg の約束）
    s_rdata(CSR_WIN_LA_MEM) <= (others => '0');
  end generate;

  la_csr_i : entity work.csr_la
    port map (clk => clk, rst => rst_g(3), bus_req => s_req(CSR_WIN_LA), bus_we => c_we,
              bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_LA),
              bus_err => s_err(CSR_WIN_LA), bus_rdata => s_rdata(CSR_WIN_LA),
              control_run => la_run, rate => la_rate, pretrigger => la_pretrigger,
              trig_mask => la_mask, trig_value => la_value, trig_rise => la_rise, trig_fall => la_fall,
              status_capturing => la_capturing, status_triggered => la_triggered, status_done => la_done,
              start_index => la_start, trigger_index => la_trigger_index,
              trigger_time_lo => std_logic_vector(la_time(31 downto 0)),
              trigger_time_hi => std_logic_vector(la_time(63 downto 32)),
              filter_channels => filter_channels, filter_cycles => filter_cycles,
              pulse_mode => pulse_mode, pulse_level => pulse_level, pulse_channel => pulse_channel,
              pulse_min => pulse_min, pulse_max => pulse_max);

  caps_i : entity work.caps_rom
    generic map (CONTENT => CAPS, WINDOW_SIZE => CSR_WINDOWS(CSR_WIN_CAPS).size)
    port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_CAPS), bus_addr => c_addr,
              bus_ack => s_ack(CSR_WIN_CAPS), bus_rdata => s_rdata(CSR_WIN_CAPS));
  s_err(CSR_WIN_CAPS) <= '0';                              -- ROM は範囲外も 0 で答える（エラーにしない）
  -- 多段トリガ: パターン A / B（ロジアナと同じピン）・番地一致・ホストの指令を段ごとの条件で数える
  trig_seq_g : if TRIG_SEQ generate
  seq_i : entity work.trig_seq
    generic map (WIDTH => 16)
    port map (clk => clk, rst => rst_g(3), probes => la_probes, match => match, host => host_fire,
              enable => seq_enable, stages => unsigned(seq_stages),
              a_mask => seq_a_mask, a_value => seq_a_value, a_rise => seq_a_rise, a_fall => seq_a_fall,
              b_mask => seq_b_mask, b_value => seq_b_value, b_rise => seq_b_rise, b_fall => seq_b_fall,
              cond0 => seq_cond0, cond1 => seq_cond1, cond2 => seq_cond2, cond3 => seq_cond3,
              count0 => unsigned(seq_count0), count1 => unsigned(seq_count1),
              count2 => unsigned(seq_count2), count3 => unsigned(seq_count3),
              within1 => unsigned(seq_within1), within2 => unsigned(seq_within2), within3 => unsigned(seq_within3),
              fire => seq_fire, fired => seq_fired, stage => seq_stage);
  process (clk)
  begin
    if rising_edge(clk) then
      if seq_fire = '1' then
        seq_time <= time_count;
      end if;
    end if;
  end process;

  trigseq_csr_i : entity work.csr_trigseq
    port map (clk => clk, rst => rst_g(3), bus_req => s_req(CSR_WIN_TRIGSEQ), bus_we => c_we,
              bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_TRIGSEQ),
              bus_err => s_err(CSR_WIN_TRIGSEQ), bus_rdata => s_rdata(CSR_WIN_TRIGSEQ),
              control_enable => seq_enable, control_stages => seq_stages,
              status_stage => std_logic_vector(seq_stage), status_fired => seq_fired,
              pattern_a_mask => seq_a_mask, pattern_a_value => seq_a_value,
              edge_a_rise => seq_a_rise, edge_a_fall => seq_a_fall,
              pattern_b_mask => seq_b_mask, pattern_b_value => seq_b_value,
              edge_b_rise => seq_b_rise, edge_b_fall => seq_b_fall,
              stage0_cond => seq_cond0, stage0_count => seq_count0, stage1_cond => seq_cond1, stage1_count => seq_count1,
              stage2_cond => seq_cond2, stage2_count => seq_count2, stage3_cond => seq_cond3, stage3_count => seq_count3,
              within1 => seq_within1, within2 => seq_within2, within3 => seq_within3,
              fire_time_lo => std_logic_vector(seq_time(31 downto 0)),
              fire_time_hi => std_logic_vector(seq_time(63 downto 32)));
  end generate;

  no_trig_seq : if not TRIG_SEQ generate
    seq_fire  <= '0';
    trigseq_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_TRIGSEQ), bus_ack => s_ack(CSR_WIN_TRIGSEQ),
                bus_err => s_err(CSR_WIN_TRIGSEQ), bus_rdata => s_rdata(CSR_WIN_TRIGSEQ));
  end generate;

  -- クロストリガ: 源（bit0 ロジアナのトリガ、bit1 番地一致、bit2 ホストの指令、bit3 多段トリガ、bit4 FPGA の観測の
  -- 値の一致、bit5 サンプラの値の一致）→ 動作。1 周期で届く
  inj_trigger <= or (sources and route_to_inject);
  sources <= smp_match & obs_match & seq_fire & host_fire & match & ((la_triggered and not la_triggered_prev) or
                                    (deep_triggered and not deep_triggered_prev));
  process (clk)
  begin
    if rising_edge(clk) then
      la_triggered_prev   <= la_triggered;
      deep_triggered_prev <= deep_triggered;
      la_ext      <= or (sources and route_to_la);
      inj_request <= inj_trigger;
      if rst_g(3) = '1' then
        match_count <= (others => '0');
      elsif match = '1' then
        match_count <= match_count + 1;
        match_time  <= ev_time;                          -- 一致した転送の線上の点（match は ev と同じ周期）
      end if;
      -- 語の時刻: クロストリガの要求は inject_time・inject_first_time、始動の列は start_*_time（見張りの停止要求の時刻を
      -- 再開の時刻で上書きしない。halt-watch-fpga-restart-design §3.2）
      if inj_written = '1' and inj_kind_start = '0' then
        inject_time <= inj_w_time;                       -- 最後の語の DRW の線上の点（time-accuracy R5）
        if inj_words = 1 then
          inject_first_time <= inj_w_time;               -- 最初の語
        end if;
      end if;
      if inj_written = '1' and inj_kind_start = '1' then
        start_last_time <= inj_w_time;
        if inj_words = 1 then
          start_first_time <= inj_w_time;
        end if;
      end if;
      -- 始動の列が成功で終わった周期の次に、値の一致の門を開ける（その後に始めたサンプラのか所の標本だけが比べられる）
      start_ok <= inj_done and inj_kind_start and not inj_result(0) and not inj_result(1);
    end if;
  end process;

  -- 外部メモリ: 状態とメモリ試験
  mem_csr_i : entity work.csr_mem
    port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_MEM), bus_we => c_we,
              bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_MEM),
              bus_err => s_err(CSR_WIN_MEM), bus_rdata => s_rdata(CSR_WIN_MEM),
              status_ready => mem_ready_s, status_busy => mem_busy, status_done => mem_done,
              status_in_reset => mem_in_reset, status_clock_tick => mem_tick,
              command_test => mem_test_start, test_base => mem_base, test_count => mem_count,
              test_seed => mem_seed, errors => mem_errors, first_error => mem_first,
              write_cycles => mem_wc, read_cycles => mem_rc);

  deep_csr_i : entity work.csr_la_deep
    port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_LA_DEEP), bus_we => c_we,
              bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_LA_DEEP),
              bus_err => s_err(CSR_WIN_LA_DEEP), bus_rdata => s_rdata(CSR_WIN_LA_DEEP),
              control_run => deep_run, control_rle => deep_rle, control_stream => deep_stream,
              control_ps_read => deep_ps_read, control_pack => deep_pack, control_step => deep_step,
              control_first => deep_first_ch, control_gaps => deep_gaps,
              post_beats => deep_post, stream_beats => deep_stream_beats,
              status_capturing => deep_capturing, status_triggered => deep_triggered, status_done => deep_done,
              status_overrun => deep_overrun, status_reading => deep_reading, status_gap_open => deep_gap_open,
              status_trigger_slot => deep_slot,
              trigger_beat => deep_beat, written => deep_written,
              trigger_time_lo => std_logic_vector(deep_time(31 downto 0)),
              trigger_time_hi => std_logic_vector(deep_time(63 downto 32)),
              read_first => deep_first, read_count => deep_count, command_read => deep_read, command_read_ps => deep_read_ps,
              read_region => deep_region, ring_beats => deep_ring_beats,
              la_written => deep_la_written, ring_unread => deep_unread, ring_peak => deep_peak,
              start_time_lo => std_logic_vector(deep_start_time(31 downto 0)),
              start_time_hi => std_logic_vector(deep_start_time(63 downto 32)),
              info_lanes => std_logic_vector(to_unsigned(DEEP_LANES, 4)), info_latency => std_logic_vector(deep_latency),
              info_start_valid => deep_start_valid, gap_last_beat => deep_gap_last, gaps => deep_gap_count);

  debug_csr_i : entity work.csr_debug
    port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_DEBUG), bus_we => c_we,
              bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_DEBUG),
              bus_err => s_err(CSR_WIN_DEBUG), bus_rdata => s_rdata(CSR_WIN_DEBUG),
              sample_delay_swd => swd_sample_delay, sample_delay_jtag => jtag_sample_delay);

  -- 補助マイコン（Pico）の計測値: ch 0（ターゲット電圧）の最新値を CSR に、全チャンネルの値をイベント 6 に
  aux_g : if AUX generate
    signal target_mv : std_logic_vector(13 downto 0) := (others => '0');
    signal samples, errors : unsigned(31 downto 0) := (others => '0');
  begin
    aux_i : entity work.aux_link
      generic map (CLK_HZ => CLK_HZ, CHANNELS => 4)
      port map (clk => clk, rst => rst_g(2), rx => aux_rx, time => time_count(47 downto 0), sample_valid => aux_valid, sample_ch => aux_ch,
                sample_value => aux_value, sample_time => aux_time, errors => errors);
    process (clk)
    begin
      if rising_edge(clk) then
        if rst_g(2) = '1' then
          samples <= (others => '0');
        elsif aux_valid = '1' then
          if aux_ch = 0 then
            target_mv <= std_logic_vector(aux_value);
          end if;
          samples <= samples + 1;
        end if;
      end if;
    end process;
    aux_csr_i : entity work.csr_aux
      port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_AUX), bus_we => c_we,
                bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_AUX),
                bus_err => s_err(CSR_WIN_AUX), bus_rdata => s_rdata(CSR_WIN_AUX),
                target_mv => target_mv,
                samples => std_logic_vector(samples), errors => std_logic_vector(errors));
  end generate;
  no_aux_g : if not AUX generate
    aux_valid <= '0';
    aux_ch    <= 0;
    aux_value <= (others => '0');
    aux_time  <= (others => '0');
    aux_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_AUX), bus_ack => s_ack(CSR_WIN_AUX),
                bus_err => s_err(CSR_WIN_AUX), bus_rdata => s_rdata(CSR_WIN_AUX));
  end generate;

  -- ボードの間の時刻の同期: 親として sync_out に出し、子として sync_in の組をイベント 10 に
  sync_g : if SYNC generate
    signal enable, master : std_logic;
    signal period : std_logic_vector(31 downto 0);
    signal local, parent : unsigned(63 downto 0);
    signal crc_errors, pairs : unsigned(15 downto 0);
  begin
    link_i : entity work.sync_link
      port map (clk => clk, rst => rst_g(2), time_count => time_count, enable => enable, master => master,
                period => unsigned(period), sync_in => sync_in, sync_out => sync_out, pair_valid => sync_valid,
                pair_local => local, pair_parent => parent, crc_errors => crc_errors);
    sync_local  <= std_logic_vector(local(47 downto 0));
    sync_parent <= std_logic_vector(parent);
    process (clk)
    begin
      if rising_edge(clk) then
        if rst_g(2) = '1' or enable = '0' then
          pairs <= (others => '0');
        elsif sync_valid = '1' then
          pairs <= pairs + 1;
        end if;
      end if;
    end process;
    sync_csr_i : entity work.csr_sync
      port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_SYNC), bus_we => c_we,
                bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_SYNC),
                bus_err => s_err(CSR_WIN_SYNC), bus_rdata => s_rdata(CSR_WIN_SYNC),
                control_enable => enable, control_master => master, period => period,
                status_pairs => std_logic_vector(pairs), status_crc_errors => std_logic_vector(crc_errors));
  end generate;
  no_sync_g : if not SYNC generate
    sync_out    <= '0';
    sync_valid  <= '0';
    sync_local  <= (others => '0');
    sync_parent <= (others => '0');
    sync_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_SYNC), bus_ack => s_ack(CSR_WIN_SYNC),
                bus_err => s_err(CSR_WIN_SYNC), bus_rdata => s_rdata(CSR_WIN_SYNC));
  end generate;

  -- 対象の FPGA の観測: 対象の JTAG の観測の回路をスキャンし続け、記録をイベント 8、時刻合わせをイベント 9 に
  obs_g : if OBS_PORT generate
    signal enable, free_pins, push : std_logic;
    signal half_period, sync_period : std_logic_vector(15 downto 0);
    signal ir_len, ir_before, ir_after : std_logic_vector(5 downto 0);
    signal dr_before, dr_after : std_logic_vector(2 downto 0);
    signal user_ir, read_value, id : std_logic_vector(31 downto 0);
    signal cmd : std_logic_vector(15 downto 0);
    signal words : std_logic_vector(8 downto 0);
    signal running, id_ok, stuck, id_timeout, pending, overflow_seen, dropped, target_running, driving : std_logic;
    signal id_scans : std_logic_vector(7 downto 0);
    signal scans : unsigned(31 downto 0);
    signal m_mask, m_value, m_change : std_logic_vector(15 downto 0);
    signal m_on : std_logic;
    signal m_count : unsigned(31 downto 0);
  begin
    obs_i : entity work.obs_jtag_engine
      port map (clk => clk, rst => rst_g(2), time_count => time_count(47 downto 0), enable => enable,
                free_pins => free_pins, half_period => unsigned(half_period),
                sample_delay => 0, ir_len => unsigned(ir_len),   -- ponytail: 観測の口の TDO の遅れは High 相の終わり。遅れて届く対象が出たら CSR に
                ir_before => unsigned(ir_before), ir_after => unsigned(ir_after), dr_before => unsigned(dr_before),
                dr_after => unsigned(dr_after), user_ir => user_ir, words => unsigned(words),
                sync_period => unsigned(sync_period), id_scans => unsigned(id_scans), cmd_push => push, cmd_data => cmd,
                match_mask => m_mask, match_value => m_value, match_change => m_change, match_on => m_on,
                match => obs_match, match_count => m_count,
                running => running, target_running => target_running, driving => driving, id_ok => id_ok,
                stuck => stuck, id_timeout => id_timeout, cmd_pending => pending,
                overflow_seen => overflow_seen, cmd_dropped => dropped, scans => scans, read_value => read_value,
                id => id, ev_valid => obs_valid, ev_sync => obs_sync, ev_data => obs_data, ev_addr => obs_addr,
                ev_time => obs_time, tck => obs_tck, tms => obs_tms, tdi => obs_tdi, tdo => obs_tdo, oe => obs_oe);
    obs_csr_i : entity work.csr_obs
      port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_OBS), bus_we => c_we,
                bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_OBS),
                bus_err => s_err(CSR_WIN_OBS), bus_rdata => s_rdata(CSR_WIN_OBS),
                control_enable => enable, control_release => free_pins, half_period => half_period,
                chain_ir_len => ir_len, chain_ir_before => ir_before, chain_ir_after => ir_after,
                chain_dr_before => dr_before, chain_dr_after => dr_after, user_ir => user_ir, words => words,
                sync_period => sync_period, id_scans => id_scans, cmd => cmd, cmd_push => push,
                status_running => running, status_id_ok => id_ok, status_stuck => stuck, status_id_timeout => id_timeout,
                status_cmd_pending => pending, status_overflow_seen => overflow_seen, status_cmd_dropped => dropped,
                status_target_running => target_running, status_driving => driving,
                scans => std_logic_vector(scans), read_value => read_value, id => id,
                match_mask => m_mask, match_value => m_value, match_change_change => m_change,
                match_change_enable => m_on, match_count => std_logic_vector(m_count));
  end generate;
  no_obs_g : if not OBS_PORT generate
    obs_valid <= '0';
    obs_sync  <= '0';
    obs_data  <= (others => '0');
    obs_addr  <= (others => '0');
    obs_time  <= (others => '0');
    obs_tck   <= '1';
    obs_tms   <= '1';
    obs_tdi   <= '1';
    obs_oe    <= '0';
    obs_match <= '0';
    obs_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_OBS), bus_ack => s_ack(CSR_WIN_OBS),
                bus_err => s_err(CSR_WIN_OBS), bus_rdata => s_rdata(CSR_WIN_OBS));
  end generate;

  events_csr_i : entity work.csr_events
    port map (clk => clk, rst => rst_g(0), bus_req => s_req(CSR_WIN_EVENTS), bus_we => c_we,
              bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_EVENTS),
              bus_err => s_err(CSR_WIN_EVENTS), bus_rdata => s_rdata(CSR_WIN_EVENTS),
              control_enable => events_enable, written => events_written(31 downto 0), lost => events_lost,
              capacity => events_capacity, written_hi => events_written_hi);

  -- written_hi は written（下位）を読んだ周期の上位を保持する。written を読む直後（同じ連続読み出しの中）に written_hi を読めば、
  -- 下位と上位は同じ時点の値で、桁上がりをまたいで化けない（二つのワードを別の時点に読むと、2**32 の境で下位だけ 0 に戻る）。
  -- 保持は 1 つの連続読み出しのあいだ他の読み手に割り込まれない前提（CSR の読みは 1 要求ずつ順に処理される）
  process (clk)
  begin
    if rising_edge(clk) then
      if s_req(CSR_WIN_EVENTS) = '1' and s_ack(CSR_WIN_EVENTS) = '0' and bus_we = '0'
         and to_integer(bus_addr(3 downto 0)) = 1 then
        events_written_hi <= events_written(63 downto 32);
      end if;
      if rst_g(0) = '1' then
        events_written_hi <= (others => '0');
      end if;
    end if;
  end process;

  -- PHY の設定（イベント 16 の [119:48]。並びは event_collector.vhd の先頭）。設定は PHY が命令を実行していない間に変わる
  -- SWD の取り込みの遅れの上位ビット（0〜7 に広げた分）は互換のため [57] に置く（下位 2 ビットは [28:27] のまま）
  dap_cfg_word <= (71 downto 58 => '0') & swd_sample_delay(2) & std_logic_vector(to_unsigned(jtag_ir_len - 1, 5))
                  & std_logic_vector(to_unsigned(jtag_ir_after, 6)) & std_logic_vector(to_unsigned(jtag_ir_before, 6))
                  & std_logic_vector(to_unsigned(jtag_dr_after, 3)) & std_logic_vector(to_unsigned(jtag_dr_before, 3))
                  & swd_enable & jtag & jtag_sample_delay & swd_sample_delay(1 downto 0) & swd_data_phase
                  & std_logic_vector(to_unsigned(swd_turnaround - 1, 2)) & std_logic_vector(to_unsigned(swd_idle_cycles, 8))
                  & std_logic_vector(swd_half_period);

  -- イベント記録の集約（形式は event_collector.vhd の先頭と csr_events の仕様）
  collector_i : entity work.event_collector
    generic map (BYTE_PACK => EVENT_BYTE_PACK, CONFIG_EVENTS => true)
    port map (clk => clk, rst => rst_g(4), time_count => time_count(47 downto 0), events_enable => events_enable,
              ev_valid => ev_valid, host_kind => host_kind, ev_parity => ev_parity, ev_request => ev_request,
              ev_ack => ev_ack, ev_data => ev_data, ev_addr => ev_addr, ev_time => std_logic_vector(ev_time(47 downto 0)),
              inj_written => inj_written, inj_transfers => inj_transfers, inj_value => inj_w_value, inj_addr => inj_w_addr,
              inj_time => std_logic_vector(inj_w_time(47 downto 0)),
              smp_valid => smp_rec_valid, smp_kind => smp_rec_kind, smp_word => smp_rec_word,
              uart_rx_fire => uart_out_valid and uart_out_ready, uart_rx_data => uart_out_data,
              uart_rx_time => uart_out_time,
              uart_tx_fire => uart_s_valid and uart_s_ready, uart_tx_data => uart_s,
              swo_fire => swo_out_valid, swo_data => swo_out_data, swo_time => swo_out_time,
              aux_valid => aux_valid, aux_ch => aux_ch, aux_value => aux_value, aux_src_time => aux_time,
              obs_valid => obs_valid, obs_sync => obs_sync, obs_data => obs_data, obs_addr => obs_addr,
              obs_time => obs_time,
              sync_valid => sync_valid, sync_local => sync_local, sync_parent => sync_parent,
              cfg_word => dap_cfg_word,
              event_valid => event_valid, event_data => event_data, byte_lost => byte_lost);
  memory : if MEM_ADDR_BITS > 0 generate
    signal first : unsigned(MEM_ADDR_BITS - 1 downto 0);
    signal errors, wc, rc : unsigned(31 downto 0);
    signal t_valid, t_write : std_logic;
    signal t_addr : unsigned(MEM_ADDR_BITS - 1 downto 0);
    signal t_data : std_logic_vector(127 downto 0);
  begin
    -- 状態（初期化・較正の完了など）は常に。試験回路は MEM_TEST のときだけ（mem_port の使い手は試験回路か
    -- 深いバッファのどちらか一方）
    test_i : entity work.mem_test_sys
      generic map (ADDR_BITS => MEM_ADDR_BITS, DATA_BITS => 128, TESTER => MEM_TEST)
      port map (clk => clk, rst => rst_g(4), start => mem_test_start,
                base => unsigned(mem_base(MEM_ADDR_BITS - 1 downto 0)),
                count => unsigned(mem_count(MEM_ADDR_BITS downto 0)), seed => mem_seed,
                ready => mem_ready_s, in_reset => mem_in_reset, clock_tick => mem_tick,
                busy => mem_busy, done => mem_done, errors => errors,
                first_error => first, write_cycles => wc, read_cycles => rc,
                mem_clk => mem_clk, mem_rst => mem_rst, mem_ready => mem_ready,
                cmd_valid => t_valid, cmd_ready => mem_cmd_ready, cmd_write => t_write,
                cmd_addr => t_addr, cmd_data => t_data, rd_valid => mem_rd_valid, rd_data => mem_rd_data);
    mem_errors <= std_logic_vector(errors);
    mem_first  <= std_logic_vector(resize(first, 32));
    mem_wc     <= std_logic_vector(wc);
    mem_rc     <= std_logic_vector(rc);

    tester_port : if MEM_TEST generate
      mem_cmd_valid <= t_valid;
      mem_cmd_write <= t_write;
      mem_cmd_addr  <= t_addr;
      mem_cmd_data  <= t_data;
      deep_capturing <= '0'; deep_triggered <= '0'; deep_done <= '0'; deep_overrun <= '0'; deep_reading <= '0';
      deep_time <= (others => '0'); deep_beat <= (others => '0'); deep_slot <= (others => '0');
      deep_written <= (others => '0'); deep_ring_beats <= (others => '0');
      events_written <= (others => '0'); events_lost <= (others => '0'); events_capacity <= (others => '0');
      deep_stream_beats <= (others => '0');
      deep_la_written <= (others => '0'); deep_unread <= (others => '0'); deep_peak <= (others => '0');
      deep_start_time <= (others => '0'); deep_start_valid <= '0'; deep_latency <= (others => '0');
      deep_gap_open <= '0'; deep_gap_count <= (others => '0'); deep_gap_last <= (others => '0');
      ps_bus_rdata <= (others => '0');
      stream_out_valid <= '0';
      stream_out_data  <= (others => '0');
    end generate;

    deep : if not MEM_TEST generate
      signal beat : unsigned(MEM_ADDR_BITS - 1 downto 0);
      signal slot : unsigned(2 downto 0);
      signal written, la_beats, ev_beats : unsigned(MEM_ADDR_BITS downto 0);
      signal ev_written : unsigned(63 downto 0);
      signal ev_lost, stream_beats_u : unsigned(31 downto 0);
      -- la_deep_buffer ⇄ la_ps_port（PS が DDR を直接読む流れの位置の約束）
      signal ps_epoch_u, ps_pub_epoch_u, ps_ret_epoch_u : unsigned(15 downto 0);
      signal ps_active_u, ps_writing_u, ps_overrun_u, ps_rd_region_u : std_logic;
      signal ps_la_written_u, ps_la_consumed_u, ps_ev_consumed_u, ps_out_beats_u : unsigned(31 downto 0);
      signal ps_la_wptr_u : unsigned(MEM_ADDR_BITS - 1 downto 0);
      signal ps_ev_wpos_u, ps_ev_start_u : unsigned(MEM_ADDR_BITS - 5 downto 0);
      signal ps_rd_epoch_u, ps_rd_ack_u : unsigned(7 downto 0);
      signal ps_rd_first_u : unsigned(MEM_ADDR_BITS - 1 downto 0);
      signal ps_rd_count_u : unsigned(MEM_ADDR_BITS downto 0);
      signal stat_written_u, stat_unread_u, stat_peak_u : unsigned(31 downto 0);
      signal gap_count_u, gap_last_u : unsigned(31 downto 0);
    begin
    deep_i : entity work.la_deep_buffer
      generic map (WIDTH => LA_WIDTH, ADDR_BITS => MEM_ADDR_BITS, STREAM_EVENTS => LA_STREAM_EVENTS,
                   STREAM_GAPS => LA_STREAM_GAPS,
                   COUNT_START_BELOW_4G => EVENT_COUNT_START_BELOW_4G, LANES => DEEP_LANES,
                   IN_AGE => LA_LANE_IN_AGE * boolean'pos(USE_DEEP_LANES), EXT_DELAY => LA_EXT_DELAY,
                   SAME_CLOCK => MEM_SAME_CLOCK)
      port map (clk => clk, rst => rst_g(4), probes => la_probes, ext_trigger => la_ext, time => time_count,
                lane_data => la_lane_data(DEEP_LANES * LA_WIDTH - 1 downto 0), step => unsigned(deep_step),
                first_ch => unsigned(deep_first_ch),
                rate => unsigned(la_rate), pretrigger => unsigned(la_pretrigger),
                trig_mask => la_mask, trig_value => la_value, trig_rise => la_rise,
                trig_fall => la_fall, pw_enable => pw_enable, pw_event => pw_event,
                run => deep_run, rle => deep_rle, pack => deep_pack, stream => deep_stream, gaps => deep_gaps,
                ps_read => deep_ps_read, ps_epoch => ps_epoch_u, ps_ret_epoch => ps_ret_epoch_u,
                ps_la_consumed => ps_la_consumed_u, ps_out_beats => ps_out_beats_u,
                ps_ev_consumed => ps_ev_consumed_u(MEM_ADDR_BITS - 5 downto 0),
                ps_pub_epoch => ps_pub_epoch_u, ps_active => ps_active_u, ps_writing => ps_writing_u,
                ps_overrun => ps_overrun_u, ps_la_written => ps_la_written_u, ps_la_wptr => ps_la_wptr_u,
                ps_ev_wpos => ps_ev_wpos_u, ps_ev_start => ps_ev_start_u, wr_pending => mem_wr_pending,
                post_beats => unsigned(deep_post(MEM_ADDR_BITS - 1 downto 0)),
                capturing => deep_capturing, triggered => deep_triggered, done => deep_done, overrun => deep_overrun,
                trigger_beat => beat, trigger_slot => slot, written => written, trigger_time => deep_time,
                stream_beats => stream_beats_u,
                stat_written => stat_written_u, stat_unread => stat_unread_u, stat_peak => stat_peak_u,
                start_time => deep_start_time, start_valid => deep_start_valid, latency => deep_latency,
                gap_count => gap_count_u, gap_last_beat => gap_last_u, gap_open => deep_gap_open,
                ev_enable => events_enable, ev_valid => event_valid, ev_data => event_data,
                ev_written => ev_written, ev_lost => ev_lost, la_beats => la_beats, ev_beats => ev_beats,
                read => deep_read, read_region => deep_region,
                read_ps => deep_read_ps, ps_rd_ack => ps_rd_ack_u, ps_rd_epoch => ps_rd_epoch_u,
                ps_rd_region => ps_rd_region_u, ps_rd_first => ps_rd_first_u, ps_rd_count => ps_rd_count_u,
                read_first => unsigned(deep_first(MEM_ADDR_BITS - 1 downto 0)),
                read_count => unsigned(deep_count(MEM_ADDR_BITS downto 0)), reading => deep_reading,
                out_valid => stream_out_valid, out_ready => stream_out_ready, out_data => stream_out_data,
                mem_clk => mem_clk, mem_rst => mem_rst, mem_active => open,
                cmd_valid => mem_cmd_valid, cmd_ready => mem_cmd_ready, cmd_write => mem_cmd_write,
                cmd_addr => mem_cmd_addr, cmd_data => mem_cmd_data, rd_valid => mem_rd_valid, rd_data => mem_rd_data);
    deep_beat    <= std_logic_vector(resize(beat, 32));
    deep_slot    <= std_logic_vector(slot);
    deep_written <= std_logic_vector(resize(written, 32));
    deep_ring_beats <= std_logic_vector(resize(la_beats, 32));
    events_written  <= std_logic_vector(ev_written);
    events_lost     <= std_logic_vector(sat_add(ev_lost, byte_lost));   -- 和を上限で止める（折り返して小さく見せない。C9）
    events_capacity <= std_logic_vector(resize(ev_beats, 32));
    deep_stream_beats <= std_logic_vector(stream_beats_u);
    deep_la_written   <= std_logic_vector(stat_written_u);
    deep_unread       <= std_logic_vector(stat_unread_u);
    deep_peak         <= std_logic_vector(stat_peak_u);
    deep_gap_count    <= std_logic_vector(gap_count_u);
    deep_gap_last     <= std_logic_vector(gap_last_u);
    -- PS が DDR を直接読む流れの窓の側のレジスタ（位置の約束を守らせる）
    ps_port_i : entity work.la_ps_port
      port map (clk => clk, rst => rst_g(4), bus_rd => ps_bus_rd, bus_wr => ps_bus_wr, bus_addr => ps_bus_addr,
                bus_wdata => ps_bus_wdata, bus_rdata => ps_bus_rdata, epoch => ps_epoch_u, pub_epoch => ps_pub_epoch_u,
                pub_active => ps_active_u, pub_writing => ps_writing_u, pub_overrun => ps_overrun_u,
                pub_la_written => ps_la_written_u, pub_la_wptr => resize(ps_la_wptr_u, 32),
                pub_ev_wpos => resize(ps_ev_wpos_u, 32), pub_ev_start => resize(ps_ev_start_u, 32),
                rd_epoch => ps_rd_epoch_u, rd_region => ps_rd_region_u, rd_first => resize(ps_rd_first_u, 32),
                rd_count => resize(ps_rd_count_u, 32), ret_epoch => ps_ret_epoch_u, la_consumed => ps_la_consumed_u,
                ev_consumed => ps_ev_consumed_u, out_beats => ps_out_beats_u, rd_ack => ps_rd_ack_u);
    end generate;
  end generate;

  -- 外部メモリが無い: 状態・深いバッファ・イベント記録の値も明示的に 0（CSR から読まれる。駆動されない配線は
  -- 実機で値が定まらない）
  no_memory : if MEM_ADDR_BITS = 0 generate
    mem_ready_s <= '0'; mem_busy <= '0'; mem_done <= '0'; mem_in_reset <= '0'; mem_tick <= '0';
    mem_errors <= (others => '0'); mem_first <= (others => '0'); mem_wc <= (others => '0'); mem_rc <= (others => '0');
    deep_capturing <= '0'; deep_triggered <= '0'; deep_done <= '0'; deep_overrun <= '0';
    deep_time <= (others => '0'); deep_beat <= (others => '0'); deep_slot <= (others => '0');
    deep_written <= (others => '0'); deep_ring_beats <= (others => '0'); deep_stream_beats <= (others => '0');
    deep_la_written <= (others => '0'); deep_unread <= (others => '0'); deep_peak <= (others => '0');
    deep_start_time <= (others => '0'); deep_start_valid <= '0'; deep_latency <= (others => '0');
    deep_gap_open <= '0'; deep_gap_count <= (others => '0'); deep_gap_last <= (others => '0');
    ps_bus_rdata <= (others => '0');
    mem_cmd_valid <= '0';
    mem_cmd_write <= '0';
    mem_cmd_addr  <= (others => '0');
    mem_cmd_data  <= (others => '0');

    no_events : if EVENT_RING_BITS = 0 generate
      deep_reading <= '0';
      events_written <= (others => '0'); events_lost <= (others => '0'); events_capacity <= (others => '0');
      stream_out_valid <= '0';
      stream_out_data  <= (others => '0');
    end generate;

    -- イベント記録を内蔵 RAM のリングに（外部メモリの深いバッファと同じ CSR・読み出し・ストリーム。
    -- docs/superpowers/specs/2026-10-20-bram-event-ring-design.md）
    ring_events : if EVENT_RING_BITS > 0 generate
      signal ev_written : unsigned(63 downto 0);
      signal ev_lost, ev_beats : unsigned(31 downto 0);
    begin
      ring_i : entity work.event_ring
        generic map (DEPTH_BITS => EVENT_RING_BITS, ADDR_BITS => EVENT_RING_BITS,
                     COUNT_START_BELOW_4G => EVENT_COUNT_START_BELOW_4G)
        port map (clk => clk, rst => rst_g(4), ev_enable => events_enable, ev_valid => event_valid, ev_data => event_data,
                  ev_written => ev_written, ev_lost => ev_lost, ev_beats => ev_beats,
                  read => deep_read, read_region => deep_region,
                  read_first => unsigned(deep_first(EVENT_RING_BITS - 1 downto 0)),
                  read_count => unsigned(deep_count(EVENT_RING_BITS downto 0)), reading => deep_reading,
                  out_valid => stream_out_valid, out_ready => stream_out_ready, out_data => stream_out_data);
      events_written  <= std_logic_vector(ev_written);
      events_lost     <= std_logic_vector(sat_add(ev_lost, byte_lost));
      events_capacity <= std_logic_vector(ev_beats);
    end generate;
  end generate;

  xtrig_csr_g : if XTRIG generate
  xtrig_csr_i : entity work.csr_xtrig
    port map (clk => clk, rst => rst_g(3), bus_req => s_req(CSR_WIN_XTRIG), bus_we => c_we,
              bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_XTRIG),
              bus_err => s_err(CSR_WIN_XTRIG), bus_rdata => s_rdata(CSR_WIN_XTRIG),
              route_to_la => route_to_la, route_to_inject => route_to_inject, command_fire => host_fire,
              inject_ap => inj_ap, inject_addr => inj_addr, inject_value => inj_value,
              inject_status_busy => inj_busy, inject_status_result => inj_result,
              inject_status_ack => inj_ack, inject_status_count => std_logic_vector(inj_count),
              inject_time_lo => std_logic_vector(inject_time(31 downto 0)),
              inject_time_hi => std_logic_vector(inject_time(63 downto 32)),
              match_addr => match_addr, match_addr_mask => match_addr_mask, match_data => match_data,
              match_data_mask => match_data_mask, match_mode_on_write => match_on_write,
              match_mode_on_read => match_on_read, match_count => std_logic_vector(match_count),
              match_time_lo => std_logic_vector(match_time(31 downto 0)),
              match_time_hi => std_logic_vector(match_time(63 downto 32)));
  end generate;

  -- クロストリガの動作「出力の端子」: 源（sources）と、ロジアナの外部ピン 0〜7 の並び → 出力 k
  xout_g : if XOUT_COUNT > 0 generate
    constant N : positive := XOUT_COUNT;
    type cfg8_t is array (0 to 1) of std_logic_vector(7 downto 0);
    type cfg6_t is array (0 to 1) of std_logic_vector(5 downto 0);
    type cfg3_t is array (0 to 1) of std_logic_vector(2 downto 0);
    type cfg2_t is array (0 to 1) of std_logic_vector(1 downto 0);
    signal w_mask, w_value, c_length : cfg8_t;
    signal w_sources : cfg6_t;
    signal c_unit : cfg3_t;
    signal c_mode, c_pin : cfg2_t;
    signal w_any, c_enable, c_level, c_claim : std_logic_vector(1 downto 0);
    signal f_mask, f_value, f_length : std_logic_vector(8 * N - 1 downto 0);
    signal f_src : std_logic_vector(6 * N - 1 downto 0);
    signal f_unit : std_logic_vector(3 * N - 1 downto 0);
    signal f_mode : std_logic_vector(2 * N - 1 downto 0);
    signal out_o, out_oe : std_logic_vector(N - 1 downto 0);
    signal pins8 : std_logic_vector(7 downto 0);
    signal active : std_logic_vector(1 downto 0);
  begin
    xout_csr_i : entity work.csr_xtrig_out
      port map (clk => clk, rst => rst_g(3), bus_req => s_req(CSR_WIN_XTRIG_OUT), bus_we => c_we,
                bus_addr => c_addr, bus_wdata => c_wdata, bus_ack => s_ack(CSR_WIN_XTRIG_OUT),
                bus_err => s_err(CSR_WIN_XTRIG_OUT), bus_rdata => s_rdata(CSR_WIN_XTRIG_OUT),
                status_active => active,
                when0_mask => w_mask(0), when0_value => w_value(0), when0_sources => w_sources(0), when0_any => w_any(0),
                ctrl0_enable => c_enable(0), ctrl0_mode => c_mode(0), ctrl0_level => c_level(0),
                ctrl0_claim => c_claim(0), ctrl0_length => c_length(0), ctrl0_unit => c_unit(0), ctrl0_pin => c_pin(0),
                when1_mask => w_mask(1), when1_value => w_value(1), when1_sources => w_sources(1), when1_any => w_any(1),
                ctrl1_enable => c_enable(1), ctrl1_mode => c_mode(1), ctrl1_level => c_level(1),
                ctrl1_claim => c_claim(1), ctrl1_length => c_length(1), ctrl1_unit => c_unit(1), ctrl1_pin => c_pin(1));

    flat : for k in 0 to N - 1 generate
      f_mask(8 * k + 7 downto 8 * k)   <= w_mask(k);
      f_value(8 * k + 7 downto 8 * k)  <= w_value(k);
      f_length(8 * k + 7 downto 8 * k) <= c_length(k);
      f_src(6 * k + 5 downto 6 * k)    <= w_sources(k);
      f_unit(3 * k + 2 downto 3 * k)   <= c_unit(k);
      f_mode(2 * k + 1 downto 2 * k)   <= c_mode(k);
    end generate;

    -- 端子の並びはロジアナの外部ピン 0〜7（ピンが 8 本より少なければ残りは 0）
    process (all)
    begin
      pins8 <= (others => '0');
      pins8(minimum(LA_PIN_COUNT, 8) - 1 downto 0) <= la_pins(minimum(LA_PIN_COUNT, 8) - 1 downto 0);
    end process;

    xout_i : entity work.xtrig_out
      generic map (COUNT => N, WIDTH => 8)
      port map (clk => clk, rst => rst_g(3), pins => pins8, sources => sources, time => time_count(31 downto 0),
                enable => c_enable(N - 1 downto 0), mode => f_mode, level => c_level(N - 1 downto 0),
                any => w_any(N - 1 downto 0), mask => f_mask, value => f_value, src => f_src,
                length => f_length, unit => f_unit, o => out_o, oe => out_oe);

    process (all)
    begin
      xout_o     <= (others => '0');
      xout_oe    <= (others => '0');
      xout_claim <= (others => '0');
      xout_pin   <= (others => '0');
      active     <= (others => '0');
      xout_o(N - 1 downto 0)     <= out_o;
      xout_oe(N - 1 downto 0)    <= out_oe;
      xout_claim(N - 1 downto 0) <= c_claim(N - 1 downto 0);
      for k in 0 to N - 1 loop
        xout_pin(2 * k + 1 downto 2 * k) <= c_pin(k);
      end loop;
      active(N - 1 downto 0)     <= out_oe;
    end process;
  end generate;

  no_xout_g : if XOUT_COUNT = 0 generate
    xout_o     <= (others => '0');
    xout_oe    <= (others => '0');
    xout_claim <= (others => '0');
    xout_pin   <= (others => '0');
    xout_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_XTRIG_OUT), bus_ack => s_ack(CSR_WIN_XTRIG_OUT),
                bus_err => s_err(CSR_WIN_XTRIG_OUT), bus_rdata => s_rdata(CSR_WIN_XTRIG_OUT));
  end generate;

  no_xtrig_csr : if not XTRIG generate
    inj_ap           <= (others => '0');
    inj_addr         <= (others => '0');
    inj_value        <= (others => '0');
    route_to_la      <= (others => '0');
    route_to_inject  <= (others => '0');
    host_fire        <= '0';
    match_addr       <= (others => '0');
    match_addr_mask  <= (others => '0');
    match_data       <= (others => '0');
    match_data_mask  <= (others => '0');
    match_on_write   <= '0';
    match_on_read    <= '0';
    xtrig_absent_i : entity work.csr_absent
      port map (clk => clk, bus_req => s_req(CSR_WIN_XTRIG), bus_ack => s_ack(CSR_WIN_XTRIG),
                bus_err => s_err(CSR_WIN_XTRIG), bus_rdata => s_rdata(CSR_WIN_XTRIG));
  end generate;
end architecture;
