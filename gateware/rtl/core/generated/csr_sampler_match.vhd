-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック sampler_match（0x00000C40, 64 バイト）: サンプラの値の一致（R21。docs/superpowers/specs/2026-10-23-sampler-match-trigger-design.md）。サンプラの標本（ACK OK・番地が addr）が (値 xor value) and mask = 0 に入った瞬間（前の標本が不一致）にクロストリガの源 bit5。構成によって無い（FEATURES bit10。無ければ読むとエラー）
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_sampler_match is
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
    addr : out std_logic_vector(31 downto 0);   -- addr（RW）
    mask : out std_logic_vector(31 downto 0);   -- mask（RW）
    value : out std_logic_vector(31 downto 0);   -- value（RW）
    control_enable : out std_logic;   -- control.enable（RW）
    control_matched : in  std_logic;   -- control.matched（RO）
    control_seen_any : in  std_logic;   -- control.seen_any（RO）
    seen : in  std_logic_vector(31 downto 0);   -- seen（RO）
    count : in  std_logic_vector(31 downto 0);   -- count（RO）
    time_lo : in  std_logic_vector(31 downto 0);   -- time_lo（RO）
    time_hi : in  std_logic_vector(31 downto 0);   -- time_hi（RO）
    prev_lo : in  std_logic_vector(31 downto 0);   -- prev_lo（RO）
    prev_hi : in  std_logic_vector(31 downto 0)   -- prev_hi（RO）
  );
end entity;

architecture rtl of csr_sampler_match is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal addr_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal mask_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal value_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal control_enable_r : std_logic := '0';
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
        addr_r <= "00000000000000000000000000000000";
        mask_r <= "00000000000000000000000000000000";
        value_r <= "00000000000000000000000000000000";
        control_enable_r <= '0';
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- addr
            rdata(31 downto 0) <= addr_r;
            if bus_we = '1' then
              addr_r <= bus_wdata(31 downto 0);
            end if;
          when 1 =>   -- mask
            rdata(31 downto 0) <= mask_r;
            if bus_we = '1' then
              mask_r <= bus_wdata(31 downto 0);
            end if;
          when 2 =>   -- value
            rdata(31 downto 0) <= value_r;
            if bus_we = '1' then
              value_r <= bus_wdata(31 downto 0);
            end if;
          when 3 =>   -- control
            rdata(0) <= control_enable_r;
            rdata(8) <= control_matched;
            rdata(9) <= control_seen_any;
            if bus_we = '1' then
              control_enable_r <= bus_wdata(0);
            end if;
          when 4 =>   -- seen
            rdata(31 downto 0) <= seen;
          when 5 =>   -- count
            rdata(31 downto 0) <= count;
          when 6 =>   -- time_lo
            rdata(31 downto 0) <= time_lo;
          when 7 =>   -- time_hi
            rdata(31 downto 0) <= time_hi;
          when 8 =>   -- prev_lo
            rdata(31 downto 0) <= prev_lo;
          when 9 =>   -- prev_hi
            rdata(31 downto 0) <= prev_hi;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_ack   <= ack;
  bus_err   <= err;
  bus_rdata <= rdata;
  addr <= addr_r;
  mask <= mask_r;
  value <= value_r;
  control_enable <= control_enable_r;
end architecture;
