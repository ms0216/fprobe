-- 8N1 UART 受信器。
--
-- * rx は内部で 2 段同期化する（非同期入力をそのまま接続してよい）。
-- * 開始ビットは半ビット時点で再確認し、それより短いパルスは捨てる。
-- * 停止ビットが '0' なら frame_error を 1 クロック出し、線が '1' に戻るまで次を探さない。
-- * 前のバイトが未消費のまま次のバイトが揃ったら overrun を 1 クロック出し、新しい値で上書きする。
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
    out_valid   : out std_logic;
    out_ready   : in  std_logic;
    out_data    : out std_logic_vector(7 downto 0);
    frame_error : out std_logic;
    overrun     : out std_logic
  );
end entity;

architecture rtl of uart_rx is
  type state_t is (S_IDLE, S_START, S_DATA, S_STOP, S_BREAK);
  signal state     : state_t := S_IDLE;
  signal rx_sync   : std_logic;
  signal timer     : unsigned(DIVISOR_BITS - 1 downto 0) := (others => '0');
  signal bit_index : natural range 0 to 7 := 0;
  signal shreg     : std_logic_vector(7 downto 0) := (others => '0');
  signal valid     : std_logic := '0';
  signal data      : std_logic_vector(7 downto 0) := (others => '0');
  signal bit_end   : boolean;
  signal half_bit  : boolean;
begin
  sync_i : entity work.sync_bit
    generic map (INIT => '1')
    port map (clk => clk, d => rx, q => rx_sync);

  bit_end  <= timer >= divisor - 1;    -- 途中で divisor を小さくされても通り過ぎない（= だと 2^N 周期止まる）
  half_bit <= timer >= shift_right(divisor, 1) - 1;

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
              timer <= (others => '0');
              state <= S_START;
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
                data  <= shreg;
                valid <= '1';
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
end architecture;
