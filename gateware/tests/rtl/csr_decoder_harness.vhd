-- テスト専用: csr_decoder + 2 つのスレーブ（スレーブはデコーダが配る番地・書き込み値・書き込みの印を使う）。
--   窓 0（ワード 0..3）: 4 ワードのレジスタ。次の周期に応答
--   窓 1（ワード 16..31）: 3 周期待ってから応答。読み値は 0xA000_0000 + ワードアドレス。最後のワードはエラーを返す
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_decoder_harness is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    bus_req   : in  std_logic;
    bus_we    : in  std_logic;
    bus_addr  : in  csr_addr_t;
    bus_wdata : in  csr_word_t;
    bus_ack   : out std_logic;
    bus_err   : out std_logic;
    bus_rdata : out csr_word_t
  );
end entity;

architecture rtl of csr_decoder_harness is
  constant WINDOWS : csr_windows_t := ((base => 0, size => 4), (base => 16, size => 16));
  signal s_req, s_ack, s_err : std_logic_vector(0 to 1) := (others => '0');
  signal s_rdata : csr_word_array_t(0 to 1) := (others => (others => '0'));
  signal s_rsp   : csr_rsp_array_t(0 to 1);
  signal s_cmd   : csr_cmd_t;
  signal regs    : csr_word_array_t(0 to 3) := (others => (others => '0'));
  signal wait_n  : natural range 0 to 3 := 0;
  signal c_we    : std_logic;                     -- デコーダが窓へ配る値（レジスタを通した写し）
  signal c_addr  : csr_addr_t;
  signal c_wdata : csr_word_t;
begin
  decoder_i : entity work.csr_decoder
    generic map (WINDOWS => WINDOWS)
    port map (clk => clk, rst => rst, bus_req => bus_req, bus_we => bus_we, bus_addr => bus_addr, bus_wdata => bus_wdata,
              bus_ack => bus_ack, bus_err => bus_err, bus_rdata => bus_rdata,
              s_cmd => s_cmd, s_req => s_req, s_rsp => s_rsp);
  c_we    <= s_cmd.we;
  c_addr  <= s_cmd.addr;
  c_wdata <= s_cmd.wdata;
  rsp_g : for i in 0 to 1 generate
    s_rsp(i) <= (ack => s_ack(i), err => s_err(i), rdata => s_rdata(i));
  end generate;

  process (clk)
    variable index : natural range 0 to 3;
  begin
    if rising_edge(clk) then
      s_ack(0)   <= '0';
      s_rdata    <= (others => (others => '0'));   -- 応答の周期以外は 0（csr_bus_pkg の約束）
      s_err(1)   <= '0';
      index      := to_integer(c_addr(1 downto 0));
      if s_req(0) = '1' and s_ack(0) = '0' then
        s_ack(0) <= '1';
        if c_we = '1' then
          regs(index) <= c_wdata;
        end if;
        s_rdata(0) <= regs(index);
      end if;

      s_ack(1) <= '0';
      if s_req(1) = '1' and s_ack(1) = '0' then
        if wait_n = 3 then
          wait_n     <= 0;
          s_ack(1)   <= '1';
          s_err(1)   <= '1' when c_addr(3 downto 0) = "1111" else '0';
          s_rdata(1) <= std_logic_vector(x"A0000000" + resize(c_addr, 32));
        else
          wait_n <= wait_n + 1;
        end if;
      end if;
    end if;
  end process;
end architecture;
