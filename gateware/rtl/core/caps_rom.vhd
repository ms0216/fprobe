-- ケイパビリティ ROM（CSR バスのスレーブ）。内容は生成パッケージの CAPS_ROM をジェネリックで受ける。
-- 窓内の範囲外は 0 を返す。書き込みは無視する（RO と同じ扱い）。要求の次の周期に応答する。
-- 応答の周期以外の bus_rdata は 0（csr_bus_pkg の約束）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity caps_rom is
  generic (
    CONTENT     : csr_word_array_t;   -- 0 から始まること
    WINDOW_SIZE : positive            -- 窓のワード数（2 の冪）
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    bus_req   : in  std_logic;
    bus_addr  : in  csr_addr_t;
    bus_ack   : out std_logic;
    bus_rdata : out csr_word_t
  );
end entity;

architecture rtl of caps_rom is
  signal ack   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal index : natural range 0 to WINDOW_SIZE - 1;
begin
  assert CONTENT'low = 0 and CONTENT'length <= WINDOW_SIZE
    report "caps_rom: CONTENT must start at 0 and fit in the window" severity failure;

  index <= to_integer(bus_addr) mod WINDOW_SIZE;

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_bus_pkg の約束）
      if rst = '0' and bus_req = '1' and ack = '0' then
        ack <= '1';
        if index < CONTENT'length then
          rdata <= CONTENT(index);
        end if;
      end if;
    end if;
  end process;

  bus_ack   <= ack;
  bus_rdata <= rdata;
end architecture;
