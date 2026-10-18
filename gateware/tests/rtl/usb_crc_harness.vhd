-- テスト専用: usb_crc_pkg の関数を組み合わせ回路として露出する
library ieee;
use ieee.std_logic_1164.all;
use work.usb_crc_pkg.all;

entity usb_crc_harness is
  port (
    crc_in       : in  std_logic_vector(15 downto 0);
    byte_in      : in  std_logic_vector(7 downto 0);
    crc_out      : out std_logic_vector(15 downto 0);
    token_field  : in  std_logic_vector(15 downto 0);
    crc5_ok      : out std_logic
  );
end entity;

architecture rtl of usb_crc_harness is
begin
  crc_out <= crc16_next(crc_in, byte_in);
  crc5_ok <= '1' when crc5_token_ok(token_field) else '0';
end architecture;
