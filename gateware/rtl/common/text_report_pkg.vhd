-- テキスト報告（デバッグ・実機確認用）のテンプレート記述。text_report と組み合わせて使う。
--
--   constant MSG : template_t := lit("vid=") & hex(0) & hex(1) & lit(CR & LF);
--
-- lit は文字列そのもの、hex(n) はフィールド n（8 ビット）を 16 進 2 桁で表す。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package text_report_pkg is

  constant MAX_FIELDS : positive := 16;

  type item_t is record
    is_hex : boolean;
    char   : character;
    field  : natural range 0 to MAX_FIELDS - 1;
    high   : boolean;              -- 上位ニブル
  end record;
  type template_t is array (natural range <>) of item_t;
  type fields_t is array (0 to MAX_FIELDS - 1) of std_logic_vector(7 downto 0);

  function lit(s : string) return template_t;
  function hex(field : natural) return template_t;
  function hex_char(nibble : std_logic_vector(3 downto 0)) return std_logic_vector;

end package;

package body text_report_pkg is

  function lit(s : string) return template_t is
    variable t : template_t(0 to s'length - 1);
  begin
    for i in t'range loop
      t(i) := (false, s(s'low + i), 0, false);
    end loop;
    return t;
  end function;

  function hex(field : natural) return template_t is
  begin
    return ((true, ' ', field, true), (true, ' ', field, false));
  end function;

  function hex_char(nibble : std_logic_vector(3 downto 0)) return std_logic_vector is
    constant DIGITS : string(1 to 16) := "0123456789ABCDEF";
  begin
    return std_logic_vector(to_unsigned(character'pos(DIGITS(to_integer(unsigned(nibble)) + 1)), 8));
  end function;

end package body;
