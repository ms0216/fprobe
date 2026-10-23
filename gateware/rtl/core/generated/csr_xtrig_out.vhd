-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック xtrig_out（0x00000D00, 64 バイト）: クロストリガの動作: 出力の端子（docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md）。動作 k（同時に 2 本）が、ボードの出力の端子の候補（ケイパビリティ OUTPUTS）の 1 本を動かす。源は xtrig と同じ（bit0 ロジアナのトリガ、bit1 DAP の番地一致、bit2 ホストの指令、bit3 多段トリガ、bit4 FPGA の観測の値の一致、bit5 サンプラの値の一致）
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_xtrig_out is
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
    status_active : in  std_logic_vector(1 downto 0);   -- status.active（RO）
    when0_mask : out std_logic_vector(7 downto 0);   -- when0.mask（RW）
    when0_value : out std_logic_vector(7 downto 0);   -- when0.value（RW）
    when0_sources : out std_logic_vector(5 downto 0);   -- when0.sources（RW）
    when0_any : out std_logic;   -- when0.any（RW）
    ctrl0_enable : out std_logic;   -- ctrl0.enable（RW）
    ctrl0_mode : out std_logic_vector(1 downto 0);   -- ctrl0.mode（RW）
    ctrl0_level : out std_logic;   -- ctrl0.level（RW）
    ctrl0_claim : out std_logic;   -- ctrl0.claim（RW）
    ctrl0_length : out std_logic_vector(7 downto 0);   -- ctrl0.length（RW）
    ctrl0_unit : out std_logic_vector(2 downto 0);   -- ctrl0.unit（RW）
    ctrl0_pin : out std_logic_vector(1 downto 0);   -- ctrl0.pin（RW）
    when1_mask : out std_logic_vector(7 downto 0);   -- when1.mask（RW）
    when1_value : out std_logic_vector(7 downto 0);   -- when1.value（RW）
    when1_sources : out std_logic_vector(5 downto 0);   -- when1.sources（RW）
    when1_any : out std_logic;   -- when1.any（RW）
    ctrl1_enable : out std_logic;   -- ctrl1.enable（RW）
    ctrl1_mode : out std_logic_vector(1 downto 0);   -- ctrl1.mode（RW）
    ctrl1_level : out std_logic;   -- ctrl1.level（RW）
    ctrl1_claim : out std_logic;   -- ctrl1.claim（RW）
    ctrl1_length : out std_logic_vector(7 downto 0);   -- ctrl1.length（RW）
    ctrl1_unit : out std_logic_vector(2 downto 0);   -- ctrl1.unit（RW）
    ctrl1_pin : out std_logic_vector(1 downto 0)   -- ctrl1.pin（RW）
  );
end entity;

