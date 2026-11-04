-- 外部メモリのリングの割り振り（la_deep_buffer・la_mem_writer で共有）: 2^addr_bits 拍のうち、上の 1/16（2^(addr_bits − 4) 拍）が
-- イベントのリング、残り（下）がロジアナのリング。イベントのリングはロジアナのリングの直後（番地 = ロジアナの拍の数 + 位置）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package mem_ring_pkg is

  -- イベントのリングの位置のビット数
  function ring_ev_bits(addr_bits : positive) return natural;
  -- イベントのリングの拍の数（addr_bits + 1 ビット）
  function ring_ev_beats(addr_bits : positive) return unsigned;
  -- ロジアナのリングの拍の数（addr_bits + 1 ビット）
  function ring_la_beats(addr_bits : positive) return unsigned;

end package;

package body mem_ring_pkg is

  function ring_ev_bits(addr_bits : positive) return natural is
  begin
    return addr_bits - 4;
  end function;

  function ring_ev_beats(addr_bits : positive) return unsigned is
  begin
    return shift_left(to_unsigned(1, addr_bits + 1), ring_ev_bits(addr_bits));
  end function;

  function ring_la_beats(addr_bits : positive) return unsigned is
  begin
    return shift_left(to_unsigned(1, addr_bits + 1), addr_bits) - ring_ev_beats(addr_bits);
  end function;

end package body;
