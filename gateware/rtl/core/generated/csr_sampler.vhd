-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック sampler（0x00000C00, 64 バイト）: DAP サンプラ（F4: 変数のライブ観測。docs/superpowers/specs/2026-10-17-f4-live-sampler-design.md）。最大 8 か所（各 1〜16 語）を周期的に読み、1 語ごとにイベント（種類 7）にする
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_sampler is
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
    control_run : out std_logic;   -- control.run（RW）
    control_ap : out std_logic_vector(7 downto 0);   -- control.ap（RW）
    control_jitter : out std_logic_vector(3 downto 0);   -- control.jitter（RW）
    period : out std_logic_vector(31 downto 0);   -- period（RW）
    slot_index : out std_logic_vector(2 downto 0);   -- slot_index（RW）
    slot_addr : out std_logic_vector(31 downto 0);   -- slot_addr（RW）
    slot_config_enable : out std_logic;   -- slot_config.enable（RW）
    slot_config_words : out std_logic_vector(3 downto 0);   -- slot_config.words（RW）
    slot_write : out std_logic;   -- slot_write（PULSE: 1 を書いた次の周期に 1）
    rounds : in  std_logic_vector(31 downto 0);   -- rounds（RO）
    errors : in  std_logic_vector(31 downto 0);   -- errors（RO）
    overruns : in  std_logic_vector(31 downto 0);   -- overruns（RO）
    status_last_ack : in  std_logic_vector(2 downto 0);   -- status.last_ack（RO）
    status_busy : in  std_logic;   -- status.busy（RO）
    status_held : in  std_logic   -- status.held（RO）
  );
end entity;

architecture rtl of csr_sampler is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal control_run_r : std_logic := '0';
  signal control_ap_r : std_logic_vector(7 downto 0) := "00000000";
  signal control_jitter_r : std_logic_vector(3 downto 0) := "0000";
  signal period_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal slot_index_r : std_logic_vector(2 downto 0) := "000";
  signal slot_addr_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal slot_config_enable_r : std_logic := '0';
  signal slot_config_words_r : std_logic_vector(3 downto 0) := "0000";
  signal slot_write_r : std_logic := '0';
begin
  start <= bus_req = '1' and ack = '0';
  index <= to_integer(bus_addr(3 downto 0));

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      err   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）
      slot_write_r <= '0';
      if rst = '1' then
        control_run_r <= '0';
        control_ap_r <= "00000000";
        control_jitter_r <= "0000";
        period_r <= "00000000000000000000000000000000";
        slot_index_r <= "000";
        slot_addr_r <= "00000000000000000000000000000000";
        slot_config_enable_r <= '0';
        slot_config_words_r <= "0000";
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- control
            rdata(0) <= control_run_r;
            rdata(15 downto 8) <= control_ap_r;
            rdata(19 downto 16) <= control_jitter_r;
            if bus_we = '1' then
              control_run_r <= bus_wdata(0);
              control_ap_r <= bus_wdata(15 downto 8);
              control_jitter_r <= bus_wdata(19 downto 16);
            end if;
          when 1 =>   -- period
            rdata(31 downto 0) <= period_r;
            if bus_we = '1' then
              period_r <= bus_wdata(31 downto 0);
            end if;
          when 2 =>   -- slot_index
            rdata(2 downto 0) <= slot_index_r;
            if bus_we = '1' then
              slot_index_r <= bus_wdata(2 downto 0);
            end if;
          when 3 =>   -- slot_addr
            rdata(31 downto 0) <= slot_addr_r;
            if bus_we = '1' then
              slot_addr_r <= bus_wdata(31 downto 0);
            end if;
          when 4 =>   -- slot_config
            rdata(0) <= slot_config_enable_r;
            rdata(11 downto 8) <= slot_config_words_r;
            if bus_we = '1' then
              slot_config_enable_r <= bus_wdata(0);
              slot_config_words_r <= bus_wdata(11 downto 8);
            end if;
          when 5 =>   -- slot_write
            if bus_we = '1' then
              slot_write_r <= bus_wdata(0);
            end if;
          when 6 =>   -- rounds
            rdata(31 downto 0) <= rounds;
          when 7 =>   -- errors
            rdata(31 downto 0) <= errors;
          when 8 =>   -- overruns
            rdata(31 downto 0) <= overruns;
          when 9 =>   -- status
            rdata(2 downto 0) <= status_last_ack;
            rdata(8) <= status_busy;
            rdata(9) <= status_held;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_ack   <= ack;
  bus_err   <= err;
  bus_rdata <= rdata;
  control_run <= control_run_r;
  control_ap <= control_ap_r;
  control_jitter <= control_jitter_r;
  period <= period_r;
  slot_index <= slot_index_r;
  slot_addr <= slot_addr_r;
  slot_config_enable <= slot_config_enable_r;
  slot_config_words <= slot_config_words_r;
  slot_write <= slot_write_r;
end architecture;
