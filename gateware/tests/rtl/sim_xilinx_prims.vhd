-- GHDL 用の Xilinx のプリミティブの振る舞いの模型（sim_gowin_prims.vhd と同じ考え。合成には使わない）。
-- MMCME2_BASE: 入力の周期 × DIVCLK × OUT_DIV / MULT の周期のクロックを出し、入力の 16 周期後に LOCKED を立てる。
-- CLKOUT1 は同じ VCO の CLKOUT1_DIVIDE 分周で、CLKOUT0 の立ち上がりごとに頭がそろう（実物と同じ）。
library ieee;
use ieee.std_logic_1164.all;

entity MMCME2_BASE is
  generic (
    CLKFBOUT_MULT_F  : real := 5.0;
    CLKIN1_PERIOD    : real := 0.0;
    CLKOUT0_DIVIDE_F : real := 1.0;
    CLKOUT1_DIVIDE   : integer := 1;
    DIVCLK_DIVIDE    : integer := 1
  );
  port (
    CLKIN1   : in  std_logic;
    CLKFBIN  : in  std_logic;
    RST      : in  std_logic;
    PWRDWN   : in  std_logic;
    CLKFBOUT : out std_logic;
    CLKOUT0  : out std_logic;
    CLKOUT1  : out std_logic;
    LOCKED   : out std_logic
  );
end entity;

architecture model of MMCME2_BASE is
  constant HALF : time := CLKIN1_PERIOD * real(DIVCLK_DIVIDE) * CLKOUT0_DIVIDE_F / CLKFBOUT_MULT_F / 2.0 * 1 ns;
  constant HALF1 : time := CLKIN1_PERIOD * real(DIVCLK_DIVIDE) * real(CLKOUT1_DIVIDE) / CLKFBOUT_MULT_F / 2.0 * 1 ns;
  constant RATIO1 : natural := integer(CLKOUT0_DIVIDE_F) / CLKOUT1_DIVIDE;   -- CLKOUT1 / CLKOUT0（整数比のときだけ使う）
  signal clk : std_logic := '0';
  signal clk1 : std_logic := '0';
  signal edges : natural := 0;
begin
  clk <= not clk after HALF;
  CLKOUT0 <= clk;
  CLKOUT1 <= clk1;

  -- CLKOUT0 の立ち上がりごとに、RATIO1 周期の CLKOUT1 を出す（丸めの誤差を持ち越さない）
  process
  begin
    wait until rising_edge(clk);
    for i in 1 to RATIO1 loop
      clk1 <= '1';
      wait for HALF1;
      clk1 <= '0';
      if i < RATIO1 then
        wait for HALF1;
      end if;
    end loop;
  end process;
  CLKFBOUT <= CLKFBIN;

  process (CLKIN1)
  begin
    if rising_edge(CLKIN1) and edges < 16 then
      edges <= edges + 1;
    end if;
  end process;
  LOCKED <= '1' when edges = 16 and RST = '0' else '0';
end architecture;

-- ISERDESE2: xilinx_la_iserdes が使う形（INTERFACE_TYPE = NETWORKING、DATA_RATE = DDR、DATA_WIDTH = 6・8、IOBDELAY = NONE、
-- NUM_CE = 1、BITSLIP なし）だけの振る舞いの模型。CLK と CLKB（= CLK の反転。IS_CLKB_INVERTED = '1' なら CLK をそのまま
-- つなぐ）の立ち上がり、つまり CLK の両方の辺で D を取り、CLKDIV の立ち上がりで Q1〜Q<DATA_WIDTH> を出す。
-- 並び（UG471 v1.10 p.146「The first data bit received appears on the highest order Q output」）と、どの辺のサンプルが
-- どの CLKDIV の周期の語に入るかは、Vivado 2025.2 の UNISIM の ISERDESE2 を xsim で動かした記録
-- （tests/data/iserdes_ref_w6.txt・_w8.txt、tests/xsim/run_iserdes_ref.py）に合わせてある:
--   CLK の辺の番号 i（CLKDIV の立ち上がり w に一致する辺が i = DATA_WIDTH × w）のサンプルを b(i) とすると、
--   立ち上がり w の後の Qj = b(DATA_WIDTH × w − (j + DATA_WIDTH − 4))。
-- tests/test_xilinx_la_iserdes.py がこの模型と記録の一致を確かめる。RST（CLKDIV に同期）の間は Q = 0。
library ieee;
use ieee.std_logic_1164.all;

