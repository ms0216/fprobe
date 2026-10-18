-- ホスト（CDC-ACM）とターゲットの UART をつなぐブリッジ（8N1、sys ドメイン）。
-- 仕様: docs/superpowers/specs/2026-10-16-m6-uart-swo-design.md
--
-- ボーレートは baud_load（1 周期）で受け取り、分周比 round(CLK_HZ / baud) を求めて送受信の両方に使う
-- （計算中も前の分周比で動き続ける。切り替えの瞬間のバイトは崩れうる）。baud = 0 は無視する。
-- 計算中に次のボーレートが来たら、計算の後にそれで計算し直す（最後の値が残る）。
-- 割り算器は SHARED_DIVIDER なら外（div_* ポート、udiv_shared の使い手の約束）、でなければ中に持つ。
-- リセット直後は DEFAULT_BAUD。分周比は 2〜2^20-1（100 MHz で約 95 baud〜50 Mbaud）に制限する。
-- 受信の異常（停止ビットが 0、取り出されないうちに次のバイトが揃った）は数えて観測できるようにする。
--
-- ブレーク（brk = '1'）: 送信の線を Low に保つ（送りかけのバイトは崩れる。新しいバイトは受け取らない）。
-- フロー制御（FLOW のときだけ。docs/superpowers/specs/2026-10-19-u51-u52-swo-uart-design.md §4）:
--   線の RTS / CTS は UART の慣習どおりアクティブ Low（rts_n・cts_n）。
--   flow_auto = '1'（自動の RTS/CTS）: CTS が出ている間だけ次のバイトを送り始める（送りかけは最後まで送る）。
--     RTS は受信の下流（out）が受けられる間だけ出す（下流が満杯で RTS を下げる。uart_rx の 1 バイトの保持があるので、
--     相手が RTS を見てから止まるまでの 1 バイトは失わない）。
--   flow_auto = '0': RTS はホストの要求（rts_host = CDC-ACM の SET_CONTROL_LINE_STATE）どおり、CTS は見ない。
--   cts は CTS の状態（同期化した値、出ている = 1）。FLOW = false なら rts_n = '1'（出さない）、cts = '1'。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity uart_bridge is
  generic (
    CLK_HZ         : positive;
    DEFAULT_BAUD   : positive := 115_200;
    SHARED_DIVIDER : boolean := false;
    FLOW           : boolean := false      -- RTS / CTS のフロー制御の回路を持つ
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    baud         : in  unsigned(31 downto 0);
    baud_load    : in  std_logic;
    -- ホスト → ターゲット
    in_valid     : in  std_logic;
    in_ready     : out std_logic;
    in_data      : in  std_logic_vector(7 downto 0);
    -- ターゲット → ホスト
    out_valid    : out std_logic;
    out_ready    : in  std_logic;
    out_data     : out std_logic_vector(7 downto 0);
    -- ピン
    rx           : in  std_logic;
    tx           : out std_logic;
    -- ブレークとフロー制御（線は非同期でよい）
    brk          : in  std_logic := '0';
    flow_auto    : in  std_logic := '0';
    rts_host     : in  std_logic := '0';
    cts_n        : in  std_logic := '0';
    rts_n        : out std_logic;
    cts          : out std_logic;
    -- 観測
    current_baud : out unsigned(31 downto 0);
    frame_errors : out unsigned(31 downto 0);
    overruns     : out unsigned(31 downto 0);
    -- 共有の割り算器（SHARED_DIVIDER のとき）
    div_req      : out std_logic;
    div_num      : out unsigned(31 downto 0);
    div_den      : out unsigned(31 downto 0);
    div_done     : in  std_logic := '0';
    div_quot     : in  unsigned(31 downto 0) := (others => '0')
  );
end entity;

