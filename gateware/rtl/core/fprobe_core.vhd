-- fprobe のコア（ボード非依存、sys ドメイン）。CONTROL チャンネルのストリームを受け、CSR 空間を提供する。
-- 仕様: docs/superpowers/specs/2026-10-15-m3-control-plane-design.md §2
--
--   CONTROL 要求 → control_plane → csr_decoder ┬ csr_system（識別・疎通確認）
--                  ← 応答                      ├ csr_timebase ⇄ timebase（time_lo を読むと上位を保持）
--                                              ├ csr_gpio（拡張ヘッダの汎用入出力）
--                                              ├ caps_rom（ケイパビリティ）
--                                              └ 部分の窓（下）
-- 機能は部分の entity に分け、この entity は CSR のバス・時刻・共有の割り算器と、部分の間の配線だけを持つ:
--   core_debug   DAP の処理部 → swd_injector → dap_sampler → debug_port（swd_phy / jtag_phy）、dap_monitor（番地一致・
--                ホストの転送のイベント）、サンプラの値の一致。窓 inject・debug・sampler・sampler_match
--   core_xtrig   クロストリガ: 源（ロジアナのトリガ・番地一致・ホストの指令・多段トリガ・観測とサンプラの値の一致）→ 動作
--                （ロジアナのトリガ・割り込み書き込み = 既定はターゲット停止・出力の端子 xout_*）。窓 xtrig・trigseq・xtrig_out
--                （端子への割り当てと、機能との切り替えはボードのトップ。docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md）
--   core_periph  SWO の受信・UART ブリッジ（TX はホストが最初の 1 バイトを送るまで駆動しない。B29）・補助マイコン・
--                ボードの間の時刻の同期・対象の FPGA の観測。窓 uart・aux・sync・obs
--   core_la      ロジアナの入力の並び（ch: ピン、内部の信号 2 本、専用の入力。ケイパビリティ LA_LAYOUT と一致）・前段
--                （グリッチフィルタ・パルス幅のトリガ。U21）・内蔵バッファ。窓 la・la_cap・la_testgen・la_mem・la_lanes_mem
--   core_storage イベント記録（event_collector → 深いバッファか内蔵 RAM のリング）と外部メモリ（メモリ試験・深いバッファ・
--                PS の直読み）。窓 mem・la_deep・events
-- 部分の間で受け渡す組の型と構成の式は core_pkg。
-- ボードのトップはクロック・リセット・ホストリンク（USB 等）とクロック乗せ換えの FIFO を用意してつなぐ。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;
use work.csr_map_pkg.all;
use work.core_pkg.all;

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
    LA_RLE_TOKENS : boolean := false;      -- 深いバッファの垂れ流しの RLE にトークン形式がある（FEATURES の LA_RLE_TOKENS と一致させる。1 周期 1 サンプルの構成だけ）
    LA_RLE_REPEAT : boolean := false;      -- そのトークン形式に REPEAT（周期的な信号の繰り返しを 1 トークンにまとめる）がある（TLV LA_RLE_TOKEN_INFO の REPEAT と一致させる。LA_RLE_TOKENS が要る）
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
  -- 構成で決まるロジアナの形（core_la と core_storage が同じ式を使う。core_pkg）
  constant USE_DEEP_LANES : boolean := la_use_deep_lanes(LA_LANES, LA_INTERNAL, MEM_ADDR_BITS, MEM_TEST);
  constant EXT_TRIGGER_DELAY : natural := la_ext_delay(LA_FILTER and not la_use_cap(CAP_RATIO, LA_LANES, LA_INTERNAL));

  -- 部分の並び（CSR の窓の応答をまとめる）
  constant PART_DEBUG   : natural := 0;
  constant PART_XTRIG   : natural := 1;
  constant PART_PERIPH  : natural := 2;
  constant PART_LA      : natural := 3;
  constant PART_STORAGE : natural := 4;
  constant PART_TOP     : natural := 5;              -- この entity の中の窓（system・timebase・gpio・caps）
  type part_rsp_t is array (0 to 5) of csr_rsp_array_t(CSR_WINDOWS'range);

  -- 塊ごとのリセット（core_pkg の RST_*）
  signal rst_g : core_resets_t;
  signal bus_req, bus_we, bus_ack, bus_err : std_logic;
  signal bus_addr  : csr_addr_t;
  signal bus_wdata, bus_rdata : csr_word_t;
  -- CSR の窓へ配る番地・書き込み値・書き込みの印（csr_decoder が要求の最初の周期にレジスタに取った写し。2026-10-29）
  signal c_cmd : csr_cmd_t;
  signal s_req : std_logic_vector(CSR_WINDOWS'range);
  signal s_rsp : csr_rsp_array_t(CSR_WINDOWS'range);
  signal p_rsp : part_rsp_t;

  signal time_count : unsigned(63 downto 0);
  signal time_lo    : std_logic_vector(31 downto 0);
  signal time_read  : std_logic;
  signal time_hi    : std_logic_vector(31 downto 0);
  signal level_s : std_logic_vector(7 downto 0);          -- gpio_level（端子の値）を clk に同期化
  -- 入力のレジスタスライス（ホストリンク側の FIFO と処理部の組み合わせ回路を切り離す）
  signal ctrl_s_valid, ctrl_s_ready : std_logic;
  signal ctrl_s : std_logic_vector(8 downto 0);
  -- 共有の割り算器（DAP の SWJ_Clock・SWO のボーレートと、UART のボーレート）
  signal dap_div_req, dap_div_done, uart_div_req, uart_div_done : std_logic;
  signal dap_div_num, dap_div_den, uart_div_num, uart_div_den, div_quot : unsigned(31 downto 0);

  -- 部分の間
  signal swo_run, swo_error, swo_overrun, swo_manchester : std_logic;
  signal swo_divisor : unsigned(15 downto 0);
  signal dap_busy, swdio_oe_i : std_logic;
  signal inj_trigger, inj_request, match, smp_match, obs_match : std_logic;
  signal match_time : unsigned(63 downto 0);
  signal inject_cfg : inject_cfg_t;
  signal match_cfg : match_cfg_t;
  signal inject_status : inject_status_t;
  signal la_probes : std_logic_vector(LA_WIDTH - 1 downto 0);
  signal la_lane_data : std_logic_vector(LA_LANES * LA_WIDTH - 1 downto 0);
  signal la_trig_cfg : la_trig_cfg_t;
  signal pw_enable, pw_event, la_triggered, la_ext, deep_triggered : std_logic;
  signal deep_latency : unsigned(7 downto 0);
  -- イベント記録の源
  signal xfer_event : xfer_event_t;
  signal inject_event : inject_event_t;
  signal sampler_event : sampler_event_t;
  signal uart_rx_event, uart_tx_event, swo_event : byte_event_t;
  signal aux_event : aux_event_t;
  signal obs_event : obs_event_t;
  signal sync_event : sync_event_t;
  signal dap_cfg_word : std_logic_vector(71 downto 0);
begin
  split_reset_g : if RESET_SPLIT generate
    reset_fanout_i : entity work.reset_fanout
      generic map (COUNT => 5)
      port map (clk => clk, rst => rst, rst_out => rst_g);
  end generate;
  single_reset_g : if not RESET_SPLIT generate
    rst_g <= (others => rst);
  end generate;

  ---------------------------------------------------------------------------------------------
  -- CONTROL チャンネル → CSR バス → 窓
  ---------------------------------------------------------------------------------------------
  ctrl_slice_i : entity work.stream_slice
    generic map (WIDTH => 9)
    port map (clk => clk, rst => rst_g(RST_BUS), in_valid => ctrl_in_valid, in_ready => ctrl_in_ready,
              in_data => ctrl_in_last & ctrl_in_data, out_valid => ctrl_s_valid, out_ready => ctrl_s_ready,
              out_data => ctrl_s);

  control_i : entity work.control_plane
    port map (clk => clk, rst => rst_g(RST_BUS),
              in_valid => ctrl_s_valid, in_ready => ctrl_s_ready, in_data => ctrl_s(7 downto 0),
              in_last => ctrl_s(8), out_valid => ctrl_out_valid, out_ready => ctrl_out_ready,
              out_data => ctrl_out_data, out_last => ctrl_out_last,
              bus_req => bus_req, bus_we => bus_we, bus_addr => bus_addr, bus_wdata => bus_wdata,
              bus_ack => bus_ack, bus_err => bus_err, bus_rdata => bus_rdata);

  decoder_i : entity work.csr_decoder
    generic map (WINDOWS => CSR_WINDOWS)
    port map (clk => clk, rst => rst_g(RST_BUS), bus_req => bus_req, bus_we => bus_we, bus_addr => bus_addr, bus_wdata => bus_wdata,
              bus_ack => bus_ack, bus_err => bus_err, bus_rdata => bus_rdata,
              s_cmd => c_cmd, s_req => s_req, s_rsp => s_rsp);

  -- 窓の応答: 各部分は自分の窓だけを駆動し、ほかの窓は 0（csr_bus_pkg の約束どおり応答の周期以外も 0）。論理和でまとめる
  process (all)
    variable rsp : csr_rsp_array_t(CSR_WINDOWS'range);
  begin
    rsp := (others => CSR_RSP_IDLE);
    for p in part_rsp_t'range loop
      for w in CSR_WINDOWS'range loop
        rsp(w).ack   := rsp(w).ack or p_rsp(p)(w).ack;
        rsp(w).err   := rsp(w).err or p_rsp(p)(w).err;
        rsp(w).rdata := rsp(w).rdata or p_rsp(p)(w).rdata;
      end loop;
    end loop;
    s_rsp <= rsp;
  end process;

  -- この entity の窓: system（識別・疎通確認）・timebase（time_lo を読むと上位を保持）・gpio（拡張ヘッダの汎用入出力）・caps
  top_idle_g : for i in CSR_WINDOWS'range generate
    other_g : if i /= CSR_WIN_SYSTEM and i /= CSR_WIN_TIMEBASE and i /= CSR_WIN_GPIO and i /= CSR_WIN_CAPS generate
      p_rsp(PART_TOP)(i) <= CSR_RSP_IDLE;
    end generate;
  end generate;

  system_i : entity work.csr_system
    port map (clk => clk, rst => rst_g(RST_BUS), bus_req => s_req(CSR_WIN_SYSTEM), bus_cmd => c_cmd,
              bus_rsp => p_rsp(PART_TOP)(CSR_WIN_SYSTEM), scratch => open);

  timebase_i : entity work.timebase
    port map (clk => clk, rst => rst_g(RST_BUS), count => time_count, hold => time_read, held_hi => time_hi);

  time_lo <= std_logic_vector(time_count(31 downto 0));

  timebase_csr_i : entity work.csr_timebase
    port map (clk => clk, rst => rst_g(RST_BUS), bus_req => s_req(CSR_WIN_TIMEBASE), bus_cmd => c_cmd,
              bus_rsp => p_rsp(PART_TOP)(CSR_WIN_TIMEBASE), time_lo => time_lo, time_lo_read => time_read, time_hi => time_hi);

  gpio_i : entity work.csr_gpio
    port map (clk => clk, rst => rst_g(RST_BUS), bus_req => s_req(CSR_WIN_GPIO), bus_cmd => c_cmd,
              bus_rsp => p_rsp(PART_TOP)(CSR_WIN_GPIO), drive => gpio_drive, enable => gpio_enable, level => level_s, io => level_s);

  level_sync_g : for i in gpio_level'range generate
    level_sync_i : entity work.sync_bit
      port map (clk => clk, d => gpio_level(i), q => level_s(i));
  end generate;

  caps_i : entity work.caps_rom
    generic map (CONTENT => CAPS, WINDOW_SIZE => CSR_WINDOWS(CSR_WIN_CAPS).size)
    port map (clk => clk, rst => rst_g(RST_BUS), bus_req => s_req(CSR_WIN_CAPS), bus_addr => c_cmd.addr,
              bus_ack => p_rsp(PART_TOP)(CSR_WIN_CAPS).ack, bus_rdata => p_rsp(PART_TOP)(CSR_WIN_CAPS).rdata);
  p_rsp(PART_TOP)(CSR_WIN_CAPS).err <= '0';                    -- ROM は範囲外も 0 で答える（エラーにしない）

  -- 割り算器は 1 つを DAP（優先）と UART ブリッジで共有する（どちらもまれ: SWJ_Clock・SWO・ボーレートの変更）
  divider_i : entity work.udiv_shared
    generic map (WIDTH => 32)
    port map (clk => clk, rst => rst_g(RST_DEBUG),
              p0_req => dap_div_req, p0_num => dap_div_num, p0_den => dap_div_den, p0_done => dap_div_done,
              p1_req => uart_div_req, p1_num => uart_div_num, p1_den => uart_div_den, p1_done => uart_div_done,
              quotient => div_quot);

  ---------------------------------------------------------------------------------------------
  -- 部分
  ---------------------------------------------------------------------------------------------
  debug_i : entity work.core_debug
    generic map (CLK_HZ => CLK_HZ, HAS_JTAG => HAS_JTAG, HAS_SWO_MANCHESTER => HAS_SWO_MANCHESTER, XTRIG => XTRIG,
                 INJECTOR => INJECTOR, SAMPLER => SAMPLER, SAMPLER_MATCH => SAMPLER_MATCH,
                 SAMPLER_CHECK_MAX => SAMPLER_CHECK_MAX)
    port map (clk => clk, rst => rst_g, time_count => time_count,
              csr_cmd => c_cmd, csr_req => s_req, csr_rsp => p_rsp(PART_DEBUG),
              packet_size => packet_size,
              dap_in_valid => dap_in_valid, dap_in_ready => dap_in_ready, dap_in_data => dap_in_data, dap_in_last => dap_in_last,
              dap_out_valid => dap_out_valid, dap_out_ready => dap_out_ready, dap_out_data => dap_out_data,
              dap_out_last => dap_out_last,
              swclk_o => swclk_o, swclk_oe => swclk_oe, swdio_o => swdio_o, swdio_oe => swdio_oe_i,
              swdio_o_lead => swdio_o_lead, swdio_i => swdio_i, tdi_o => tdi_o, tdi_oe => tdi_oe, tdo_i => tdo_i,
              nreset_low => nreset_low, nreset_i => nreset_i,
              swo_run => swo_run, swo_divisor => swo_divisor, swo_manchester => swo_manchester,
              swo_error => swo_error, swo_overrun => swo_overrun,
              div_req => dap_div_req, div_num => dap_div_num, div_den => dap_div_den,
              div_done => dap_div_done, div_quot => div_quot,
              inj_trigger => inj_trigger, inj_request => inj_request, inject_cfg => inject_cfg, match_cfg => match_cfg,
              inject_status => inject_status, match => match, match_time => match_time, smp_match => smp_match,
              dap_busy => dap_busy, xfer_event => xfer_event, inject_event => inject_event,
              sampler_event => sampler_event, cfg_word => dap_cfg_word);
  swdio_oe <= swdio_oe_i;

  xtrig_i : entity work.core_xtrig
    generic map (XTRIG => XTRIG, TRIG_SEQ => TRIG_SEQ, XOUT_COUNT => XOUT_COUNT, LA_PIN_COUNT => LA_PIN_COUNT)
    port map (clk => clk, rst => rst_g, time_count => time_count,
              csr_cmd => c_cmd, csr_req => s_req, csr_rsp => p_rsp(PART_XTRIG),
              la_probes => la_probes, la_pins => la_pins, la_triggered => la_triggered, deep_triggered => deep_triggered,
              match => match, match_time => match_time, obs_match => obs_match, smp_match => smp_match,
              la_ext => la_ext, inj_trigger => inj_trigger, inj_request => inj_request,
              inject_cfg => inject_cfg, match_cfg => match_cfg, inject_status => inject_status,
              xout_o => xout_o, xout_oe => xout_oe, xout_claim => xout_claim, xout_pin => xout_pin);

  periph_i : entity work.core_periph
    generic map (CLK_HZ => CLK_HZ, HAS_SWO_MANCHESTER => HAS_SWO_MANCHESTER, UART_FLOW => UART_FLOW, AUX => AUX,
                 SYNC => SYNC, OBS_PORT => OBS_PORT)
    port map (clk => clk, rst => rst_g, time_count => time_count,
              csr_cmd => c_cmd, csr_req => s_req, csr_rsp => p_rsp(PART_PERIPH),
              swo_run => swo_run, swo_divisor => swo_divisor, swo_manchester => swo_manchester,
              swo_error => swo_error, swo_overrun => swo_overrun, swo_i => swo_i,
              swo_out_valid => swo_out_valid, swo_out_ready => swo_out_ready, swo_out_data => swo_out_data,
              swo_flush => swo_flush,
              uart_baud => uart_baud, uart_baud_load => uart_baud_load,
              uart_in_valid => uart_in_valid, uart_in_ready => uart_in_ready, uart_in_data => uart_in_data,
              uart_out_valid => uart_out_valid, uart_out_ready => uart_out_ready, uart_out_data => uart_out_data,
              uart_rx => uart_rx, uart_tx => uart_tx, uart_tx_oe => uart_tx_oe,
              uart_host_rts => uart_host_rts, uart_host_break => uart_host_break, uart_cts_n => uart_cts_n,
              uart_rts_n => uart_rts_n, uart_rts_claim => uart_rts_claim, uart_rts_pin => uart_rts_pin,
              uart_cts_claim => uart_cts_claim, uart_cts_pin => uart_cts_pin, uart_cts => uart_cts,
              div_req => uart_div_req, div_num => uart_div_num, div_den => uart_div_den,
              div_done => uart_div_done, div_quot => div_quot,
              aux_rx => aux_rx, sync_in => sync_in, sync_out => sync_out,
              obs_tck => obs_tck, obs_tms => obs_tms, obs_tdi => obs_tdi, obs_tdo => obs_tdo, obs_oe => obs_oe,
              obs_match => obs_match,
              uart_rx_event => uart_rx_event, uart_tx_event => uart_tx_event, swo_event => swo_event,
              aux_event => aux_event, obs_event => obs_event, sync_event => sync_event);

  la_i : entity work.core_la
    generic map (LA_PIN_COUNT => LA_PIN_COUNT, LA_INPUT_COUNT => LA_INPUT_COUNT, LA_INTERNAL => LA_INTERNAL,
                 LA_FILTER => LA_FILTER, LA_PULSE => LA_PULSE, CAP_RATIO => CAP_RATIO, LA_TESTGEN => LA_TESTGEN,
                 LA_LANES => LA_LANES, LA_LANE_ADDR_BITS => LA_LANE_ADDR_BITS, LA_LANE_IN_AGE => LA_LANE_IN_AGE,
                 LA_FINE_PINS => LA_FINE_PINS, USE_DEEP_LANES => USE_DEEP_LANES)
    port map (clk => clk, cap_clk => cap_clk, rst => rst_g, time_count => time_count,
              csr_cmd => c_cmd, csr_req => s_req, csr_rsp => p_rsp(PART_LA),
              la_pins => la_pins, la_inputs => la_inputs, la_input_lanes => la_input_lanes, la_pin_lanes => la_pin_lanes,
              dap_busy => dap_busy, swdio_oe => swdio_oe_i, la_ext => la_ext, deep_latency => deep_latency,
              la_probes => la_probes, la_lane_data => la_lane_data, trig_cfg => la_trig_cfg,
              pw_enable => pw_enable, pw_event => pw_event, la_triggered => la_triggered,
              testgen_out => testgen_out, testgen_oe => testgen_oe);

  storage_i : entity work.core_storage
    generic map (MEM_ADDR_BITS => MEM_ADDR_BITS, MEM_TEST => MEM_TEST, LA_STREAM_EVENTS => LA_STREAM_EVENTS,
                 LA_STREAM_GAPS => LA_STREAM_GAPS, LA_RLE_TOKENS => LA_RLE_TOKENS, LA_RLE_REPEAT => LA_RLE_REPEAT, EVENT_RING_BITS => EVENT_RING_BITS,
                 EVENT_COUNT_START_BELOW_4G => EVENT_COUNT_START_BELOW_4G, EVENT_BYTE_PACK => EVENT_BYTE_PACK,
                 MEM_SAME_CLOCK => MEM_SAME_CLOCK, LA_LANES => LA_LANES, LA_LANE_IN_AGE => LA_LANE_IN_AGE,
                 USE_DEEP_LANES => USE_DEEP_LANES, EXT_DELAY => EXT_TRIGGER_DELAY)
    port map (clk => clk, rst => rst_g, time_count => time_count,
              csr_cmd => c_cmd, csr_req => s_req, csr_rsp => p_rsp(PART_STORAGE),
              la_probes => la_probes, la_lane_data => la_lane_data, trig_cfg => la_trig_cfg,
              pw_enable => pw_enable, pw_event => pw_event, la_ext => la_ext,
              deep_triggered => deep_triggered, deep_latency => deep_latency,
              xfer_event => xfer_event, inject_event => inject_event, sampler_event => sampler_event,
              uart_rx_event => uart_rx_event, uart_tx_event => uart_tx_event, swo_event => swo_event,
              aux_event => aux_event, obs_event => obs_event, sync_event => sync_event, cfg_word => dap_cfg_word,
              mem_clk => mem_clk, mem_rst => mem_rst, mem_ready => mem_ready,
              mem_cmd_valid => mem_cmd_valid, mem_cmd_ready => mem_cmd_ready, mem_cmd_write => mem_cmd_write,
              mem_cmd_addr => mem_cmd_addr, mem_cmd_data => mem_cmd_data, mem_rd_valid => mem_rd_valid,
              mem_rd_data => mem_rd_data, mem_wr_pending => mem_wr_pending,
              stream_out_valid => stream_out_valid, stream_out_ready => stream_out_ready, stream_out_data => stream_out_data,
              ps_bus_rd => ps_bus_rd, ps_bus_wr => ps_bus_wr, ps_bus_addr => ps_bus_addr, ps_bus_wdata => ps_bus_wdata,
              ps_bus_rdata => ps_bus_rdata);
end architecture;
