-- 1 件の保持レジスタ（取りこぼしを数える）。来る側は待てない（valid だけ）、出す側はストリーム（valid / ready）。
--
-- enable の間に来た in_valid を 1 件保持して出す。出すのと同じ周期に次を受ける（連続する周期に来てよい）。保持が空く前に
-- 来たものは捨てて lost に数える（飽和）。lost は enable の 0→1 で 0 に戻す。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity hold_one is
  generic (
    WIDTH : positive
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    enable    : in  std_logic;
    in_valid  : in  std_logic;
    in_data   : in  std_logic_vector(WIDTH - 1 downto 0);
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(WIDTH - 1 downto 0);
    lost      : out unsigned(31 downto 0)
  );
end entity;

architecture rtl of hold_one is
  signal valid_r, en_prev : std_logic := '0';
  signal data_r : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal lost_r : unsigned(31 downto 0) := (others => '0');
  signal take : std_logic;
begin
  take <= not valid_r or out_ready;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        valid_r <= '0';
      elsif take = '1' then
        valid_r <= in_valid and enable;
        data_r  <= in_data;
      end if;
      if rst = '1' or (enable = '1' and en_prev = '0') then
        lost_r <= (others => '0');
      elsif in_valid = '1' and enable = '1' and take = '0' then
        lost_r <= sat_inc(lost_r);
      end if;
      en_prev <= enable;
    end if;
  end process;
  out_valid <= valid_r;
  out_data  <= data_r;
  lost      <= lost_r;
end architecture;
