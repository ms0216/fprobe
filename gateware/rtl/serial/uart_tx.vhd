-- 8N1 UART 送信器。
--
-- * ボーレートは実行時の分周比 divisor（1 ビットあたりの clk 数, 2 以上）で指定する。
-- * 入力はストリーム（in_valid/in_ready/in_data）。連続送信時のフレーム周期はちょうど
--   10 * divisor クロック（停止ビットの最後のクロックで次のフレームを受理する）。
-- * tx はフレームシフトレジスタの LSB をそのまま出す（レジスタ出力でグリッチなし）。
--   リセット中・アイドル時は '1'。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity uart_tx is
  generic (
    DIVISOR_BITS : positive := 16
  );
  port (
    clk      : in  std_logic;
    rst      : in  std_logic;
    divisor  : in  unsigned(DIVISOR_BITS - 1 downto 0);
    in_valid : in  std_logic;
    in_ready : out std_logic;
    in_data  : in  std_logic_vector(7 downto 0);
    tx       : out std_logic;
    busy     : out std_logic
  );
end entity;

architecture rtl of uart_tx is
  signal frame     : std_logic_vector(9 downto 0) := (others => '1');  -- LSB から送出
  signal bits_left : natural range 0 to 10 := 0;
  signal timer     : unsigned(DIVISOR_BITS - 1 downto 0) := (others => '0');
  signal bit_end   : boolean;
  signal ready     : std_logic;
begin
  bit_end <= timer = divisor - 1;
  ready   <= '1' when bits_left = 0 or (bits_left = 1 and bit_end) else '0';

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        frame     <= (others => '1');
        bits_left <= 0;
        timer     <= (others => '0');
      elsif in_valid = '1' and ready = '1' then
        frame     <= '1' & in_data & '0';   -- 停止 / データ (LSB first) / 開始
        bits_left <= 10;
        timer     <= (others => '0');
      elsif bits_left /= 0 then
        if bit_end then
          frame     <= '1' & frame(9 downto 1);
          bits_left <= bits_left - 1;
          timer     <= (others => '0');
        else
          timer <= timer + 1;
        end if;
      end if;
    end if;
  end process;

  in_ready <= ready;
  tx       <= frame(0);
  busy     <= '0' when bits_left = 0 else '1';
end architecture;
