-- 共通ユーティリティ関数。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package util_pkg is

  -- ボードの LED の心拍用の自走カウンタの幅。最上位ビットが LED になる（sys 100 MHz で 2^25 周期 = 約 0.33 秒ごとに反転）
  constant HEARTBEAT_BITS : positive := 26;

  -- n を表すのに必要なビット数（ceil(log2(n))。n = 1 なら 0）
  function clog2(n : positive) return natural;

  -- 真なら 1、偽なら 0
  function to_natural(b : boolean) return natural;
  function to_sl(b : boolean) return std_logic;

  -- 1 を足す。全部 1 なら止める（飽和の数え。数えた値が折り返して小さく見えないように）
  function sat_inc(a : unsigned) return unsigned;

  -- a を lo〜hi に収める（a と同じ幅）
  function clamp(a : unsigned; lo, hi : natural) return unsigned;

  -- UART 等の 1 ビットの周期数（clk_hz / baud を四捨五入）
  function baud_divisor(clk_hz, baud : positive) return natural;

  -- 2^n で折り返す総数（書いた拍・読んだ拍など、流れの開始から数えて n ビットで回るもの）どうしの差と比べ。
  -- 総数は折り返すので、大小の比べ（>=・<=）を直接使わない（2^32 拍 = 68.7 GB、66.7 MB/s で 17 分で折り返す）。
  -- a・b は同じ幅。比べの約束: 本当の差（折り返さない値どうしの差）の絶対値が 2^(n-1) 未満であること（RFC 1982 の通し番号と同じ）。
  -- mod_diff(a, b) = a - b（mod 2^n）。a が b より後（b <= a の本当の値）なら、本当の差そのもの
  function mod_diff(a, b : unsigned) return unsigned;
  -- a が b 以前（a <= b の本当の値）: (b - a) mod 2^n < 2^(n-1)
  function mod_le(a, b : unsigned) return boolean;
  -- a が b より前（a < b の本当の値）
  function mod_lt(a, b : unsigned) return boolean;

  -- 飽和の和（a + b が a の幅に入らなければ全部 1）。数え（失った数など）の和が折り返して小さく見えないように
  function sat_add(a, b : unsigned) return unsigned;

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

  function to_sl(b : boolean) return std_logic is
  begin
    if b then
      return '1';
    end if;
    return '0';
  end function;

  function sat_inc(a : unsigned) return unsigned is
    variable r : unsigned(a'length - 1 downto 0);
  begin
    r := a;
    if r /= (r'range => '1') then
      r := r + 1;
    end if;
    return r;
  end function;

  function clamp(a : unsigned; lo, hi : natural) return unsigned is
    variable r : unsigned(a'length - 1 downto 0);
  begin
    r := a;
    if r < lo then
      r := to_unsigned(lo, r'length);
    elsif r > hi then
      r := to_unsigned(hi, r'length);
    end if;
    return r;
  end function;

  function baud_divisor(clk_hz, baud : positive) return natural is
  begin
    return (clk_hz + baud / 2) / baud;
  end function;

  function mod_diff(a, b : unsigned) return unsigned is
    variable r : unsigned(a'length - 1 downto 0);
  begin
    r := a - b;                                    -- numeric_std の減算は a の幅で折り返す
    return r;
  end function;

  function mod_le(a, b : unsigned) return boolean is
    variable d : unsigned(a'length - 1 downto 0);
  begin
    d := mod_diff(b, a);
    return d(d'high) = '0';                        -- 差が 2^(n-1) 未満
  end function;

  function mod_lt(a, b : unsigned) return boolean is
  begin
    return mod_le(a, b) and a /= b;
  end function;

  function sat_add(a, b : unsigned) return unsigned is
    variable s : unsigned(a'length downto 0);
    variable r : unsigned(a'length - 1 downto 0);
  begin
    s := resize(a, a'length + 1) + resize(b, a'length + 1);
    if s(s'high) = '1' then
      r := (others => '1');
    else
      r := s(a'length - 1 downto 0);
    end if;
    return r;
  end function;

end package body;
