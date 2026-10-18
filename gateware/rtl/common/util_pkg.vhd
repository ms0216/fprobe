-- 共通ユーティリティ関数。
package util_pkg is

  -- n を表すのに必要なビット数（ceil(log2(n))。n = 1 なら 0）
  function clog2(n : positive) return natural;

  -- 真なら 1、偽なら 0
  function to_natural(b : boolean) return natural;

end package;

package body util_pkg is

  function clog2(n : positive) return natural is
    variable bits : natural := 0;
  begin
    while 2 ** bits < n loop
      bits := bits + 1;
    end loop;
    return bits;
  end function;

  function to_natural(b : boolean) return natural is
  begin
    if b then
      return 1;
    end if;
    return 0;
  end function;

end package body;
