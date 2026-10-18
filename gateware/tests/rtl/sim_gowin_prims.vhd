-- シミュレーション専用: Gowin プリミティブの振る舞いモデル（合成には使わない）。
-- rPLL: clkout は clkin をそのまま出し、数クロック後に lock を立てる。
-- 周波数の変換は再現しない（テスト側は clkout の周期を基準に時間を数える）。
library ieee;
use ieee.std_logic_1164.all;

entity rPLL is
  generic (
    FCLKIN : string := "100.0"; DEVICE : string := "GW2A-18C";
    DYN_IDIV_SEL : string := "false"; IDIV_SEL : integer := 0;
    DYN_FBDIV_SEL : string := "false"; FBDIV_SEL : integer := 0;
    DYN_ODIV_SEL : string := "false"; ODIV_SEL : integer := 8;
    PSDA_SEL : string := "0000"; DYN_DA_EN : string := "false"; DUTYDA_SEL : string := "1000";
    CLKOUT_FT_DIR : bit := '1'; CLKOUTP_FT_DIR : bit := '1';
    CLKOUT_DLY_STEP : integer := 0; CLKOUTP_DLY_STEP : integer := 0;
    CLKOUTD3_SRC : string := "CLKOUT"; CLKFB_SEL : string := "internal";
    CLKOUT_BYPASS : string := "false"; CLKOUTP_BYPASS : string := "false";
    CLKOUTD_BYPASS : string := "false"; CLKOUTD_SRC : string := "CLKOUT";
    DYN_SDIV_SEL : integer := 2
  );
  port (
    CLKIN : in std_logic; CLKFB : in std_logic := '0';
    IDSEL, FBDSEL, ODSEL : in std_logic_vector(5 downto 0);
    RESET, RESET_P : in std_logic := '0';
    PSDA, FDLY, DUTYDA : in std_logic_vector(3 downto 0);
    LOCK : out std_logic; CLKOUT : out std_logic;
    CLKOUTD : out std_logic; CLKOUTP : out std_logic; CLKOUTD3 : out std_logic
  );
end entity;

architecture behavioural of rPLL is
  signal count : natural := 0;
begin
  CLKOUT <= CLKIN; CLKOUTP <= CLKIN; CLKOUTD <= CLKIN; CLKOUTD3 <= CLKIN;
  process (CLKIN)
  begin
    if rising_edge(CLKIN) then
      if count < 8 then
        count <= count + 1;
      end if;
    end if;
  end process;
  LOCK <= '1' when count = 8 else '0';
end architecture;
