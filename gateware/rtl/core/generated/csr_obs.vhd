-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック obs（0x00000E00, 64 バイト）: 対象の FPGA の観測（docs/superpowers/specs/2026-10-20-fpga-observation-v2-design.md）。対象の JTAG のユーザーの DR の観測の回路（target_ip/fprobe_obs）をスキャンし続け、記録をイベント 8、時刻合わせをイベント 9 にする
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_obs is
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
    control_enable : out std_logic;   -- control.enable（RW）
    control_release : out std_logic;   -- control.release（RW）
    half_period : out std_logic_vector(15 downto 0);   -- half_period（RW）
    chain_ir_len : out std_logic_vector(5 downto 0);   -- chain.ir_len（RW）
    chain_ir_before : out std_logic_vector(5 downto 0);   -- chain.ir_before（RW）
    chain_ir_after : out std_logic_vector(5 downto 0);   -- chain.ir_after（RW）
    chain_dr_before : out std_logic_vector(2 downto 0);   -- chain.dr_before（RW）
    chain_dr_after : out std_logic_vector(2 downto 0);   -- chain.dr_after（RW）
    user_ir : out std_logic_vector(31 downto 0);   -- user_ir（RW）
    words : out std_logic_vector(8 downto 0);   -- words（RW）
    sync_period : out std_logic_vector(15 downto 0);   -- sync_period（RW）
    cmd : out std_logic_vector(15 downto 0);   -- cmd（RW）
    cmd_push : out std_logic;   -- cmd_push（PULSE: 1 を書いた次の周期に 1）
    status_running : in  std_logic;   -- status.running（RO）
    status_id_ok : in  std_logic;   -- status.id_ok（RO）
    status_stuck : in  std_logic;   -- status.stuck（RO）
    status_cmd_pending : in  std_logic;   -- status.cmd_pending（RO）
    status_overflow_seen : in  std_logic;   -- status.overflow_seen（RO）
    status_cmd_dropped : in  std_logic;   -- status.cmd_dropped（RO）
    status_target_running : in  std_logic;   -- status.target_running（RO）
    status_driving : in  std_logic;   -- status.driving（RO）
    scans : in  std_logic_vector(31 downto 0);   -- scans（RO）
    read_value : in  std_logic_vector(31 downto 0);   -- read_value（RO）
    id : in  std_logic_vector(31 downto 0);   -- id（RO）
    match_mask : out std_logic_vector(15 downto 0);   -- match.mask（RW）
    match_value : out std_logic_vector(15 downto 0);   -- match.value（RW）
    match_change_change : out std_logic_vector(15 downto 0);   -- match_change.change（RW）
    match_change_enable : out std_logic;   -- match_change.enable（RW）
    match_count : in  std_logic_vector(31 downto 0)   -- match_count（RO）
  );
end entity;

architecture rtl of csr_obs is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal control_enable_r : std_logic := '0';
  signal control_release_r : std_logic := '0';
  signal half_period_r : std_logic_vector(15 downto 0) := "0000000000001010";
  signal chain_ir_len_r : std_logic_vector(5 downto 0) := "000100";
  signal chain_ir_before_r : std_logic_vector(5 downto 0) := "000000";
  signal chain_ir_after_r : std_logic_vector(5 downto 0) := "000000";
  signal chain_dr_before_r : std_logic_vector(2 downto 0) := "000";
  signal chain_dr_after_r : std_logic_vector(2 downto 0) := "000";
  signal user_ir_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000010";
  signal words_r : std_logic_vector(8 downto 0) := "000010000";
  signal sync_period_r : std_logic_vector(15 downto 0) := "0000000000010000";
  signal cmd_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal cmd_push_r : std_logic := '0';
  signal match_mask_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal match_value_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal match_change_change_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal match_change_enable_r : std_logic := '0';
