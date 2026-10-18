-- Gowin rPLL（GW2A）の固定分周比ラッパ。
-- 分周比は rPLL のパラメータ値（*_SEL）で受け取る。値はツール側の PLL ソルバで求め、
-- ボード定数パッケージ経由で渡す（直接の数値指定はしない）。
library ieee;
use ieee.std_logic_1164.all;

entity gowin_rpll is
  generic (
    FCLKIN_MHZ : string;     -- 入力周波数（MHz, 例 "27"）
    DEVICE     : string;     -- 例 "GW2A-18C"
    IDIV_SEL   : natural;
    FBDIV_SEL  : natural;
    ODIV_SEL   : natural
  );
  port (
    clkin  : in  std_logic;
    clkout : out std_logic;
    lock   : out std_logic
  );
end entity;

architecture gowin of gowin_rpll is
  -- 宣言は Gowin 同梱の simlib/gw2a/prim_syn.vhd と同一
  component rPLL
    generic (
      FCLKIN : string := "100.0"; DEVICE : string := "GW2A-55";
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
      IDSEL : in std_logic_vector(5 downto 0);
      FBDSEL : in std_logic_vector(5 downto 0);
      ODSEL : in std_logic_vector(5 downto 0);
      RESET : in std_logic := '0'; RESET_P : in std_logic := '0';
      PSDA, FDLY : in std_logic_vector(3 downto 0);
      DUTYDA : in std_logic_vector(3 downto 0);
      LOCK : out std_logic; CLKOUT : out std_logic;
      CLKOUTD : out std_logic; CLKOUTP : out std_logic; CLKOUTD3 : out std_logic
    );
  end component;
begin
  pll_i : rPLL
    generic map (
      FCLKIN => FCLKIN_MHZ, DEVICE => DEVICE,
      DYN_IDIV_SEL => "false", IDIV_SEL => IDIV_SEL,
      DYN_FBDIV_SEL => "false", FBDIV_SEL => FBDIV_SEL,
      DYN_ODIV_SEL => "false", ODIV_SEL => ODIV_SEL,
      -- 以下は Gowin IP 生成器の既定値（Sipeed の実機実績例と同じ）
      PSDA_SEL => "0000", DYN_DA_EN => "true", DUTYDA_SEL => "1000",
      CLKOUT_FT_DIR => '1', CLKOUTP_FT_DIR => '1',
      CLKOUT_DLY_STEP => 0, CLKOUTP_DLY_STEP => 0,
      CLKOUTD3_SRC => "CLKOUT", CLKFB_SEL => "internal",
      CLKOUT_BYPASS => "false", CLKOUTP_BYPASS => "false", CLKOUTD_BYPASS => "false",
      CLKOUTD_SRC => "CLKOUT", DYN_SDIV_SEL => 2)
    port map (
      CLKIN => clkin, CLKFB => '0',
      IDSEL => (others => '0'), FBDSEL => (others => '0'), ODSEL => (others => '0'),
      RESET => '0', RESET_P => '0',
      PSDA => (others => '0'), FDLY => (others => '0'), DUTYDA => (others => '0'),
      LOCK => lock, CLKOUT => clkout,
      CLKOUTD => open, CLKOUTP => open, CLKOUTD3 => open);
end architecture;
