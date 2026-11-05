-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック sampler_match（0x00000C40, 64 バイト）: サンプラの値の一致（R21。docs/superpowers/specs/2026-10-23-sampler-match-trigger-design.md）。サンプラの標本（ACK OK・番地が addr）が (値 xor value) and mask = 0 に入った瞬間（前の標本が不一致）にクロストリガの源 bit5。構成によって無い（FEATURES bit10。無ければ読むとエラー）
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rsp の rdata・err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_sampler_match is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    bus_req   : in  std_logic;
    bus_cmd   : in  csr_cmd_t;
    bus_rsp : out csr_rsp_t;
    addr : out std_logic_vector(31 downto 0);   -- addr（RW）
    mask : out std_logic_vector(31 downto 0);   -- mask（RW）
    value : out std_logic_vector(31 downto 0);   -- value（RW）
    control_enable : out std_logic;   -- control.enable（RW）
    control_once : out std_logic;   -- control.once（RW）
    control_after_start : out std_logic;   -- control.after_start（RW）
    control_first_sample : out std_logic;   -- control.first_sample（RW）
    control_matched : in  std_logic;   -- control.matched（RO）
    control_seen_any : in  std_logic;   -- control.seen_any（RO）
    control_started : in  std_logic;   -- control.started（RO）
    seen : in  std_logic_vector(31 downto 0);   -- seen（RO）
    count : in  std_logic_vector(31 downto 0);   -- count（RO）
    time_lo : in  std_logic_vector(31 downto 0);   -- time_lo（RO）
    time_hi : in  std_logic_vector(31 downto 0);   -- time_hi（RO）
    prev_lo : in  std_logic_vector(31 downto 0);   -- prev_lo（RO）
    prev_hi : in  std_logic_vector(31 downto 0);   -- prev_hi（RO）
    addr_mask : out std_logic_vector(31 downto 0);   -- addr_mask（RW）
    fire_index : in  std_logic_vector(6 downto 0);   -- fire.index（RO）
    fire_first : in  std_logic;   -- fire.first（RO）
    unmatched0 : in  std_logic_vector(31 downto 0);   -- unmatched0（RO）
    unmatched1 : in  std_logic_vector(31 downto 0);   -- unmatched1（RO）
    unmatched2 : in  std_logic_vector(31 downto 0);   -- unmatched2（RO）
    unmatched3 : in  std_logic_vector(31 downto 0)   -- unmatched3（RO）
  );
end entity;

architecture rtl of csr_sampler_match is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal addr_r : std_logic_vector(31 downto 0) := (others => '0');
  signal mask_r : std_logic_vector(31 downto 0) := (others => '0');
  signal value_r : std_logic_vector(31 downto 0) := (others => '0');
  signal control_enable_r : std_logic := '0';
  signal control_once_r : std_logic := '0';
  signal control_after_start_r : std_logic := '0';
  signal control_first_sample_r : std_logic := '0';
  signal addr_mask_r : std_logic_vector(31 downto 0) := x"FFFFFFFF";
begin
  start <= bus_req = '1' and ack = '0';
  index <= to_integer(bus_cmd.addr(3 downto 0));

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      err   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）
      if rst = '1' then
        addr_r <= (others => '0');
        mask_r <= (others => '0');
        value_r <= (others => '0');
        control_enable_r <= '0';
        control_once_r <= '0';
        control_after_start_r <= '0';
        control_first_sample_r <= '0';
        addr_mask_r <= x"FFFFFFFF";
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- addr
            rdata(31 downto 0) <= addr_r;
            if bus_cmd.we = '1' then
              addr_r <= bus_cmd.wdata(31 downto 0);
            end if;
          when 1 =>   -- mask
            rdata(31 downto 0) <= mask_r;
            if bus_cmd.we = '1' then
              mask_r <= bus_cmd.wdata(31 downto 0);
            end if;
          when 2 =>   -- value
            rdata(31 downto 0) <= value_r;
            if bus_cmd.we = '1' then
              value_r <= bus_cmd.wdata(31 downto 0);
            end if;
          when 3 =>   -- control
            rdata(0) <= control_enable_r;
            rdata(1) <= control_once_r;
            rdata(2) <= control_after_start_r;
            rdata(3) <= control_first_sample_r;
            rdata(8) <= control_matched;
            rdata(9) <= control_seen_any;
            rdata(10) <= control_started;
            if bus_cmd.we = '1' then
              control_enable_r <= bus_cmd.wdata(0);
              control_once_r <= bus_cmd.wdata(1);
              control_after_start_r <= bus_cmd.wdata(2);
              control_first_sample_r <= bus_cmd.wdata(3);
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
          when 10 =>   -- addr_mask
            rdata(31 downto 0) <= addr_mask_r;
            if bus_cmd.we = '1' then
              addr_mask_r <= bus_cmd.wdata(31 downto 0);
            end if;
          when 11 =>   -- fire
            rdata(6 downto 0) <= fire_index;
            rdata(8) <= fire_first;
          when 12 =>   -- unmatched0
            rdata(31 downto 0) <= unmatched0;
          when 13 =>   -- unmatched1
            rdata(31 downto 0) <= unmatched1;
          when 14 =>   -- unmatched2
            rdata(31 downto 0) <= unmatched2;
          when 15 =>   -- unmatched3
            rdata(31 downto 0) <= unmatched3;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_rsp.ack   <= ack;
  bus_rsp.err   <= err;
  bus_rsp.rdata <= rdata;
  addr <= addr_r;
  mask <= mask_r;
  value <= value_r;
  control_enable <= control_enable_r;
  control_once <= control_once_r;
  control_after_start <= control_after_start_r;
  control_first_sample <= control_first_sample_r;
  addr_mask <= addr_mask_r;
end architecture;
