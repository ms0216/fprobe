-- ボードの間の時刻の同期（モジュール化の段階 1。仕様 docs/superpowers/specs/2026-10-22-modular-design.md §3.2・§3.3）。
--
-- 親（master = '1'）: period 刻みごとに、立ち上げを決めた周期の time_count を T として、1 本の線 sync_out に
--   パルス（1 を BIT_TICKS）→ T の 64 ビット（下位から、各 BIT_TICKS）→ CRC-8 の 8 ビット（下位から）→ 0
--   を出す。線はその 1 周期後に 1 になる（TX_LATENCY_TICKS）。
-- 子（enable = '1'）: sync_in（非同期。2 段で同期化）が 1 になったのを見て、その周期の time_count を pair_local に保持し、
--   パルスの中央でまだ 1 かを確かめ（短い雑音では受け始めない）、各ビットの中央で 72 ビットを読む。CRC が合えば pair_valid を 1 周期（pair_local・pair_parent = T）、合わなければ
--   crc_errors を 1 増やして捨てる。pair_local は「入力の線が 1 になった周期」から RX_LATENCY_TICKS 後の time_count。
-- 時刻の換算（ずれと傾き）は PC がする（ここでは組を出すだけ）。
-- CRC-8: 多項式 x^8 + x^2 + x + 1（0x07）、初期値 0xFF（線が途中で 0 に落ちた全部 0 の並びを通さない）、送る順の 64 ビットに 1 ビットずつ（1 周期の深い論理を作らない）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package sync_link_pkg is
  constant TX_LATENCY_TICKS : natural := 1;   -- T を決めた周期 → sync_out が 1
  constant CRC_INIT : std_logic_vector(7 downto 0) := x"FF";
  constant RX_LATENCY_TICKS : natural := 2;   -- sync_in が 1 になった周期 → pair_local の周期（同期化 2。検出は 2 段目と同じ周期）
  function crc8_step(crc : std_logic_vector(7 downto 0); b : std_logic) return std_logic_vector;
end package;

package body sync_link_pkg is
  function crc8_step(crc : std_logic_vector(7 downto 0); b : std_logic) return std_logic_vector is
    variable fb  : std_logic;
    variable nxt : std_logic_vector(7 downto 0);
  begin
    fb := crc(7) xor b;
    nxt := crc(6 downto 0) & '0';
    if fb = '1' then
      nxt := nxt xor x"07";
    end if;
    return nxt;
  end function;
end package body;

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.sync_link_pkg.all;

entity sync_link is
  generic (
    BIT_TICKS : positive := 10                      -- 直列の 1 ビットの刻み（10 = 100 MHz で 10 Mbit/s）
  );
  port (
    clk, rst    : in  std_logic;
    time_count  : in  unsigned(63 downto 0);
    enable      : in  std_logic;                    -- 子として受ける
    master      : in  std_logic;                    -- 親として出す
    period      : in  unsigned(31 downto 0);        -- 親のパルスの間隔（刻み）
    sync_in     : in  std_logic;
    sync_out    : out std_logic;
    pair_valid  : out std_logic;
    pair_local  : out unsigned(63 downto 0);
    pair_parent : out unsigned(63 downto 0);
    crc_errors  : out unsigned(15 downto 0)
  );
end entity;

architecture rtl of sync_link is
  constant SYMBOLS : natural := 73;                 -- パルス 1 + 時刻 64 + CRC 8
  constant GAP_TICKS : natural := 2 * BIT_TICKS;    -- 送りの間の 0 の最小（period が短くても次のパルスを見落とさせない）

  type tx_state_t is (S_TX_IDLE, S_TX_SEND);
  signal tx_state : tx_state_t;
  signal tx_left  : unsigned(31 downto 0);        -- 次の送りまでの残りの刻み（0 で出せる。比べを 0 の判定だけにする）
  signal tx_gap   : natural range 0 to GAP_TICKS;
  signal tx_tick  : natural range 0 to BIT_TICKS - 1;
  signal tx_sym   : natural range 0 to SYMBOLS;
  signal tx_data  : unsigned(63 downto 0);
  signal tx_crc   : std_logic_vector(7 downto 0);
  signal tx_out   : std_logic;

  type rx_state_t is (S_RX_IDLE, S_RX_PULSE, S_RX_BITS);
  signal rx_state : rx_state_t;
  signal rx_s     : std_logic;
  signal rx_prev  : std_logic;
  signal rx_wait  : natural range 0 to 2 * BIT_TICKS;
  signal rx_bit   : natural range 0 to SYMBOLS;
  signal rx_data  : unsigned(63 downto 0);
  signal rx_crc   : std_logic_vector(7 downto 0);
  signal rx_got   : std_logic_vector(7 downto 0);
  signal rx_local : unsigned(63 downto 0);
  signal errors   : unsigned(15 downto 0);
  signal valid_r  : std_logic;
  signal local_r  : unsigned(63 downto 0);
  signal parent_r : unsigned(63 downto 0);
