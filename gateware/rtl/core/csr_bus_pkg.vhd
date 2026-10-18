-- CSR バス（sys ドメイン、単一マスタ）の型。
--
-- 2 相ハンドシェイク: マスタは bus_req を bus_ack まで保持する（bus_we / bus_addr / bus_wdata も保持）。
-- スレーブは完了の周期に bus_ack を 1 周期出し、同時に bus_rdata（読み出し時）と bus_err を有効にする。
-- アドレスはワード（32 ビット）単位。窓は 2 の冪のサイズで、その倍数のベースに置く。
-- スレーブはマスタの bus_we / bus_addr / bus_wdata を直接受け、窓内の位置はアドレスの下位ビットで決める。
-- スレーブは bus_ack を出す周期以外、bus_rdata と bus_err を 0 にする（csr_decoder は窓の応答の論理和を取る。
-- 窓ごとの選択が要らない）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package csr_bus_pkg is

  subtype csr_addr_t is unsigned(29 downto 0);
  subtype csr_word_t is std_logic_vector(31 downto 0);
  type csr_word_array_t is array (natural range <>) of csr_word_t;

  type csr_window_t is record
    base : natural;      -- ワードアドレス
    size : positive;     -- ワード数（2 の冪。base はその倍数）
  end record;
  type csr_windows_t is array (natural range <>) of csr_window_t;

  function csr_hit(addr : csr_addr_t; window : csr_window_t) return boolean;
  function csr_window_ok(window : csr_window_t) return boolean;

end package;

package body csr_bus_pkg is

  function csr_hit(addr : csr_addr_t; window : csr_window_t) return boolean is
  begin
    return addr / window.size = window.base / window.size;
  end function;

  function csr_window_ok(window : csr_window_t) return boolean is
    variable size : natural := 1;
  begin
    while size < window.size loop
      size := size * 2;
    end loop;
    return size = window.size and window.base mod window.size = 0;
  end function;

end package body;
