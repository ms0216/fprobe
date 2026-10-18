-- CMSIS-DAP v2 のコマンド処理（ハードウェア）。1 パケット = 1 コマンドを読み、応答パケットを返す。
-- 仕様: docs/superpowers/specs/2026-10-15-m4-swd-cmsis-dap-design.md §4、2026-10-16-dap-processor-split-design.md
--
-- 構成（各部品は小さな状態機械）:
--   dap_packet       パケットの枠（引数の読み取り・応答の組み立て・読み捨て・送出）。コマンドを担当ユニットへ
--   dap_cmd_config   設定系（Info / Connect / TransferConfig / SWJ_Clock / JTAG_Configure 等）と設定の保持
--   dap_cmd_swo      SWO のコマンド
--   dap_cmd_sequence SWJ_Pins / SWJ_Sequence / SWD_Sequence / JTAG_Sequence と nRESET
--   dap_cmd_transfer Transfer / TransferBlock / WriteAbort / JTAG_IDCODE（参照実装 DAP.c と同じ意味論）
--   dap_link         PHY への命令（転送の WAIT 再試行と応答の符号化）
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.dap_pkg.all;

entity dap_processor is
  generic (
    CLK_HZ     : positive;                  -- clk の周波数（DAP_Delay・SWJ_Clock・SWO のボーレートの換算）
    SWO_BUFFER : positive := 2048;          -- SWO のバッファ（バイト。DAP_Info 0xFD で申告）
    HAS_JTAG   : boolean := true;           -- JTAG の PHY がある（無ければ DAP_Connect の JTAG は失敗、DAP_Info に出さない）
    HAS_SWO_MANCHESTER : boolean := true;      -- SWO の Manchester 方式の受信回路がある（無ければ Mode 2 を断り、DAP_Info に出さない）
    SHARED_DIVIDER : boolean := false       -- 割り算器を外（div_* ポート。fprobe_core が uart_bridge と共有）に置く
  );
  port (
    clk             : in  std_logic;
    rst             : in  std_logic;
    packet_size     : in  natural range 64 to 512;   -- 現在のパケットサイズ（USB の MPS）
    -- コマンド / 応答（last はパケットの終わり）
    in_valid        : in  std_logic;
    in_ready        : out std_logic;
    in_data         : in  std_logic_vector(7 downto 0);
    in_last         : in  std_logic;
    out_valid       : out std_logic;
    out_ready       : in  std_logic;
    out_data        : out std_logic_vector(7 downto 0);
    out_last        : out std_logic;
    -- swd_phy
    swd_enable      : out std_logic;
    swd_half_period : out unsigned(15 downto 0);
    swd_turnaround  : out natural range 1 to 4;
    swd_data_phase  : out std_logic;
    swd_idle_cycles : out natural range 0 to 255;
    swd_cmd_valid   : out std_logic;
    swd_cmd_ready   : in  std_logic;
    swd_cmd_op      : out std_logic_vector(1 downto 0);
    swd_cmd_count   : out natural range 1 to 64;
    swd_cmd_request : out std_logic_vector(3 downto 0);
    swd_cmd_data    : out std_logic_vector(63 downto 0);
    swd_rsp_valid   : in  std_logic;
    swd_rsp_ack     : in  std_logic_vector(2 downto 0);
    swd_rsp_data    : in  std_logic_vector(63 downto 0);
    swd_rsp_parity  : in  std_logic;
    -- nRESET（オープンドレイン）
    nreset_low      : out std_logic;                 -- '1' で Low を駆動
    nreset_in       : in  std_logic;                 -- ピンの値（非同期。中で同期化する）
    -- JTAG（jtag = '1' の間、swd_* の命令は jtag_phy が受ける。チェーン上の位置は jtag_phy の入力と同じ意味）
    jtag            : out std_logic;
    jtag_dr_before  : out natural range 0 to 7;
    jtag_dr_after   : out natural range 0 to 7;
    jtag_ir_before  : out natural range 0 to 63;
    jtag_ir_after   : out natural range 0 to 63;
    jtag_ir_len     : out natural range 1 to 32;
    -- SWO（UART・Manchester 方式。データは別の経路 = USB の SWO エンドポイントへ流れる）
    swo_run         : out std_logic;                 -- 受信中
    swo_divisor     : out unsigned(15 downto 0);     -- 1 ビットの clk 周期数
    swo_manchester  : out std_logic;                 -- 方式: '1' = Manchester、'0' = UART
    swo_error       : in  std_logic;                 -- 開始以降にフレーム異常があった
    swo_overrun     : in  std_logic;                 -- 開始以降に取りこぼした
    busy            : out std_logic;                 -- コマンドを処理中（観測用）
    -- 共有の割り算器（SHARED_DIVIDER のとき。udiv_shared の使い手の約束: req を done まで保つ）
    div_req         : out std_logic;
    div_num         : out unsigned(31 downto 0);
    div_den         : out unsigned(31 downto 0);
    div_done        : in  std_logic := '0';
    div_quot        : in  unsigned(31 downto 0) := (others => '0')
  );
