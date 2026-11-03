-- ロジアナの入力を端子の ISERDESE2（Xilinx 7 系）で取り込み、sys の 1 周期に LANES 個のサンプル（レーン）にする包み。
-- 仕様: docs/superpowers/specs/2026-11-01-la-resolution-design.md §3.1・§3.2・§3.6（ベンダーの部品はここだけ）
--
-- CLK = clk_fast（BUFG）、CLKDIV = clk_div（= sys、BUFG）。両方とも同じ MMCM の出力（UG471 v1.10 p.153: NETWORKING で
-- 「CLK driven by MMCM or PLL, CLKDIV driven by CLKOUT[0:6] of same MMCM or PLL」、BUFG なら両方 BUFG）。
-- DDR（CLK の両方の辺で取る）なので clk_fast = clk_div × LANES / 2、サンプルの間隔 = clk_div の周期 / LANES。
-- 出力 q: ch c のレーン k（その clk_div の周期の k 番目のサンプル、k = 0 が最も古い）= q(k × CHANNELS + c)。
--   ISERDESE2 は最初に受けたビットを最も上の Q に出す（UG471 p.146）ので、レーン k = Q(LANES − k)。
-- 固定の遅れ: CLK の辺に番号 i を付け（clk_div の立ち上がり w と同時の辺が i = LANES × w）、辺 i で取ったサンプルを b(i) と
--   すると、clk_div の立ち上がり w の後の語のレーン k = b(LANES × w − 2 × LANES + 4 + k)（最も新しいレーンは LANES − 3 辺前、
--   最も古いレーンは 2 × LANES − 4 辺前）。Vivado の UNISIM の記録（tests/data/iserdes_ref_w*.txt）と同じで、
--   pynq_z2_pkg.iserdes_newest_age がこの LANES − 3 を返す（試験 tests/tb/xilinx_la_iserdes_tb.py が模型で確かめる）。
-- rst: clk_div に同期（UG953: ISERDESE2 の RST は CLKDIV の領域の同期リセット、CLKDIV 1 周期以上）。
-- 端子からの経路は非同期の入力（Vivado は端子から ISERDESE2 までの時間を測らない。制約は置いていない）。
-- 出力にも使う端子（IOBUF）にも置ける: 端子ごとに ILOGIC（ISERDESE2）と OLOGIC は別の部品の組（UG471 v1.10 p.15
-- 「Each IOB has a direct connection to an ILOGIC/OLOGIC pair」）で、ファブリックからの出力・3 状態の制御は OLOGIC の
-- 組み合わせの経路を通る（p.127）。OFB（OSERDESE2 からの折り返し）は使わない（使うと外の入力を取れない。p.156）。
library ieee;
use ieee.std_logic_1164.all;

entity xilinx_la_iserdes is
  generic (
    CHANNELS : positive := 6;
    LANES    : positive := 6          -- DDR の DATA_WIDTH（6 か 8）
  );
  port (
    clk_fast : in  std_logic;
    clk_div  : in  std_logic;
    rst      : in  std_logic;
    pins     : in  std_logic_vector(CHANNELS - 1 downto 0);
    q        : out std_logic_vector(LANES * CHANNELS - 1 downto 0);  -- レーン k の ch c = q(k × CHANNELS + c)
    -- 端子の値そのもの（ISERDESE2 の組み合わせの出力 O = D。UG471 v1.10 p.147）。ISERDESE2 を置いた端子の入力は IBUF から
    -- ISERDESE2 の D にだけつながるので、ファブリックで端子を読む回路（DAP の SWDIO・UART RX 等）はこれを使う
    o        : out std_logic_vector(CHANNELS - 1 downto 0)
  );
end entity;

architecture xilinx of xilinx_la_iserdes is
  -- 宣言は Vivado の unisim と同じ名前・口（使う口だけ。GHDL では tests/rtl/sim_xilinx_prims.vhd の模型に結び付く）
  component ISERDESE2
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
  end component;
begin
  assert LANES = 6 or LANES = 8 report "xilinx_la_iserdes: LANES must be 6 or 8 (DDR)" severity failure;

  ch_g : for c in 0 to CHANNELS - 1 generate
    signal qs : std_logic_vector(1 to 8);
  begin
    serdes_i : ISERDESE2
      generic map (DATA_RATE => "DDR", DATA_WIDTH => LANES, INTERFACE_TYPE => "NETWORKING", IOBDELAY => "NONE",
                   NUM_CE => 1, OFB_USED => "FALSE", SERDES_MODE => "MASTER",
                   DYN_CLKDIV_INV_EN => "FALSE", DYN_CLK_INV_EN => "FALSE",
                   IS_CLKB_INVERTED => '1')          -- CLKB = CLK の反転（UG471 p.148）を端子の中の反転で作る
      port map (O => o(c), Q1 => qs(1), Q2 => qs(2), Q3 => qs(3), Q4 => qs(4), Q5 => qs(5), Q6 => qs(6), Q7 => qs(7),
                Q8 => qs(8), SHIFTOUT1 => open, SHIFTOUT2 => open,
                BITSLIP => '0', CE1 => '1', CE2 => '1', CLK => clk_fast, CLKB => clk_fast, CLKDIV => clk_div,
                CLKDIVP => '0', D => pins(c), DDLY => '0', DYNCLKDIVSEL => '0', DYNCLKSEL => '0',
                OCLK => '0', OCLKB => '0', OFB => '0', RST => rst, SHIFTIN1 => '0', SHIFTIN2 => '0');

    lane_g : for k in 0 to LANES - 1 generate
      q(k * CHANNELS + c) <= qs(LANES - k);
    end generate;
  end generate;
end architecture;
