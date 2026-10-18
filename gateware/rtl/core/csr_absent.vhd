-- 構成で外した機能の CSR の窓: どの読み書きにもバスエラーで答える（ホストは機能が無いと分かる。
-- 機能の有無はケイパビリティでも申告する）。
library ieee;
use ieee.std_logic_1164.all;

entity csr_absent is
  port (
    clk       : in  std_logic;
    bus_req   : in  std_logic;
    bus_ack   : out std_logic;
    bus_err   : out std_logic;
    bus_rdata : out std_logic_vector(31 downto 0)
  );
end entity;

architecture rtl of csr_absent is
  signal ack : std_logic := '0';
begin
  process (clk)
  begin
    if rising_edge(clk) then
      ack <= bus_req and not ack;
    end if;
  end process;
  bus_ack   <= ack;
  bus_err   <= ack;                            -- 応答の周期だけ（csr_bus_pkg の約束）
  bus_rdata <= (others => '0');
end architecture;