end entity;

architecture rtl of dap_processor is
  -- dap_packet ⇄ ユニット
  signal unit       : dap_unit_t;
  signal command    : natural range 0 to 255;
  signal start      : std_logic;
  signal get_ack, put_ack : std_logic;
  signal arg        : std_logic_vector(63 downto 0);
  signal done, get_req, put_req : std_logic;
  signal get_n, put_n : natural range 1 to 8;
  signal put_val    : std_logic_vector(63 downto 0);
  signal header     : std_logic_vector(23 downto 0);
  signal header_n   : natural range 0 to 3;
  -- ユニットごとの要求
  type units_bit_t is array (dap_unit_t) of std_logic;
  type units_n_t is array (dap_unit_t) of natural range 1 to 8;
  type units_word_t is array (dap_unit_t) of std_logic_vector(63 downto 0);
  signal u_start, u_done, u_get_req, u_put_req : units_bit_t;
  signal u_get_n, u_put_n : units_n_t;
  signal u_put_val  : units_word_t;
  -- dap_link ⇄ ユニット（シーケンス・転送）
  signal link_req, link_ack : std_logic;
  signal link_op    : std_logic_vector(1 downto 0);
  signal link_count : natural range 1 to 64;
  signal link_request : std_logic_vector(3 downto 0);
  signal link_data  : std_logic_vector(63 downto 0);
  signal link_resp  : std_logic_vector(7 downto 0);
  signal link_ok    : boolean;
  signal link_rdata : std_logic_vector(31 downto 0);
  signal link_val   : std_logic_vector(63 downto 0);
  signal seq_link_req, xfer_link_req : std_logic;
  signal seq_op     : std_logic_vector(1 downto 0);
  signal seq_count  : natural range 1 to 64;
  signal seq_request, xfer_request : std_logic_vector(3 downto 0);
  signal seq_data, xfer_data : std_logic_vector(63 downto 0);
  signal xfer_header : std_logic_vector(23 downto 0);
  signal xfer_header_n : natural range 0 to 3;
  -- 設定
  signal enable, jtag_r : std_logic;
  signal wait_retry, match_retry : unsigned(15 downto 0);
  signal jtag_count : natural range 1 to JTAG_MAX_DEVICES;
  signal jtag_index : natural range 0 to JTAG_MAX_DEVICES - 1;
  signal cfg_div_start, swo_div_start, u_div_done : std_logic;
  signal cfg_div_num, cfg_div_den, swo_div_num, swo_div_den, u_div_quot : unsigned(31 downto 0);
  signal div_pending : std_logic := '0';               -- 割り算を頼んでいる（done まで）
  signal div_for_cfg : std_logic := '0';               -- 頼んだのは config（'0' なら swo）
