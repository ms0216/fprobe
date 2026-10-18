-- SWO の受信（UART = NRZ 方式と、Manchester 方式）。仕様: docs/superpowers/specs/2026-10-16-m6b-swo-design.md、
-- docs/superpowers/specs/2026-10-19-u51-u52-swo-uart-design.md
--
-- run = '1' の間、swo を divisor（1 ビットの clk 周期数）で受けたバイトを out ストリームに出す。
-- manchester = '1' なら Manchester 方式（swo_manchester_rx）、'0' なら UART 方式（uart_rx、8N1）。HAS_MANCHESTER = false の
-- 構成には Manchester の回路が無く、manchester は無視する（dap_cmd_swo が Mode 2 を断る）。
-- run の 0→1 で error / overrun を消す。error = フレーム異常（UART: 停止ビットが 0、Manchester: 8 ビットに満たない
-- 端数でフレームが終わった）があった、overrun = 下流（USB）が詰まって捨てたバイトがあった（黙って捨てない: 状態で申告する）。
--
-- run の 0→1 から FLUSH_CYCLES 周期は flush = '1' にして何も出さない（B10）。下流（ホストリンクの SWO の
-- バッファ）は flush の間に前の受信の残り（ホストが読まずに止めたバイト）を捨てる。窓の間に揃ったバイトを
-- 捨てるのは、受信を窓の分だけ遅く始めたのと同じ（受信はもともと流れの途中から始まる）。
--
-- 最高速度（clk = 100 MHz、divisor の下限 4 = 25 Mbit/s。tests/tb/swo_receiver_modes_tb.py・swo_manchester_rx_tb.py）:
--   UART: 開始ビットの検出の後は divisor ごとに数えるだけなので、送り手との速さのずれが 10 ビットで溜まる。
--         受けられるずれは divisor = 4 で -1〜+5%、5 以上で -3〜+4〜5%（正 = 送り手が遅い）。TPIU の分周
--         （TRACECLKIN / (ACPR+1)）と 100.29 MHz / divisor が合わなければ、速さを下げて divisor を大きくする。
--   Manchester: ビットごとに同期し直すので速さのずれが溜まらない（divisor = 8 で ±25%、線のひずみ ±5 ns で -15〜+10%）。
--         divisor = 4 は線のひずみの余裕が無い（2 ns で欠ける）。実用の上限は divisor = 5〜6（20〜16.7 Mbit/s）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity swo_receiver is
  generic (
    HAS_MANCHESTER : boolean := true;      -- Manchester 方式の受信回路を持つ
    FLUSH_CYCLES   : positive := 32        -- 開始の窓（下流が残りを捨てるのに要る周期。クロックの乗せ換えを含む）
  );
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    run        : in  std_logic;
    manchester : in  std_logic;                      -- '1' = Manchester 方式（HAS_MANCHESTER のときだけ効く）
    divisor    : in  unsigned(15 downto 0);
    swo        : in  std_logic;                      -- 非同期でよい
    out_valid  : out std_logic;
    out_ready  : in  std_logic;
    out_data   : out std_logic_vector(7 downto 0);
    flush      : out std_logic;                      -- 開始の窓: 下流は SWO の残りを捨てる
    error      : out std_logic;
    overrun    : out std_logic
  );
end entity;

architecture rtl of swo_receiver is
  signal uart_valid, uart_frame_error, uart_overrun : std_logic;
  signal uart_data  : std_logic_vector(7 downto 0);
  signal man_valid, man_frame_error : std_logic;
  signal man_data   : std_logic_vector(7 downto 0);
  signal use_man    : std_logic;
  signal rx_valid, rx_frame_error, rx_overrun : std_logic;
  signal rx_data    : std_logic_vector(7 downto 0);
  signal run_prev   : std_logic := '0';
  signal error_r    : std_logic := '0';
  signal overrun_r  : std_logic := '0';
  signal window     : natural range 0 to FLUSH_CYCLES := 0;    -- 開始の窓の残り
  signal flush_r    : std_logic := '0';
  signal active     : std_logic;                                -- 受けたバイトを下流に出す
begin
  uart_i : entity work.uart_rx
    generic map (DIVISOR_BITS => 16)
    port map (clk => clk, rst => rst, divisor => divisor, rx => swo, out_valid => uart_valid,
              out_ready => '1', out_data => uart_data, frame_error => uart_frame_error, overrun => uart_overrun);

  manchester_g : if HAS_MANCHESTER generate
    manchester_i : entity work.swo_manchester_rx
      generic map (DIVISOR_BITS => 16)
      port map (clk => clk, rst => rst, divisor => divisor, rx => swo, out_valid => man_valid,
                out_data => man_data, frame_error => man_frame_error);
    use_man <= manchester;
  end generate;

  no_manchester_g : if not HAS_MANCHESTER generate
    man_valid       <= '0';
    man_data        <= (others => '0');
    man_frame_error <= '0';
    use_man         <= '0';
  end generate;

  rx_valid       <= man_valid when use_man = '1' else uart_valid;
  rx_data        <= man_data when use_man = '1' else uart_data;
  rx_frame_error <= man_frame_error when use_man = '1' else uart_frame_error;
  rx_overrun     <= '0' when use_man = '1' else uart_overrun;

  -- 受けたバイトは 1 周期だけ有効（受信器は毎周期取り出す）。下流が受けられなければ捨てて overrun
  active    <= '1' when run = '1' and run_prev = '1' and window = 0 else '0';
  out_valid <= rx_valid and active;
  out_data  <= rx_data;
  flush     <= flush_r;

  process (clk)
  begin
    if rising_edge(clk) then
      run_prev <= run;
      -- flush は window /= 0 と同じ値を FF から出す（別のクロックのドメインへ渡すので組み合わせのひげを出さない）
      if rst = '1' or run = '0' then
        window  <= 0;
        flush_r <= '0';
      elsif run_prev = '0' then
        window  <= FLUSH_CYCLES;
        flush_r <= '1';
      elsif window /= 0 then
        window  <= window - 1;
        if window = 1 then
          flush_r <= '0';
        end if;
      end if;

      if rst = '1' or (run = '1' and run_prev = '0') then
        error_r   <= '0';
        overrun_r <= '0';
      elsif active = '1' then
        if rx_frame_error = '1' then
          error_r <= '1';
        end if;
        if rx_overrun = '1' or (rx_valid = '1' and out_ready = '0') then
          overrun_r <= '1';
        end if;
      end if;
    end if;
  end process;

  error   <= error_r;
  overrun <= overrun_r;
end architecture;