begin
  start <= bus_req = '1' and ack = '0';
  index <= to_integer(bus_addr(3 downto 0));

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      err   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）
      cmd_push_r <= '0';
      if rst = '1' then
        control_enable_r <= '0';
        control_release_r <= '0';
        half_period_r <= "0000000000001010";
        chain_ir_len_r <= "000100";
        chain_ir_before_r <= "000000";
        chain_ir_after_r <= "000000";
        chain_dr_before_r <= "000";
        chain_dr_after_r <= "000";
        user_ir_r <= "00000000000000000000000000000010";
        words_r <= "000010000";
        sync_period_r <= "0000000000010000";
        cmd_r <= "0000000000000000";
        match_mask_r <= "0000000000000000";
        match_value_r <= "0000000000000000";
        match_change_change_r <= "0000000000000000";
        match_change_enable_r <= '0';
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- control
            rdata(0) <= control_enable_r;
            rdata(1) <= control_release_r;
            if bus_we = '1' then
              control_enable_r <= bus_wdata(0);
              control_release_r <= bus_wdata(1);
            end if;
          when 1 =>   -- half_period
            rdata(15 downto 0) <= half_period_r;
            if bus_we = '1' then
              half_period_r <= bus_wdata(15 downto 0);
            end if;
          when 2 =>   -- chain
            rdata(5 downto 0) <= chain_ir_len_r;
            rdata(11 downto 6) <= chain_ir_before_r;
            rdata(17 downto 12) <= chain_ir_after_r;
            rdata(20 downto 18) <= chain_dr_before_r;
            rdata(23 downto 21) <= chain_dr_after_r;
            if bus_we = '1' then
              chain_ir_len_r <= bus_wdata(5 downto 0);
              chain_ir_before_r <= bus_wdata(11 downto 6);
              chain_ir_after_r <= bus_wdata(17 downto 12);
              chain_dr_before_r <= bus_wdata(20 downto 18);
              chain_dr_after_r <= bus_wdata(23 downto 21);
            end if;
          when 3 =>   -- user_ir
            rdata(31 downto 0) <= user_ir_r;
            if bus_we = '1' then
              user_ir_r <= bus_wdata(31 downto 0);
            end if;
          when 4 =>   -- words
            rdata(8 downto 0) <= words_r;
            if bus_we = '1' then
              words_r <= bus_wdata(8 downto 0);
            end if;
          when 5 =>   -- sync_period
            rdata(15 downto 0) <= sync_period_r;
            if bus_we = '1' then
              sync_period_r <= bus_wdata(15 downto 0);
            end if;
          when 6 =>   -- cmd
            rdata(15 downto 0) <= cmd_r;
            if bus_we = '1' then
              cmd_r <= bus_wdata(15 downto 0);
            end if;
          when 8 =>   -- cmd_push
            if bus_we = '1' then
              cmd_push_r <= bus_wdata(0);
            end if;
          when 9 =>   -- status
            rdata(0) <= status_running;
            rdata(1) <= status_id_ok;
            rdata(2) <= status_stuck;
            rdata(3) <= status_cmd_pending;
            rdata(4) <= status_overflow_seen;
            rdata(5) <= status_cmd_dropped;
            rdata(6) <= status_target_running;
            rdata(7) <= status_driving;
          when 10 =>   -- scans
            rdata(31 downto 0) <= scans;
          when 11 =>   -- read_value
            rdata(31 downto 0) <= read_value;
          when 12 =>   -- id
            rdata(31 downto 0) <= id;
          when 13 =>   -- match
            rdata(15 downto 0) <= match_mask_r;
            rdata(31 downto 16) <= match_value_r;
            if bus_we = '1' then
              match_mask_r <= bus_wdata(15 downto 0);
              match_value_r <= bus_wdata(31 downto 16);
            end if;
          when 14 =>   -- match_change
            rdata(15 downto 0) <= match_change_change_r;
            rdata(16) <= match_change_enable_r;
            if bus_we = '1' then
              match_change_change_r <= bus_wdata(15 downto 0);
              match_change_enable_r <= bus_wdata(16);
            end if;
          when 15 =>   -- match_count
            rdata(31 downto 0) <= match_count;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_ack   <= ack;
  bus_err   <= err;
  bus_rdata <= rdata;
  control_enable <= control_enable_r;
  control_release <= control_release_r;
  half_period <= half_period_r;
  chain_ir_len <= chain_ir_len_r;
  chain_ir_before <= chain_ir_before_r;
  chain_ir_after <= chain_ir_after_r;
  chain_dr_before <= chain_dr_before_r;
  chain_dr_after <= chain_dr_after_r;
  user_ir <= user_ir_r;
  words <= words_r;
  sync_period <= sync_period_r;
  cmd <= cmd_r;
  cmd_push <= cmd_push_r;
  match_mask <= match_mask_r;
  match_value <= match_value_r;
  match_change_change <= match_change_change_r;
  match_change_enable <= match_change_enable_r;
end architecture;
