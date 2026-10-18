-- USB 試験デザインの応答器（メッセージ単位）。
--
-- 入出力は 9 ビット（bit 8 = メッセージ最終バイト、7..0 = データ）のストリーム。
-- 各メッセージの先頭バイトで扱いを決める:
--   0x00: そのまま折り返す（ループバックの正しさの確認）
--   それ以外（0x01 など）: 捨てる（OUT 方向のスループット計測。出力側の詰まりに影響されない）
library ieee;
use ieee.std_logic_1164.all;

entity usbtest_responder is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    in_valid  : in  std_logic;
    in_ready  : out std_logic;
    in_data   : in  std_logic_vector(8 downto 0);
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(8 downto 0)
  );
end entity;

architecture rtl of usbtest_responder is
  constant ECHO : std_logic_vector(7 downto 0) := x"00";
  signal at_start : std_logic := '1';     -- 次のバイトはメッセージの先頭
  signal echoing  : std_logic := '0';     -- 現在のメッセージを折り返す
  signal echo_now : std_logic;            -- このバイトを折り返す
begin
  echo_now <= '1' when (at_start = '1' and in_data(7 downto 0) = ECHO)
                       or (at_start = '0' and echoing = '1') else '0';

  out_valid <= in_valid and echo_now;
  out_data  <= in_data;
  in_ready  <= out_ready when echo_now = '1' else '1';

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        at_start <= '1';
        echoing  <= '0';
      elsif in_valid = '1' and (echo_now = '0' or out_ready = '1') then
        if at_start = '1' then
          echoing <= echo_now;
        end if;
        at_start <= in_data(8);
      end if;
    end if;
  end process;
end architecture;
