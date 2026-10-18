-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック trigseq（0x00000A00, 64 バイト）: 多段トリガ（docs/superpowers/specs/2026-10-16-m9c-trigger-sequencer-design.md）。事象: bit0 = パターン A、bit1 = パターン B、bit2 = DAP の番地一致、bit3 = ホストの指令。発火は xtrig の源 bit3
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_trigseq is
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
    control_stages : out std_logic_vector(2 downto 0);   -- control.stages（RW）
    status_stage : in  std_logic_vector(1 downto 0);   -- status.stage（RO）
    status_fired : in  std_logic;   -- status.fired（RO）
    pattern_a_mask : out std_logic_vector(15 downto 0);   -- pattern_a.mask（RW）
    pattern_a_value : out std_logic_vector(15 downto 0);   -- pattern_a.value（RW）
    edge_a_rise : out std_logic_vector(15 downto 0);   -- edge_a.rise（RW）
    edge_a_fall : out std_logic_vector(15 downto 0);   -- edge_a.fall（RW）
    pattern_b_mask : out std_logic_vector(15 downto 0);   -- pattern_b.mask（RW）
    pattern_b_value : out std_logic_vector(15 downto 0);   -- pattern_b.value（RW）
    edge_b_rise : out std_logic_vector(15 downto 0);   -- edge_b.rise（RW）
    edge_b_fall : out std_logic_vector(15 downto 0);   -- edge_b.fall（RW）
    stage0_cond : out std_logic_vector(3 downto 0);   -- stage0.cond（RW）
    stage0_count : out std_logic_vector(15 downto 0);   -- stage0.count（RW）
    stage1_cond : out std_logic_vector(3 downto 0);   -- stage1.cond（RW）
    stage1_count : out std_logic_vector(15 downto 0);   -- stage1.count（RW）
    stage2_cond : out std_logic_vector(3 downto 0);   -- stage2.cond（RW）
    stage2_count : out std_logic_vector(15 downto 0);   -- stage2.count（RW）
    stage3_cond : out std_logic_vector(3 downto 0);   -- stage3.cond（RW）
    stage3_count : out std_logic_vector(15 downto 0);   -- stage3.count（RW）
    within1 : out std_logic_vector(31 downto 0);   -- within1（RW）
    within2 : out std_logic_vector(31 downto 0);   -- within2（RW）
    within3 : out std_logic_vector(31 downto 0);   -- within3（RW）
    fire_time_lo : in  std_logic_vector(31 downto 0);   -- fire_time_lo（RO）
    fire_time_hi : in  std_logic_vector(31 downto 0)   -- fire_time_hi（RO）
  );
end entity;

