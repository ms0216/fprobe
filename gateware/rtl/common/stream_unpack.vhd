-- 幅の広いストリームを 1 バイトずつに分ける（下位バイトから。リトルエンディアン）。
-- 1 語を受け取ったら BYTES バイトを順に出し、出し終えた周期に次の語を受け取れる（語ごとに 1 周期の隙間はない）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity stream_unpack is
  generic (
    BYTES : positive := 16
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    in_valid  : in  std_logic;
    in_ready  : out std_logic;
    in_data   : in  std_logic_vector(8 * BYTES - 1 downto 0);
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of stream_unpack is
  signal word  : std_logic_vector(8 * BYTES - 1 downto 0) := (others => '0');
  signal left  : natural range 0 to BYTES := 0;            -- まだ出していないバイト数
  signal take  : boolean;
begin
  -- 空か、最後のバイトが出ていく周期なら次の語を受け取る
  take      <= left = 0 or (left = 1 and out_ready = '1');
  in_ready  <= '1' when take else '0';
  out_valid <= '1' when left /= 0 else '0';
  out_data  <= word(7 downto 0);

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        left <= 0;
      elsif take and in_valid = '1' then
        word <= in_data;
        left <= BYTES;
      elsif left /= 0 and out_ready = '1' then
        word <= x"00" & word(8 * BYTES - 1 downto 8);
        left <= left - 1;
      end if;
    end if;
  end process;
end architecture;