architecture rtl of uart_bridge is
  constant DIVISOR_BITS : positive := 20;
  constant MAX_DIVISOR  : natural := 2 ** DIVISOR_BITS - 1;

  signal divisor   : unsigned(DIVISOR_BITS - 1 downto 0) :=
    to_unsigned((CLK_HZ + DEFAULT_BAUD / 2) / DEFAULT_BAUD, DIVISOR_BITS);
  signal baud_r    : unsigned(31 downto 0) := to_unsigned(DEFAULT_BAUD, 32);
  signal pending   : unsigned(31 downto 0) := (others => '0');   -- 計算中のボーレート
  signal wanted    : unsigned(31 downto 0) := (others => '0');   -- 最後に来たボーレート
  signal stale     : std_logic := '0';                           -- wanted をまだ計算していない
  signal req       : std_logic := '0';
  signal done      : std_logic;
  signal quotient  : unsigned(31 downto 0);
  signal ferr, orun : std_logic;
  signal ferr_count, orun_count : unsigned(31 downto 0) := (others => '0');
  signal tx_line, tx_ready : std_logic;
  signal send_ok : std_logic;                                    -- 次のバイトを送り始めてよい
  signal cts_r   : std_logic;
begin
  div_req <= req;
  div_num <= to_unsigned(CLK_HZ, 32) + shift_right(pending, 1);      -- 四捨五入
  div_den <= pending;

  own_divider : if not SHARED_DIVIDER generate
    divider_i : entity work.udiv_shared
      generic map (WIDTH => 32)
      port map (clk => clk, rst => rst, p0_req => req, p0_num => div_num, p0_den => div_den,
                p0_done => done, p1_done => open, quotient => quotient);
  end generate;
  outside_divider : if SHARED_DIVIDER generate
    done     <= div_done;
    quotient <= div_quot;
  end generate;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        divisor    <= to_unsigned((CLK_HZ + DEFAULT_BAUD / 2) / DEFAULT_BAUD, DIVISOR_BITS);
        baud_r     <= to_unsigned(DEFAULT_BAUD, 32);
        ferr_count <= (others => '0');
        orun_count <= (others => '0');
        req        <= '0';
        stale      <= '0';
      else
        if baud_load = '1' and baud /= 0 then
          wanted <= baud;
          stale  <= '1';
        end if;
        if req = '0' and stale = '1' and baud_load = '0' then
          pending <= wanted;
          stale   <= '0';
          req     <= '1';
        end if;
        if done = '1' then
          req    <= '0';
          baud_r <= pending;
          if quotient > MAX_DIVISOR then
            divisor <= to_unsigned(MAX_DIVISOR, DIVISOR_BITS);
          elsif quotient < 2 then
            divisor <= to_unsigned(2, DIVISOR_BITS);
          else
            divisor <= quotient(DIVISOR_BITS - 1 downto 0);
          end if;
        end if;
        if ferr = '1' then
          ferr_count <= ferr_count + 1;
        end if;
        if orun = '1' then
          orun_count <= orun_count + 1;
        end if;
      end if;
    end if;
  end process;

  flow_g : if FLOW generate
    signal cts_sync : std_logic;
  begin
    cts_sync_i : entity work.sync_bit
      generic map (INIT => '1')
      port map (clk => clk, d => cts_n, q => cts_sync);
    cts_r   <= not cts_sync;
    send_ok <= not brk and (not flow_auto or cts_r);
    process (clk)
    begin
      if rising_edge(clk) then
        if rst = '1' then
          rts_n <= '1';
        elsif flow_auto = '1' then
          rts_n <= not out_ready;
        else
          rts_n <= not rts_host;
        end if;
      end if;
    end process;
  end generate;

  no_flow_g : if not FLOW generate
    cts_r   <= '1';
    send_ok <= not brk;
    rts_n   <= '1';
  end generate;

  cts <= cts_r;

  tx_i : entity work.uart_tx
    generic map (DIVISOR_BITS => DIVISOR_BITS)
    port map (clk => clk, rst => rst, divisor => divisor, in_valid => in_valid and send_ok, in_ready => tx_ready,
              in_data => in_data, tx => tx_line, busy => open);
  in_ready <= tx_ready and send_ok;
  tx       <= tx_line and not brk;

  rx_i : entity work.uart_rx
    generic map (DIVISOR_BITS => DIVISOR_BITS)
    port map (clk => clk, rst => rst, divisor => divisor, rx => rx, out_valid => out_valid,
              out_ready => out_ready, out_data => out_data, frame_error => ferr, overrun => orun);

  current_baud <= baud_r;
  frame_errors <= ferr_count;
  overruns     <= orun_count;
end architecture;