begin
  packet_i : entity work.dap_packet
    port map (clk => clk, rst => rst, packet_size => packet_size,
              in_valid => in_valid, in_ready => in_ready, in_data => in_data, in_last => in_last,
              out_valid => out_valid, out_ready => out_ready, out_data => out_data, out_last => out_last,
              busy => busy, unit => unit, command => command, start => start, done => done,
              header => header, header_n => header_n,
              get_req => get_req, get_n => get_n, get_ack => get_ack, arg => arg,
              put_req => put_req, put_n => put_n, put_val => put_val, put_ack => put_ack);

  -- 起動したユニット（unit）の要求だけを dap_packet へ通す
  process (all)
  begin
    u_start         <= (others => '0');
    u_start(unit)   <= start;
  end process;
  -- ユニット無し（U_NONE）の分も明示的に駆動する。宣言の初期値は合成で無視され、駆動されない配線は実機で値が
  -- 定まらない（配置で変わり、シミュレーションでは見えない。2026-10-17 に Ethernet 版で DAP の要求が化けた原因）
  u_done(U_NONE)    <= '0';
  u_get_req(U_NONE) <= '0';
  u_put_req(U_NONE) <= '0';
  u_get_n(U_NONE)   <= 1;
  u_put_n(U_NONE)   <= 1;
  u_put_val(U_NONE) <= (others => '0');

  done     <= u_done(unit);
  get_req  <= u_get_req(unit);
  get_n    <= u_get_n(unit);
  put_req  <= u_put_req(unit);
  put_n    <= u_put_n(unit);
  put_val  <= u_put_val(unit);
  header   <= xfer_header when unit = U_TRANSFER else (others => '0');
  header_n <= xfer_header_n when unit = U_TRANSFER else 0;

  config_i : entity work.dap_cmd_config
    generic map (CLK_HZ => CLK_HZ, SWO_BUFFER => SWO_BUFFER, HAS_JTAG => HAS_JTAG, HAS_SWO_MANCHESTER => HAS_SWO_MANCHESTER)
    port map (clk => clk, rst => rst, packet_size => packet_size, command => command,
              start => u_start(U_CONFIG), done => u_done(U_CONFIG),
              get_req => u_get_req(U_CONFIG), get_n => u_get_n(U_CONFIG), get_ack => get_ack, arg => arg,
              put_req => u_put_req(U_CONFIG), put_n => u_put_n(U_CONFIG), put_val => u_put_val(U_CONFIG),
              put_ack => put_ack,
              enable => enable, jtag => jtag_r, half_period => swd_half_period, turnaround => swd_turnaround,
              data_phase => swd_data_phase, idle_cycles => swd_idle_cycles, wait_retry => wait_retry,
              match_retry => match_retry, jtag_count => jtag_count, jtag_index => jtag_index,
              dr_before => jtag_dr_before, dr_after => jtag_dr_after, ir_before => jtag_ir_before,
              ir_after => jtag_ir_after, ir_len => jtag_ir_len,
              div_start => cfg_div_start, div_num => cfg_div_num, div_den => cfg_div_den,
              div_done => u_div_done, div_quot => u_div_quot);

  swo_i : entity work.dap_cmd_swo
    generic map (CLK_HZ => CLK_HZ, HAS_MANCHESTER => HAS_SWO_MANCHESTER)
    port map (clk => clk, rst => rst, command => command,
              start => u_start(U_SWO), done => u_done(U_SWO),
              get_req => u_get_req(U_SWO), get_n => u_get_n(U_SWO), get_ack => get_ack, arg => arg,
              put_req => u_put_req(U_SWO), put_n => u_put_n(U_SWO), put_val => u_put_val(U_SWO),
              put_ack => put_ack,
              swo_run => swo_run, swo_divisor => swo_divisor, swo_manchester => swo_manchester,
              swo_error => swo_error, swo_overrun => swo_overrun,
              div_start => swo_div_start, div_num => swo_div_num, div_den => swo_div_den,
              div_done => u_div_done, div_quot => u_div_quot);

  -- 割り算は SWJ_Clock（config）と SWO_Baudrate（swo）が使う（コマンドは 1 つずつ処理するので同時に頼まない）。
  -- ユニットの div_start（1 周期）を、割り算器の約束（req を done まで保つ）にする。被除数・除数はユニットが保持している
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        div_pending <= '0';
      elsif cfg_div_start = '1' or swo_div_start = '1' then
        div_pending <= '1';
        div_for_cfg <= cfg_div_start;
      elsif u_div_done = '1' then
        div_pending <= '0';
      end if;
    end if;
  end process;
  div_req <= div_pending;
  div_num <= cfg_div_num when div_for_cfg = '1' else swo_div_num;
  div_den <= cfg_div_den when div_for_cfg = '1' else swo_div_den;

  own_divider : if not SHARED_DIVIDER generate
    divider_i : entity work.udiv_shared
      generic map (WIDTH => 32)
      port map (clk => clk, rst => rst, p0_req => div_pending, p0_num => div_num, p0_den => div_den,
                p0_done => u_div_done, p1_done => open, quotient => u_div_quot);
  end generate;
  outside_divider : if SHARED_DIVIDER generate
    u_div_done <= div_done;
    u_div_quot <= div_quot;
  end generate;

  sequence_i : entity work.dap_cmd_sequence
    generic map (CLK_HZ => CLK_HZ)
    port map (clk => clk, rst => rst, command => command,
              start => u_start(U_SEQUENCE), done => u_done(U_SEQUENCE),
              get_req => u_get_req(U_SEQUENCE), get_n => u_get_n(U_SEQUENCE), get_ack => get_ack, arg => arg,
              put_req => u_put_req(U_SEQUENCE), put_n => u_put_n(U_SEQUENCE), put_val => u_put_val(U_SEQUENCE),
              put_ack => put_ack,
              link_req => seq_link_req, link_op => seq_op, link_count => seq_count, link_request => seq_request,
              link_data => seq_data, link_ack => link_ack, link_val => link_val,
              nreset_low => nreset_low, nreset_in => nreset_in);

  transfer_i : entity work.dap_cmd_transfer
    port map (clk => clk, rst => rst, command => command,
              start => u_start(U_TRANSFER), done => u_done(U_TRANSFER),
              header => xfer_header, header_n => xfer_header_n,
              get_req => u_get_req(U_TRANSFER), get_n => u_get_n(U_TRANSFER), get_ack => get_ack, arg => arg,
              put_req => u_put_req(U_TRANSFER), put_n => u_put_n(U_TRANSFER), put_val => u_put_val(U_TRANSFER),
              put_ack => put_ack,
              link_req => xfer_link_req, link_request => xfer_request, link_data => xfer_data,
              link_ack => link_ack, link_resp => link_resp, link_ok => link_ok, link_rdata => link_rdata,
              match_retry => match_retry, jtag => jtag_r, jtag_count => jtag_count, jtag_index => jtag_index);

  -- PHY を使うのはシーケンスと転送のユニットだけ（同時には動かない）
  link_req     <= seq_link_req or xfer_link_req;
  link_op      <= OP_TRANSFER when unit = U_TRANSFER else seq_op;
  link_count   <= 1 when unit = U_TRANSFER else seq_count;
  link_request <= xfer_request when unit = U_TRANSFER else seq_request;
  link_data    <= xfer_data when unit = U_TRANSFER else seq_data;

  link_i : entity work.dap_link
    port map (clk => clk, rst => rst, enable => enable, wait_retry => wait_retry,
              req => link_req, op => link_op, count => link_count, request => link_request, data => link_data,
              ack => link_ack, resp => link_resp, ok => link_ok, rdata => link_rdata, val => link_val,
              phy_valid => swd_cmd_valid, phy_ready => swd_cmd_ready, phy_op => swd_cmd_op,
              phy_count => swd_cmd_count, phy_request => swd_cmd_request, phy_data => swd_cmd_data,
              phy_rsp_valid => swd_rsp_valid, phy_rsp_ack => swd_rsp_ack, phy_rsp_data => swd_rsp_data,
              phy_rsp_parity => swd_rsp_parity);

  swd_enable <= enable;
  jtag       <= jtag_r;
end architecture;
