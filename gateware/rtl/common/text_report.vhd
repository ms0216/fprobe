-- テンプレートに沿った報告文をバイトストリームで出す（UART 送信器などにつなぐ）。
--
-- start を受けた時点の fields を取り込み、TEMPLATE を先頭から 1 文字ずつ out_* に出す。
-- 出し終わるまで busy。出力中の start は無視する。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.text_report_pkg.all;

entity text_report is
  generic (
    TEMPLATE : template_t
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    start     : in  std_logic;
    fields    : in  fields_t;
    busy      : out std_logic;
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of text_report is
  constant LAST : natural := TEMPLATE'length - 1;
  signal active  : std_logic := '0';
  signal index   : natural range 0 to LAST := 0;
  signal latched : fields_t := (others => (others => '0'));
  signal item    : item_t;
begin
  item <= TEMPLATE(TEMPLATE'low + index);

  process (all)
  begin
    if not item.is_hex then
      out_data <= std_logic_vector(to_unsigned(character'pos(item.char), 8));
    elsif item.high then
      out_data <= hex_char(latched(item.field)(7 downto 4));
    else
      out_data <= hex_char(latched(item.field)(3 downto 0));
    end if;
  end process;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        active <= '0';
        index  <= 0;
      elsif active = '0' then
        if start = '1' then
          latched <= fields;
          index   <= 0;
          active  <= '1';
        end if;
      elsif out_ready = '1' then
        if index = LAST then
          active <= '0';
        else
          index <= index + 1;
        end if;
      end if;
    end if;
  end process;

  out_valid <= active;
  busy      <= active;
end architecture;