architecture rtl of csr_trigseq is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal control_enable_r : std_logic := '0';
  signal control_stages_r : std_logic_vector(2 downto 0) := "001";
  signal pattern_a_mask_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal pattern_a_value_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal edge_a_rise_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal edge_a_fall_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal pattern_b_mask_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal pattern_b_value_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal edge_b_rise_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal edge_b_fall_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal stage0_cond_r : std_logic_vector(3 downto 0) := "0000";
  signal stage0_count_r : std_logic_vector(15 downto 0) := "0000000000000001";
  signal stage1_cond_r : std_logic_vector(3 downto 0) := "0000";
  signal stage1_count_r : std_logic_vector(15 downto 0) := "0000000000000001";
  signal stage2_cond_r : std_logic_vector(3 downto 0) := "0000";
  signal stage2_count_r : std_logic_vector(15 downto 0) := "0000000000000001";
  signal stage3_cond_r : std_logic_vector(3 downto 0) := "0000";
  signal stage3_count_r : std_logic_vector(15 downto 0) := "0000000000000001";
  signal within1_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal within2_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal within3_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
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
        control_enable_r <= '0';
        control_stages_r <= "001";
        pattern_a_mask_r <= "0000000000000000";
        pattern_a_value_r <= "0000000000000000";
        edge_a_rise_r <= "0000000000000000";
        edge_a_fall_r <= "0000000000000000";
        pattern_b_mask_r <= "0000000000000000";
        pattern_b_value_r <= "0000000000000000";
        edge_b_rise_r <= "0000000000000000";
        edge_b_fall_r <= "0000000000000000";
        stage0_cond_r <= "0000";
        stage0_count_r <= "0000000000000001";
        stage1_cond_r <= "0000";
        stage1_count_r <= "0000000000000001";
        stage2_cond_r <= "0000";
        stage2_count_r <= "0000000000000001";
        stage3_cond_r <= "0000";
        stage3_count_r <= "0000000000000001";
        within1_r <= "00000000000000000000000000000000";
        within2_r <= "00000000000000000000000000000000";
        within3_r <= "00000000000000000000000000000000";
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- control
            rdata(0) <= control_enable_r;
            rdata(10 downto 8) <= control_stages_r;
            if bus_we = '1' then
              control_enable_r <= bus_wdata(0);
              control_stages_r <= bus_wdata(10 downto 8);
            end if;
          when 1 =>   -- status
            rdata(1 downto 0) <= status_stage;
            rdata(8) <= status_fired;
          when 2 =>   -- pattern_a
            rdata(15 downto 0) <= pattern_a_mask_r;
            rdata(31 downto 16) <= pattern_a_value_r;
            if bus_we = '1' then
              pattern_a_mask_r <= bus_wdata(15 downto 0);
              pattern_a_value_r <= bus_wdata(31 downto 16);
            end if;
          when 3 =>   -- edge_a
            rdata(15 downto 0) <= edge_a_rise_r;
            rdata(31 downto 16) <= edge_a_fall_r;
            if bus_we = '1' then
              edge_a_rise_r <= bus_wdata(15 downto 0);
              edge_a_fall_r <= bus_wdata(31 downto 16);
            end if;
          when 4 =>   -- pattern_b
            rdata(15 downto 0) <= pattern_b_mask_r;
            rdata(31 downto 16) <= pattern_b_value_r;
            if bus_we = '1' then
              pattern_b_mask_r <= bus_wdata(15 downto 0);
              pattern_b_value_r <= bus_wdata(31 downto 16);
            end if;
          when 5 =>   -- edge_b
            rdata(15 downto 0) <= edge_b_rise_r;
            rdata(31 downto 16) <= edge_b_fall_r;
            if bus_we = '1' then
              edge_b_rise_r <= bus_wdata(15 downto 0);
              edge_b_fall_r <= bus_wdata(31 downto 16);
            end if;
          when 6 =>   -- stage0
            rdata(3 downto 0) <= stage0_cond_r;
            rdata(31 downto 16) <= stage0_count_r;
            if bus_we = '1' then
              stage0_cond_r <= bus_wdata(3 downto 0);
              stage0_count_r <= bus_wdata(31 downto 16);
            end if;
          when 7 =>   -- stage1
            rdata(3 downto 0) <= stage1_cond_r;
            rdata(31 downto 16) <= stage1_count_r;
            if bus_we = '1' then
              stage1_cond_r <= bus_wdata(3 downto 0);
              stage1_count_r <= bus_wdata(31 downto 16);
            end if;
          when 8 =>   -- stage2
            rdata(3 downto 0) <= stage2_cond_r;
            rdata(31 downto 16) <= stage2_count_r;
            if bus_we = '1' then
              stage2_cond_r <= bus_wdata(3 downto 0);
              stage2_count_r <= bus_wdata(31 downto 16);
            end if;
          when 9 =>   -- stage3
            rdata(3 downto 0) <= stage3_cond_r;
            rdata(31 downto 16) <= stage3_count_r;
            if bus_we = '1' then
              stage3_cond_r <= bus_wdata(3 downto 0);
              stage3_count_r <= bus_wdata(31 downto 16);
            end if;
          when 10 =>   -- within1
            rdata(31 downto 0) <= within1_r;
            if bus_we = '1' then
              within1_r <= bus_wdata(31 downto 0);
            end if;
          when 11 =>   -- within2
            rdata(31 downto 0) <= within2_r;
            if bus_we = '1' then
              within2_r <= bus_wdata(31 downto 0);
            end if;
          when 12 =>   -- within3
            rdata(31 downto 0) <= within3_r;
            if bus_we = '1' then
              within3_r <= bus_wdata(31 downto 0);
            end if;
          when 13 =>   -- fire_time_lo
            rdata(31 downto 0) <= fire_time_lo;
          when 14 =>   -- fire_time_hi
            rdata(31 downto 0) <= fire_time_hi;
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
  control_stages <= control_stages_r;
  pattern_a_mask <= pattern_a_mask_r;
  pattern_a_value <= pattern_a_value_r;
  edge_a_rise <= edge_a_rise_r;
  edge_a_fall <= edge_a_fall_r;
  pattern_b_mask <= pattern_b_mask_r;
  pattern_b_value <= pattern_b_value_r;
  edge_b_rise <= edge_b_rise_r;
  edge_b_fall <= edge_b_fall_r;
  stage0_cond <= stage0_cond_r;
  stage0_count <= stage0_count_r;
  stage1_cond <= stage1_cond_r;
  stage1_count <= stage1_count_r;
  stage2_cond <= stage2_cond_r;
  stage2_count <= stage2_count_r;
  stage3_cond <= stage3_cond_r;
  stage3_count <= stage3_count_r;
  within1 <= within1_r;
  within2 <= within2_r;
  within3 <= within3_r;
end architecture;
