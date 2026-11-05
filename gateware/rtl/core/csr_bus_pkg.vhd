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

  -- 窓への要求（全部の窓で共通。窓ごとの選択 bus_req は別の std_logic で渡す）と、窓の応答。
  -- record のポートは GHDL の VPI が駆動できないので、cocotb の toplevel にする entity では使わない（ラッパーを通す）。
  type csr_cmd_t is record
    we    : std_logic;
    addr  : csr_addr_t;
    wdata : csr_word_t;
  end record;
  type csr_rsp_t is record
    ack   : std_logic;
    err   : std_logic;
    rdata : csr_word_t;                 -- 応答の周期以外は err・rdata とも 0（上の約束）
  end record;
  type csr_rsp_array_t is array (natural range <>) of csr_rsp_t;
  constant CSR_RSP_IDLE : csr_rsp_t := (ack => '0', err => '0', rdata => (others => '0'));

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
