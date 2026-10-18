-- USB スタック内で共有する型と定数。
library ieee;
use ieee.std_logic_1164.all;

package usb_types_pkg is

  subtype pid_t is std_logic_vector(3 downto 0);

  constant PID_OUT   : pid_t := x"1";
  constant PID_IN    : pid_t := x"9";
  constant PID_SOF   : pid_t := x"5";
  constant PID_SETUP : pid_t := x"D";
  constant PID_PING  : pid_t := x"4";
  constant PID_DATA0 : pid_t := x"3";
  constant PID_DATA1 : pid_t := x"B";
  constant PID_ACK   : pid_t := x"2";
  constant PID_NAK   : pid_t := x"A";
  constant PID_STALL : pid_t := x"E";
  constant PID_NYET  : pid_t := x"6";

  constant NUM_ENDPOINTS : positive := 16;
  subtype ep_mask_t is std_logic_vector(NUM_ENDPOINTS - 1 downto 0);   -- ビット n = エンドポイント n
  type ep_bytes_t is array (0 to NUM_ENDPOINTS - 1) of std_logic_vector(7 downto 0);

  constant MPS_CONTROL : positive := 64;    -- EP0
  constant MPS_BULK_HS : positive := 512;
  constant MPS_BULK_FS : positive := 64;

end package;
