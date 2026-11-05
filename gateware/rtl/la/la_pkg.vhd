-- ロジアナの回路（la_front・la_lane_front）で共有する関数。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package la_pkg is

  -- トリガのエッジの条件: rise のビットの ch が 0 → 1、または fall のビットの ch が 1 → 0 に変わった（前 prev、今 cur）。
  -- 全部の引数は同じ幅・同じ範囲。rise・fall が全部 0 のときの扱い（条件なし = 常に真）は使う側が決める
  function trig_edge(rise, fall, cur, prev : std_logic_vector) return boolean;

  -- RLE の 1 レコード（32 ビット。1 拍 128 ビットに 4 つ、下位から）: [31:16] 同じ値が続いたサンプルの数 − 1、[15:0] 値
  -- （WIDTH ビット、上は 0）。la_encoder・la_lane_rle が組み、la_stream_gap が拍のサンプルの数を数える
  function rle_record(run_minus_1 : unsigned(15 downto 0); value : std_logic_vector) return std_logic_vector;
  -- 拍 beat の r 番目（0〜3）のレコードの「続いた数 − 1」
  function rle_run_minus_1(beat : std_logic_vector(127 downto 0); r : natural) return unsigned;

  -- N 並列の語（レーン k の ch c = w(k × width + c)。k = 0 が最も古い）の、レーン k の width ビット。
  -- w の範囲は downto 0 でなくてもよい（w'low からの相対）。la_lane_front・la_lane_rle が使う
  function lane_slice(w : std_logic_vector; width : positive; k : natural) return std_logic_vector;

end package;

package body la_pkg is

  function trig_edge(rise, fall, cur, prev : std_logic_vector) return boolean is
  begin
    return ((rise and cur and not prev) or (fall and prev and not cur)) /= (cur'range => '0');
  end function;


  function rle_record(run_minus_1 : unsigned(15 downto 0); value : std_logic_vector) return std_logic_vector is
    variable rec : std_logic_vector(31 downto 0) := (others => '0');
  begin
    rec(31 downto 16) := std_logic_vector(run_minus_1);
    rec(value'length - 1 downto 0) := value;
    return rec;
  end function;

  function rle_run_minus_1(beat : std_logic_vector(127 downto 0); r : natural) return unsigned is
  begin
    return unsigned(beat(32 * r + 31 downto 32 * r + 16));
  end function;

  function lane_slice(w : std_logic_vector; width : positive; k : natural) return std_logic_vector is
  begin
    return w(k * width + width - 1 + w'low downto k * width + w'low);
  end function;

end package body;
