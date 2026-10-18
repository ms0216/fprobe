-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック gpio（0x00000200, 64 バイト）: 拡張ヘッダ（Primer 20K は J12）の 8 本の汎用入出力。配線確認・ビットバング用
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_gpio is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    bus_req   : in  std_logic;
    bus_we    : in  std_logic;
    bus_addr  : in  csr_addr_t;
    bus_wdata : in  csr_word_t;
    bus_ack   : out std_logic;
    bus_err   : out std_logic;
    bus_rdata : out csr_word_t;
    drive : out std_logic_vector(7 downto 0);   -- drive（RW）
    enable : out std_logic_vector(7 downto 0);   -- enable（RW）
    level : in  std_logic_vector(7 downto 0);   -- level（RO）
    io : in  std_logic_vector(7 downto 0)   -- io（RO）
  );
end entity;

architecture rtl of csr_gpio is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal drive_r : std_logic_vector(7 downto 0) := "00000000";
  signal enable_r : std_logic_vector(7 downto 0) := "00000000";
begin
  start <= bus_req = '1' and ack = '0';
  index <= to_integer(bus_addr(3 downto 0));

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      err   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）
      if rst = '1' then
        drive_r <= "00000000";
        enable_r <= "00000000";
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- drive
            rdata(7 downto 0) <= drive_r;
            if bus_we = '1' then
              drive_r <= bus_wdata(7 downto 0);
            end if;
          when 1 =>   -- enable
            rdata(7 downto 0) <= enable_r;
            if bus_we = '1' then
              enable_r <= bus_wdata(7 downto 0);
            end if;
          when 2 =>   -- level
            rdata(7 downto 0) <= level;
          when 3 =>   -- io
            rdata(7 downto 0) <= io;
            if bus_we = '1' then
              drive_r <= bus_wdata(7 downto 0);
            end if;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_ack   <= ack;
  bus_err   <= err;
  bus_rdata <= rdata;
  drive <= drive_r;
  enable <= enable_r;
end architecture;
