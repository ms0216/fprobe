-- DUT のピン診断ブロック: PS（Linux）から、端子ごとに出力・入力を切り替えて読み戻す（実機の配線・端子の確認用）。
-- 端子 pin は双方向（外から見て、OE = 1 の端子は OUT を出し、0 の端子は Z）。IN は端子の今の値（2 段の同期）。
-- レジスタ（語番地）: 0 OE、1 OUT、2 IN（読み取り専用）、3 ID = 0x50494E01（"PIN" + 版 1）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity dut_pins is
  generic (
    N : positive := 8                      -- 端子の数（32 以下）
  );
  port (
    clk   : in    std_logic;
    rst   : in    std_logic;
    wr_en : in    std_logic;
    addr  : in    unsigned(1 downto 0);
    wdata : in    std_logic_vector(31 downto 0);
    rdata : out   std_logic_vector(31 downto 0);
    pin   : inout std_logic_vector(N - 1 downto 0)
  );
end entity;

architecture rtl of dut_pins is
  signal oe, out_r, sync1, sync2 : std_logic_vector(N - 1 downto 0);
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        oe    <= (others => '0');
        out_r <= (others => '0');
      elsif wr_en = '1' then
        if to_integer(addr) = 0 then
          oe <= wdata(N - 1 downto 0);
        elsif to_integer(addr) = 1 then
          out_r <= wdata(N - 1 downto 0);
        end if;
      end if;
      for i in 0 to N - 1 loop
        if pin(i) = '1' or pin(i) = 'H' then
          sync1(i) <= '1';
        else
          sync1(i) <= '0';            -- 0・L・Z（浮いた端子）・衝突は 0 に（シミュレーションで X を読み出さない）
        end if;
      end loop;
      sync2 <= sync1;
    end if;
  end process;

  drive : for i in 0 to N - 1 generate
    pin(i) <= out_r(i) when oe(i) = '1' else 'Z';
  end generate;

  process (all)
  begin
    rdata <= (others => '0');
    case to_integer(addr) is
      when 0      => rdata(N - 1 downto 0) <= oe;
      when 1      => rdata(N - 1 downto 0) <= out_r;
      when 2      => rdata(N - 1 downto 0) <= sync2;
      when others => rdata <= x"50494E01";
    end case;
  end process;
end architecture;
