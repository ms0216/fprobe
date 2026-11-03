-- 8N1 UART 受信器。
--
-- * rx は内部で 2 段同期化する（非同期入力をそのまま接続してよい）。
-- * 開始ビットは半ビット時点で再確認し、それより短いパルスは捨てる。
-- * 停止ビットが '0' なら frame_error を 1 クロック出し、線が '1' に戻るまで次を探さない。
-- * 前のバイトが未消費のまま次のバイトが揃ったら overrun を 1 クロック出し、新しい値で上書きする。
-- * バイトの時刻（out_time）: 開始ビットの辺を見つけた周期（S_IDLE で同期化した線が 0 になった周期）の time を、バイトと一緒に
--   運ぶ（時刻の正しさの R3。docs/superpowers/specs/2026-11-01-time-accuracy-design.md）。端子の立ち下がりは
--   out_time − UART_RX_START_DELAY 以上 + 1 刻み未満のどこか（UART_RX_START_DELAY = 2 段の同期化。S1b の時刻の約束。
--   正は fprobe_gw.timing）。divisor・送り手の速さ・受け手が取り出すまでの待ちに依らない。時刻を使わない使い方は time をつながない（0）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity uart_rx is
  generic (
    DIVISOR_BITS : positive := 16
  );
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    divisor     : in  unsigned(DIVISOR_BITS - 1 downto 0);
    rx          : in  std_logic;
    time        : in  unsigned(47 downto 0) := (others => '0');   -- タイムベース（バイトの時刻に使う）
    out_valid   : out std_logic;
    out_ready   : in  std_logic;
    out_data    : out std_logic_vector(7 downto 0);
    out_time    : out unsigned(47 downto 0);                       -- そのバイトの開始ビットの辺を見つけた周期の time
    frame_error : out std_logic;
    overrun     : out std_logic
  );
end entity;

architecture rtl of uart_rx is
  type state_t is (S_IDLE, S_START, S_DATA, S_STOP, S_BREAK);
  signal state     : state_t := S_IDLE;
  signal rx_sync   : std_logic;
  signal timer     : unsigned(DIVISOR_BITS - 1 downto 0) := (others => '0');
  signal end_at, half_at : unsigned(DIVISOR_BITS - 1 downto 0) := (others => '0');   -- divisor - 1、divisor / 2 - 1
  signal bit_index : natural range 0 to 7 := 0;
  signal shreg     : std_logic_vector(7 downto 0) := (others => '0');
  signal valid     : std_logic := '0';
  signal data      : std_logic_vector(7 downto 0) := (others => '0');
  signal start_t   : unsigned(47 downto 0) := (others => '0');   -- 受けているバイトの開始ビットの時刻
  signal data_t    : unsigned(47 downto 0) := (others => '0');   -- out_data のバイトの時刻
  signal bit_end   : boolean;
  signal half_bit  : boolean;
begin
  sync_i : entity work.sync_bit
    generic map (INIT => '1')
    port map (clk => clk, d => rx, q => rx_sync);

  -- しきい値（divisor から求める）はレジスタに置く: 毎周期の引き算と比較が 1 周期に入り、sys を上げると遅い経路だった
  -- （2026-10-29）。divisor はフレームの間に変えない設定なので、反映が 1 周期遅れても振る舞いは変わらない。
  process (clk)
  begin
    if rising_edge(clk) then
      end_at  <= divisor - 1;
      half_at <= shift_right(divisor, 1) - 1;
    end if;
  end process;
  bit_end  <= timer >= end_at;         -- 途中で divisor を小さくされても通り過ぎない（= だと 2^N 周期止まる）
  half_bit <= timer >= half_at;

  process (clk)
  begin
    if rising_edge(clk) then
      frame_error <= '0';
      overrun     <= '0';
      if valid = '1' and out_ready = '1' then
        valid <= '0';
      end if;
      if bit_end then
        timer <= (others => '0');
      else
        timer <= timer + 1;
      end if;

      if rst = '1' then
        state <= S_IDLE;
        valid <= '0';
      else
        case state is
          when S_IDLE =>
            if rx_sync = '0' then
              timer   <= (others => '0');
              start_t <= time;
              state   <= S_START;
            end if;

          when S_START =>
            if half_bit then
              -- ビット中央に位相を合わせ直す。ここで '1' ならグリッチ
              timer     <= (others => '0');
              bit_index <= 0;
              state     <= S_DATA when rx_sync = '0' else S_IDLE;
            end if;

          when S_DATA =>
            if bit_end then
              shreg <= rx_sync & shreg(7 downto 1);
              if bit_index = 7 then
                state <= S_STOP;
              else
                bit_index <= bit_index + 1;
              end if;
            end if;

          when S_STOP =>
            if bit_end then
              if rx_sync = '1' then
                if valid = '1' and out_ready = '0' then
                  overrun <= '1';
                end if;
                data   <= shreg;
                data_t <= start_t;
                valid  <= '1';
                state <= S_IDLE;
              else
                frame_error <= '1';
                state       <= S_BREAK;
              end if;
            end if;

          when S_BREAK =>
            if rx_sync = '1' then
              state <= S_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;

  out_valid <= valid;
  out_data  <= data;
  out_time  <= data_t;
end architecture;