entity ISERDESE2 is
  generic (
    DATA_RATE         : string := "DDR";
    DATA_WIDTH        : integer := 4;
    DYN_CLKDIV_INV_EN : string := "FALSE";
    DYN_CLK_INV_EN    : string := "FALSE";
    INTERFACE_TYPE    : string := "MEMORY";
    IOBDELAY          : string := "NONE";
    IS_CLKB_INVERTED  : bit := '0';
    NUM_CE            : integer := 2;
    OFB_USED          : string := "FALSE";
    SERDES_MODE       : string := "MASTER"
  );
  port (
    O            : out std_logic;
    Q1, Q2, Q3, Q4, Q5, Q6, Q7, Q8 : out std_logic;
    SHIFTOUT1    : out std_logic;
    SHIFTOUT2    : out std_logic;
    BITSLIP      : in  std_logic;
    CE1          : in  std_logic;
    CE2          : in  std_logic;
    CLK          : in  std_logic;
    CLKB         : in  std_logic;
    CLKDIV       : in  std_logic;
    CLKDIVP      : in  std_logic;
    D            : in  std_logic;
    DDLY         : in  std_logic;
    DYNCLKDIVSEL : in  std_logic;
    DYNCLKSEL    : in  std_logic;
    OCLK         : in  std_logic;
    OCLKB        : in  std_logic;
    OFB          : in  std_logic;
    RST          : in  std_logic;
    SHIFTIN1     : in  std_logic;
    SHIFTIN2     : in  std_logic
  );
end entity;

architecture model of ISERDESE2 is
  constant HIST_LEN : natural := 2 * DATA_WIDTH;
  signal clkb_eff : std_logic;
  signal hist : std_logic_vector(0 to HIST_LEN - 1) := (others => '0');   -- hist(m) = 直前の辺から m 個前の辺のサンプル
  signal q : std_logic_vector(1 to 8) := (others => '0');
begin
  assert INTERFACE_TYPE = "NETWORKING" and DATA_RATE = "DDR" and (DATA_WIDTH = 6 or DATA_WIDTH = 8)
         and IOBDELAY = "NONE" and NUM_CE = 1 and OFB_USED = "FALSE" and SERDES_MODE = "MASTER"
    report "ISERDESE2 model: only NETWORKING DDR 6/8, IOBDELAY NONE, NUM_CE 1, MASTER" severity failure;

  clkb_eff <= CLKB when IS_CLKB_INVERTED = '0' else not CLKB;

  process (CLK, clkb_eff)
  begin
    if rising_edge(CLK) or rising_edge(clkb_eff) then
      hist <= D & hist(0 to HIST_LEN - 2);
    end if;
  end process;

  -- CLKDIV の立ち上がり w は CLK の辺 DATA_WIDTH × w と同時。そのときの hist(0) は辺 DATA_WIDTH × w − 1 のサンプル
  process (CLKDIV)
  begin
    if rising_edge(CLKDIV) then
      for j in 1 to 8 loop
        if RST = '1' or j > DATA_WIDTH then
          q(j) <= '0';
        else
          q(j) <= hist(j + DATA_WIDTH - 5);
        end if;
      end loop;
    end if;
  end process;

  Q1 <= q(1); Q2 <= q(2); Q3 <= q(3); Q4 <= q(4); Q5 <= q(5); Q6 <= q(6); Q7 <= q(7); Q8 <= q(8);
  O <= D;
  SHIFTOUT1 <= '0';
  SHIFTOUT2 <= '0';
end architecture;

library ieee;
use ieee.std_logic_1164.all;

entity BUFG is
  port (I : in std_logic; O : out std_logic);
end entity;

architecture model of BUFG is
begin
  O <= I;
end architecture;

-- ODDR（SAME_EDGE だけ）: 立ち上がりで D1・D2 を取り、C = 1 の間 D1、C = 0 の間 D2 を出す（UG471 v1.10 p.130）
library ieee;
use ieee.std_logic_1164.all;

entity ODDR is
  generic (
    DDR_CLK_EDGE : string := "OPPOSITE_EDGE";
    INIT         : bit := '0';
    SRTYPE       : string := "SYNC"
  );
  port (
    Q  : out std_logic;
    C  : in  std_logic;
    CE : in  std_logic;
    D1 : in  std_logic;
    D2 : in  std_logic;
    R  : in  std_logic;
    S  : in  std_logic
  );
end entity;

architecture model of ODDR is
  signal r1, r2 : std_logic := to_x01(INIT);
begin
  assert DDR_CLK_EDGE = "SAME_EDGE" report "ODDR model: SAME_EDGE only" severity failure;
  process (C)
  begin
    if rising_edge(C) then
      if R = '1' then
        r1 <= '0';
        r2 <= '0';
      elsif S = '1' then
        r1 <= '1';
        r2 <= '1';
      elsif CE = '1' then
        r1 <= D1;
        r2 <= D2;
      end if;
    end if;
  end process;
  Q <= r1 when C = '1' else r2;
end architecture;
