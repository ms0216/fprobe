-- DUT のコア: サンプル生成 + FIFO + 不具合の仕込み（docs/superpowers/specs/2026-10-24-zynq-dut-environment-design.md）。
-- Linux のアプリが AXI-Lite 越しに FIFO を読み、PL の注目信号 dbg を fprobe のロジアナが見る題材。
--
-- レジスタ（語番地。host/pynq/dut/dut_regs.h と同じ。ID 以外は reg_addr で選ぶ）:
--   0 CTRL       bit0 = 生成の有効
--   1 PERIOD     生成の間隔（クロック数、既定 1000）
--   2 DATA       読むと FIFO の先頭を 1 語取り出す。空なら 0xFFFFFFFF
--   3 LEVEL      FIFO の語数（読み取り専用）
--   4 OVERFLOWS  満杯で捨てた数（読み取り専用、読んでもクリアしない）
--   5 GENERATED  生成した数（不具合で捨てた分も含む）
--   6 FAULT      bit0 = 有効、bits[31:8] = N。有効なら N 個に 1 個の生成を黙って捨てる（通し番号は進む）
--   7 ID         0x44555401（"DUT" + 版 1）
-- reg_rdata は reg_addr に対する組合せ。reg_rd_en が立つ周期の値が読んだ値で、DATA はその周期の終わりに取り出される。
-- dbg（注目信号、レジスタ出力）: 0 生成の拍、1 FIFO 満杯、2 溢れの拍、3 DATA の読みの拍、4 不具合で捨てた拍、5 生成が有効。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity dut_stream_core is
  generic (
    FIFO_DEPTH : positive := 256   -- 2 の冪
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    reg_wr_en : in  std_logic;
    reg_addr  : in  unsigned(3 downto 0);
    reg_wdata : in  std_logic_vector(31 downto 0);
    reg_rd_en : in  std_logic;
    reg_rdata : out std_logic_vector(31 downto 0);
    dbg       : out std_logic_vector(5 downto 0)
  );
end entity;

architecture rtl of dut_stream_core is
  constant A_CTRL      : natural := 0;
  constant A_PERIOD    : natural := 1;
  constant A_DATA      : natural := 2;
  constant A_LEVEL     : natural := 3;
  constant A_OVERFLOWS : natural := 4;
  constant A_GENERATED : natural := 5;
  constant A_FAULT     : natural := 6;
  constant A_ID        : natural := 7;
  constant ID_VALUE    : std_logic_vector(31 downto 0) := x"44555401";

  signal enabled    : std_logic;
  signal period     : unsigned(31 downto 0);
  signal fault_reg  : std_logic_vector(31 downto 0);
  signal count      : unsigned(31 downto 0);
  signal generated  : unsigned(31 downto 0);
  signal overflows  : unsigned(31 downto 0);
  signal fault_cnt  : unsigned(23 downto 0);

  signal tick       : std_logic;
  signal drop       : std_logic;
  signal push       : std_logic;
  signal overflow   : std_logic;
  signal rd_data    : std_logic;
  signal fifo_ready : std_logic;
  signal out_valid  : std_logic;
  signal out_data   : std_logic_vector(31 downto 0);
  signal level      : unsigned(31 downto 0);
  signal dbg_i      : std_logic_vector(5 downto 0);
  constant STRETCH_CLOCKS : natural := 4;
  type stretch_t is array (0 to 5) of natural range 0 to STRETCH_CLOCKS;
  signal stretch    : stretch_t;
begin
  tick     <= '1' when enabled = '1' and count = 0 else '0';
  drop     <= '1' when tick = '1' and fault_reg(0) = '1' and unsigned(fault_reg(31 downto 8)) /= 0
                       and fault_cnt >= unsigned(fault_reg(31 downto 8)) - 1 else '0';
  push     <= tick and not drop;
  overflow <= push and not fifo_ready;
  rd_data  <= '1' when reg_rd_en = '1' and to_integer(reg_addr) = A_DATA else '0';

  fifo_i : entity work.sync_fifo
    generic map (WIDTH => 32, DEPTH => FIFO_DEPTH)
    port map (clk => clk, rst => rst,
              in_valid => push, in_ready => fifo_ready, in_data => std_logic_vector(generated),
              out_valid => out_valid, out_ready => rd_data, out_data => out_data, level => level);

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        enabled   <= '0';
        period    <= to_unsigned(1000, 32);
        fault_reg <= (others => '0');
        count     <= (others => '0');
        generated <= (others => '0');
        overflows <= (others => '0');
        fault_cnt <= (others => '0');
      else
        if reg_wr_en = '1' then
          case to_integer(reg_addr) is
            when A_CTRL   => enabled <= reg_wdata(0);
            when A_PERIOD => period <= unsigned(reg_wdata);
            when A_FAULT  => fault_reg <= reg_wdata;
            when others   => null;
          end case;
        end if;
        if enabled = '0' then
          count <= (others => '0');
        elsif count = 0 then
          if period > 1 then
            count <= period - 1;
          end if;
        else
          count <= count - 1;
        end if;
        if tick = '1' then
          generated <= generated + 1;
        end if;
        -- 数えるのは FAULT 有効の間だけ。FAULT の書き込みは同じクロックの拍より優先してカウンタを戻す（重なると負けて、
        -- 新しい N に届かないまま長く働かなかった。PERIOD = 1 では毎回重なる）
        if reg_wr_en = '1' and to_integer(reg_addr) = A_FAULT then
          fault_cnt <= (others => '0');
        elsif tick = '1' and fault_reg(0) = '1' then
          if drop = '1' then
            fault_cnt <= (others => '0');
          else
            fault_cnt <= fault_cnt + 1;
          end if;
        end if;
        if overflow = '1' then
          overflows <= overflows + 1;
        end if;
      end if;
    end if;
  end process;

  process (all)
  begin
    case to_integer(reg_addr) is
      when A_CTRL      => reg_rdata <= (0 => enabled, others => '0');
      when A_PERIOD    => reg_rdata <= std_logic_vector(period);
      when A_DATA      => reg_rdata <= out_data when out_valid = '1' else (others => '1');
      when A_LEVEL     => reg_rdata <= std_logic_vector(level);
      when A_OVERFLOWS => reg_rdata <= std_logic_vector(overflows);
      when A_GENERATED => reg_rdata <= std_logic_vector(generated);
      when A_FAULT     => reg_rdata <= fault_reg;
      when A_ID        => reg_rdata <= ID_VALUE;
      when others      => reg_rdata <= (others => '0');
    end case;
  end process;

  dbg_i <= enabled & drop & rd_data & overflow & (not fifo_ready) & tick;

  -- 拍（0 生成・2 溢れ・3 読み・4 捨て）は STRETCH_CLOCKS クロックに伸ばす。1 クロック（10 ns）の脈はロジアナの標本（約 100.29 MS/s）の
  -- 間に落ちて約 1 割が見えなかった（実機 2026-10-25）。1 と 5 はレベルなのでそのまま。
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        dbg <= (others => '0');
        for i in stretch'range loop
          stretch(i) <= 0;
        end loop;
      else
        for i in 0 to 5 loop
          if i = 1 or i = 5 then
            dbg(i) <= dbg_i(i);
          elsif dbg_i(i) = '1' then
            dbg(i) <= '1';
            stretch(i) <= STRETCH_CLOCKS - 1;
          elsif stretch(i) > 0 then
            dbg(i) <= '1';
            stretch(i) <= stretch(i) - 1;
          else
            dbg(i) <= '0';
          end if;
        end loop;
      end if;
    end if;
  end process;
end architecture;