begin
  in_sync : entity work.sync_bit port map (clk => clk, d => sync_in, q => rx_s);

  sync_out    <= tx_out;
  pair_valid  <= valid_r;
  pair_local  <= local_r;
  pair_parent <= parent_r;
  crc_errors  <= errors;

  -- 親: 送る
  tx : process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' or master = '0' then
        tx_state <= S_TX_IDLE;
        tx_left  <= (others => '0');                 -- master を 1 にしたらすぐ最初の送り
        tx_gap   <= GAP_TICKS;
        tx_tick  <= 0;
        tx_sym   <= 0;
        tx_data  <= (others => '0');
        tx_crc   <= (others => '0');
        tx_out   <= '0';
      else
        case tx_state is
          when S_TX_IDLE =>
            tx_out <= '0';
            if tx_gap /= GAP_TICKS then
              tx_gap <= tx_gap + 1;
            end if;
            if tx_left /= 0 then
              tx_left <= tx_left - 1;
            end if;
            if tx_left = 0 and tx_gap = GAP_TICKS then
              if period /= 0 then
                tx_left <= period - 1;               -- 送りの始まりから始まりまで period 刻み
              end if;
              tx_data  <= time_count;                -- T = この周期の time_count
              tx_crc   <= CRC_INIT;
              tx_out   <= '1';                       -- パルス（次の周期から 1）
              tx_tick  <= 0;
              tx_sym   <= 0;
              tx_state <= S_TX_SEND;
            end if;
          when S_TX_SEND =>
            if tx_left /= 0 then
              tx_left <= tx_left - 1;                -- 送っている間も間隔を数える
            end if;
            if tx_tick = BIT_TICKS - 1 then
              tx_tick <= 0;
              if tx_sym = SYMBOLS - 1 then
                tx_out   <= '0';
                tx_gap   <= 0;
                tx_state <= S_TX_IDLE;
              else
                tx_sym <= tx_sym + 1;
                if tx_sym < 64 then                  -- 次の記号 = 時刻のビット tx_sym
                  tx_out  <= tx_data(tx_sym);
                  tx_crc  <= crc8_step(tx_crc, tx_data(tx_sym));
                else                                 -- 次の記号 = CRC のビット tx_sym - 64
                  tx_out <= tx_crc(tx_sym - 64);
                end if;
              end if;
            else
              tx_tick <= tx_tick + 1;
            end if;
        end case;
      end if;
    end if;
  end process;

  -- 子: 受ける
  rx : process (clk)
  begin
    if rising_edge(clk) then
      valid_r <= '0';
      if rst = '1' or enable = '0' then
        errors <= (others => '0');                         -- 数は受けている間だけ（enable の 0 → 1 で 0 から）
      end if;
      if rst = '1' then
        rx_state <= S_RX_IDLE;
        rx_prev  <= '0';
        rx_wait  <= 0;
        rx_bit   <= 0;
        rx_data  <= (others => '0');
        rx_crc   <= (others => '0');
        rx_got   <= (others => '0');
        rx_local <= (others => '0');
        local_r  <= (others => '0');
        parent_r <= (others => '0');
      else
        rx_prev <= rx_s;
        case rx_state is
          when S_RX_IDLE =>
            if enable = '1' and rx_s = '1' and rx_prev = '0' then
              rx_local <= time_count;
              rx_wait  <= BIT_TICKS / 2 - 1;           -- パルスの中央まで
              rx_bit   <= 0;
              rx_crc   <= CRC_INIT;
              rx_state <= S_RX_PULSE;
            end if;
          when S_RX_PULSE =>
            if rx_wait = 0 then
              if rx_s = '1' then
                rx_wait  <= BIT_TICKS - 1;             -- 最初のビットの中央まで
                rx_state <= S_RX_BITS;
              else
                rx_state <= S_RX_IDLE;                 -- パルスの半分より短い: 受けない
              end if;
            else
              rx_wait <= rx_wait - 1;
            end if;
          when S_RX_BITS =>
            if rx_wait = 0 then
              rx_wait <= BIT_TICKS - 1;
              if rx_bit < 64 then
                rx_data(rx_bit) <= rx_s;
                rx_crc <= crc8_step(rx_crc, rx_s);
              else
                rx_got(rx_bit - 64) <= rx_s;
              end if;
              if rx_bit = SYMBOLS - 2 then           -- 最後（CRC の 8 ビット目）
                rx_state <= S_RX_IDLE;
                if rx_crc = (rx_s & rx_got(6 downto 0)) then
                  valid_r  <= '1';
                  local_r  <= rx_local;
                  parent_r <= rx_data;
                elsif errors /= x"FFFF" and enable = '1' then
                  errors <= errors + 1;
                end if;
              end if;
              rx_bit <= rx_bit + 1;
            else
              rx_wait <= rx_wait - 1;
            end if;
        end case;
        if enable = '0' then
          rx_state <= S_RX_IDLE;                       -- 受けている途中のフレームも捨てる（case の後: 上書きされない）
        end if;
      end if;
    end if;
  end process;
end architecture;
