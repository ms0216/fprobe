-- USB 2.0 の CRC5（トークン）と CRC16（データ）。
-- 根拠: docs/reference/usb2-hs-device-on-ulpi.md §4
--
-- CRC16 は反射形（LSB first）のレジスタで扱う: 初期値 CRC16_INIT、1 バイトずつ crc16_next で更新し、
-- 送信値は not（下位バイトから送る）。受信ではペイロードと CRC 2 バイトを通した値が CRC16_RESIDUAL なら正常。
-- PID バイトは CRC に含めない。
library ieee;
use ieee.std_logic_1164.all;

package usb_crc_pkg is

  constant CRC16_INIT     : std_logic_vector(15 downto 0) := x"FFFF";
  constant CRC16_RESIDUAL : std_logic_vector(15 downto 0) := x"B001";

  function crc16_next(crc : std_logic_vector(15 downto 0); data : std_logic_vector(7 downto 0))
    return std_logic_vector;

  -- トークン 2・3 バイト目（field(i) = 線上で i 番目に送られるビット）の CRC5 が正しいか
  function crc5_token_ok(field : std_logic_vector(15 downto 0)) return boolean;

end package;

package body usb_crc_pkg is

  function crc16_next(crc : std_logic_vector(15 downto 0); data : std_logic_vector(7 downto 0))
    return std_logic_vector is
    variable r : std_logic_vector(15 downto 0) := crc xor (x"00" & data);
  begin
    for i in 0 to 7 loop
      if r(0) = '1' then
        r := ('0' & r(15 downto 1)) xor x"A001";
      else
        r := '0' & r(15 downto 1);
      end if;
    end loop;
    return r;
  end function;

  function crc5_token_ok(field : std_logic_vector(15 downto 0)) return boolean is
    variable r        : std_logic_vector(4 downto 0) := "11111";
    variable feedback : std_logic;
  begin
    -- 多項式 x^5 + x^2 + 1、初期値 11111、MSB 側からシフトする形。全 16 ビットを通した剰余は 01100
    for i in 0 to 15 loop
      feedback := r(4) xor field(i);
      r := r(3 downto 0) & '0';
      if feedback = '1' then
        r := r xor "00101";
      end if;
    end loop;
    return r = "01100";
  end function;

end package body;
