-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック uart（0x00000300, 64 バイト）: UART ブリッジ（CDC-ACM ⇄ ターゲット）の状態
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_uart is
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
    baud : in  std_logic_vector(31 downto 0);   -- baud（RO）
    frame_errors : in  std_logic_vector(31 downto 0);   -- frame_errors（RO）
    overruns : in  std_logic_vector(31 downto 0);   -- overruns（RO）
    flow_auto : out std_logic;   -- flow.auto（RW）
    flow_rts_claim : out std_logic;   -- flow.rts_claim（RW）
    flow_rts_pin : out std_logic_vector(1 downto 0);   -- flow.rts_pin（RW）
    flow_cts_claim : out std_logic;   -- flow.cts_claim（RW）
    flow_cts_pin : out std_logic_vector(1 downto 0);   -- flow.cts_pin（RW）
    lines_cts : in  std_logic;   -- lines.cts（RO）
    lines_rts : in  std_logic;   -- lines.rts（RO）
    lines_host_rts : in  std_logic;   -- lines.host_rts（RO）
    lines_host_break : in  std_logic   -- lines.host_break（RO）
  );
end entity;

architecture rtl of csr_uart is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal flow_auto_r : std_logic := '0';
  signal flow_rts_claim_r : std_logic := '0';
  signal flow_rts_pin_r : std_logic_vector(1 downto 0) := "00";
  signal flow_cts_claim_r : std_logic := '0';
  signal flow_cts_pin_r : std_logic_vector(1 downto 0) := "00";
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
        flow_auto_r <= '0';
        flow_rts_claim_r <= '0';
        flow_rts_pin_r <= "00";
        flow_cts_claim_r <= '0';
        flow_cts_pin_r <= "00";
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- baud
            rdata(31 downto 0) <= baud;
          when 1 =>   -- frame_errors
            rdata(31 downto 0) <= frame_errors;
          when 2 =>   -- overruns
            rdata(31 downto 0) <= overruns;
          when 3 =>   -- flow
            rdata(0) <= flow_auto_r;
            rdata(4) <= flow_rts_claim_r;
            rdata(9 downto 8) <= flow_rts_pin_r;
            rdata(12) <= flow_cts_claim_r;
            rdata(17 downto 16) <= flow_cts_pin_r;
            if bus_we = '1' then
              flow_auto_r <= bus_wdata(0);
              flow_rts_claim_r <= bus_wdata(4);
              flow_rts_pin_r <= bus_wdata(9 downto 8);
              flow_cts_claim_r <= bus_wdata(12);
              flow_cts_pin_r <= bus_wdata(17 downto 16);
            end if;
          when 4 =>   -- lines
            rdata(0) <= lines_cts;
            rdata(1) <= lines_rts;
            rdata(2) <= lines_host_rts;
            rdata(3) <= lines_host_break;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_ack   <= ack;
  bus_err   <= err;
  bus_rdata <= rdata;
  flow_auto <= flow_auto_r;
  flow_rts_claim <= flow_rts_claim_r;
  flow_rts_pin <= flow_rts_pin_r;
  flow_cts_claim <= flow_cts_claim_r;
  flow_cts_pin <= flow_cts_pin_r;
end architecture;