architecture rtl of csr_xtrig_out is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal when0_mask_r : std_logic_vector(7 downto 0) := "00000000";
  signal when0_value_r : std_logic_vector(7 downto 0) := "00000000";
  signal when0_sources_r : std_logic_vector(5 downto 0) := "000000";
  signal when0_any_r : std_logic := '0';
  signal ctrl0_enable_r : std_logic := '0';
  signal ctrl0_mode_r : std_logic_vector(1 downto 0) := "00";
  signal ctrl0_level_r : std_logic := '1';
  signal ctrl0_claim_r : std_logic := '0';
  signal ctrl0_length_r : std_logic_vector(7 downto 0) := "00000001";
  signal ctrl0_unit_r : std_logic_vector(2 downto 0) := "000";
  signal ctrl0_pin_r : std_logic_vector(1 downto 0) := "00";
  signal when1_mask_r : std_logic_vector(7 downto 0) := "00000000";
  signal when1_value_r : std_logic_vector(7 downto 0) := "00000000";
  signal when1_sources_r : std_logic_vector(5 downto 0) := "000000";
  signal when1_any_r : std_logic := '0';
  signal ctrl1_enable_r : std_logic := '0';
  signal ctrl1_mode_r : std_logic_vector(1 downto 0) := "00";
  signal ctrl1_level_r : std_logic := '1';
  signal ctrl1_claim_r : std_logic := '0';
  signal ctrl1_length_r : std_logic_vector(7 downto 0) := "00000001";
  signal ctrl1_unit_r : std_logic_vector(2 downto 0) := "000";
  signal ctrl1_pin_r : std_logic_vector(1 downto 0) := "00";
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
        when0_mask_r <= "00000000";
        when0_value_r <= "00000000";
        when0_sources_r <= "000000";
        when0_any_r <= '0';
        ctrl0_enable_r <= '0';
        ctrl0_mode_r <= "00";
        ctrl0_level_r <= '1';
        ctrl0_claim_r <= '0';
        ctrl0_length_r <= "00000001";
        ctrl0_unit_r <= "000";
        ctrl0_pin_r <= "00";
        when1_mask_r <= "00000000";
        when1_value_r <= "00000000";
        when1_sources_r <= "000000";
        when1_any_r <= '0';
        ctrl1_enable_r <= '0';
        ctrl1_mode_r <= "00";
        ctrl1_level_r <= '1';
        ctrl1_claim_r <= '0';
        ctrl1_length_r <= "00000001";
        ctrl1_unit_r <= "000";
        ctrl1_pin_r <= "00";
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- status
            rdata(1 downto 0) <= status_active;
          when 1 =>   -- when0
            rdata(7 downto 0) <= when0_mask_r;
            rdata(15 downto 8) <= when0_value_r;
            rdata(21 downto 16) <= when0_sources_r;
            rdata(24) <= when0_any_r;
            if bus_we = '1' then
              when0_mask_r <= bus_wdata(7 downto 0);
              when0_value_r <= bus_wdata(15 downto 8);
              when0_sources_r <= bus_wdata(21 downto 16);
              when0_any_r <= bus_wdata(24);
            end if;
          when 2 =>   -- ctrl0
            rdata(0) <= ctrl0_enable_r;
            rdata(5 downto 4) <= ctrl0_mode_r;
            rdata(8) <= ctrl0_level_r;
            rdata(12) <= ctrl0_claim_r;
            rdata(23 downto 16) <= ctrl0_length_r;
            rdata(26 downto 24) <= ctrl0_unit_r;
            rdata(29 downto 28) <= ctrl0_pin_r;
            if bus_we = '1' then
              ctrl0_enable_r <= bus_wdata(0);
              ctrl0_mode_r <= bus_wdata(5 downto 4);
              ctrl0_level_r <= bus_wdata(8);
              ctrl0_claim_r <= bus_wdata(12);
              ctrl0_length_r <= bus_wdata(23 downto 16);
              ctrl0_unit_r <= bus_wdata(26 downto 24);
              ctrl0_pin_r <= bus_wdata(29 downto 28);
            end if;
          when 3 =>   -- when1
            rdata(7 downto 0) <= when1_mask_r;
            rdata(15 downto 8) <= when1_value_r;
            rdata(21 downto 16) <= when1_sources_r;
            rdata(24) <= when1_any_r;
            if bus_we = '1' then
              when1_mask_r <= bus_wdata(7 downto 0);
              when1_value_r <= bus_wdata(15 downto 8);
              when1_sources_r <= bus_wdata(21 downto 16);
              when1_any_r <= bus_wdata(24);
            end if;
          when 4 =>   -- ctrl1
            rdata(0) <= ctrl1_enable_r;
            rdata(5 downto 4) <= ctrl1_mode_r;
            rdata(8) <= ctrl1_level_r;
            rdata(12) <= ctrl1_claim_r;
            rdata(23 downto 16) <= ctrl1_length_r;
            rdata(26 downto 24) <= ctrl1_unit_r;
            rdata(29 downto 28) <= ctrl1_pin_r;
            if bus_we = '1' then
              ctrl1_enable_r <= bus_wdata(0);
              ctrl1_mode_r <= bus_wdata(5 downto 4);
              ctrl1_level_r <= bus_wdata(8);
              ctrl1_claim_r <= bus_wdata(12);
              ctrl1_length_r <= bus_wdata(23 downto 16);
              ctrl1_unit_r <= bus_wdata(26 downto 24);
              ctrl1_pin_r <= bus_wdata(29 downto 28);
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
  when0_mask <= when0_mask_r;
  when0_value <= when0_value_r;
  when0_sources <= when0_sources_r;
  when0_any <= when0_any_r;
  ctrl0_enable <= ctrl0_enable_r;
  ctrl0_mode <= ctrl0_mode_r;
  ctrl0_level <= ctrl0_level_r;
  ctrl0_claim <= ctrl0_claim_r;
  ctrl0_length <= ctrl0_length_r;
  ctrl0_unit <= ctrl0_unit_r;
  ctrl0_pin <= ctrl0_pin_r;
  when1_mask <= when1_mask_r;
  when1_value <= when1_value_r;
  when1_sources <= when1_sources_r;
  when1_any <= when1_any_r;
  ctrl1_enable <= ctrl1_enable_r;
  ctrl1_mode <= ctrl1_mode_r;
  ctrl1_level <= ctrl1_level_r;
  ctrl1_claim <= ctrl1_claim_r;
  ctrl1_length <= ctrl1_length_r;
  ctrl1_unit <= ctrl1_unit_r;
  ctrl1_pin <= ctrl1_pin_r;
end architecture;
