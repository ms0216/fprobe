-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック xtrig（0x00000500, 64 バイト）: クロストリガ（docs/superpowers/specs/2026-10-16-m9-cross-trigger-design.md）。源: bit0 = ロジアナのトリガ、bit1 = DAP の番地一致、bit2 = ホストの指令（command.fire）、bit3 = 多段トリガ（trigseq）、bit4 = FPGA の観測の値の一致（obs.match）、bit5 = サンプラの値の一致（sampler_match。構成によって無い）
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_xtrig is
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
    route_to_la : out std_logic_vector(5 downto 0);   -- route.to_la（RW）
    route_to_inject : out std_logic_vector(5 downto 0);   -- route.to_inject（RW）
    command_fire : out std_logic;   -- command.fire（PULSE: 1 を書いた次の周期に 1）
    inject_ap : out std_logic_vector(7 downto 0);   -- inject_ap（RW）
    inject_addr : out std_logic_vector(31 downto 0);   -- inject_addr（RW）
    inject_value : out std_logic_vector(31 downto 0);   -- inject_value（RW）
    inject_status_busy : in  std_logic;   -- inject_status.busy（RO）
    inject_status_result : in  std_logic_vector(1 downto 0);   -- inject_status.result（RO）
    inject_status_ack : in  std_logic_vector(2 downto 0);   -- inject_status.ack（RO）
    inject_status_count : in  std_logic_vector(15 downto 0);   -- inject_status.count（RO）
    inject_time_lo : in  std_logic_vector(31 downto 0);   -- inject_time_lo（RO）
    inject_time_hi : in  std_logic_vector(31 downto 0);   -- inject_time_hi（RO）
    match_addr : out std_logic_vector(31 downto 0);   -- match_addr（RW）
    match_addr_mask : out std_logic_vector(31 downto 0);   -- match_addr_mask（RW）
    match_data : out std_logic_vector(31 downto 0);   -- match_data（RW）
    match_data_mask : out std_logic_vector(31 downto 0);   -- match_data_mask（RW）
    match_mode_on_write : out std_logic;   -- match_mode.on_write（RW）
    match_mode_on_read : out std_logic;   -- match_mode.on_read（RW）
    match_count : in  std_logic_vector(31 downto 0);   -- match_count（RO）
    match_time_lo : in  std_logic_vector(31 downto 0);   -- match_time_lo（RO）
    match_time_hi : in  std_logic_vector(31 downto 0)   -- match_time_hi（RO）
  );
end entity;

architecture rtl of csr_xtrig is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal route_to_la_r : std_logic_vector(5 downto 0) := "000000";
  signal route_to_inject_r : std_logic_vector(5 downto 0) := "000000";
  signal command_fire_r : std_logic := '0';
  signal inject_ap_r : std_logic_vector(7 downto 0) := "00000000";
  signal inject_addr_r : std_logic_vector(31 downto 0) := "11100000000000001110110111110000";
  signal inject_value_r : std_logic_vector(31 downto 0) := "10100000010111110000000000000011";
  signal match_addr_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal match_addr_mask_r : std_logic_vector(31 downto 0) := "11111111111111111111111111111111";
  signal match_data_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal match_data_mask_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal match_mode_on_write_r : std_logic := '0';
  signal match_mode_on_read_r : std_logic := '0';
begin
  start <= bus_req = '1' and ack = '0';
  index <= to_integer(bus_addr(3 downto 0));

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      err   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）
      command_fire_r <= '0';
      if rst = '1' then
        route_to_la_r <= "000000";
        route_to_inject_r <= "000000";
        inject_ap_r <= "00000000";
        inject_addr_r <= "11100000000000001110110111110000";
        inject_value_r <= "10100000010111110000000000000011";
        match_addr_r <= "00000000000000000000000000000000";
        match_addr_mask_r <= "11111111111111111111111111111111";
        match_data_r <= "00000000000000000000000000000000";
        match_data_mask_r <= "00000000000000000000000000000000";
        match_mode_on_write_r <= '0';
        match_mode_on_read_r <= '0';
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- route
            rdata(5 downto 0) <= route_to_la_r;
            rdata(13 downto 8) <= route_to_inject_r;
            if bus_we = '1' then
              route_to_la_r <= bus_wdata(5 downto 0);
              route_to_inject_r <= bus_wdata(13 downto 8);
            end if;
          when 1 =>   -- command
            if bus_we = '1' then
              command_fire_r <= bus_wdata(0);
            end if;
          when 2 =>   -- inject_ap
            rdata(7 downto 0) <= inject_ap_r;
            if bus_we = '1' then
              inject_ap_r <= bus_wdata(7 downto 0);
            end if;
          when 3 =>   -- inject_addr
            rdata(31 downto 0) <= inject_addr_r;
            if bus_we = '1' then
              inject_addr_r <= bus_wdata(31 downto 0);
            end if;
          when 4 =>   -- inject_value
            rdata(31 downto 0) <= inject_value_r;
            if bus_we = '1' then
              inject_value_r <= bus_wdata(31 downto 0);
            end if;
          when 5 =>   -- inject_status
            rdata(0) <= inject_status_busy;
            rdata(5 downto 4) <= inject_status_result;
            rdata(10 downto 8) <= inject_status_ack;
            rdata(31 downto 16) <= inject_status_count;
          when 6 =>   -- inject_time_lo
            rdata(31 downto 0) <= inject_time_lo;
          when 7 =>   -- inject_time_hi
            rdata(31 downto 0) <= inject_time_hi;
          when 8 =>   -- match_addr
            rdata(31 downto 0) <= match_addr_r;
            if bus_we = '1' then
              match_addr_r <= bus_wdata(31 downto 0);
            end if;
          when 9 =>   -- match_addr_mask
            rdata(31 downto 0) <= match_addr_mask_r;
            if bus_we = '1' then
              match_addr_mask_r <= bus_wdata(31 downto 0);
            end if;
          when 10 =>   -- match_data
            rdata(31 downto 0) <= match_data_r;
            if bus_we = '1' then
              match_data_r <= bus_wdata(31 downto 0);
            end if;
          when 11 =>   -- match_data_mask
            rdata(31 downto 0) <= match_data_mask_r;
            if bus_we = '1' then
              match_data_mask_r <= bus_wdata(31 downto 0);
            end if;
          when 12 =>   -- match_mode
            rdata(0) <= match_mode_on_write_r;
            rdata(1) <= match_mode_on_read_r;
            if bus_we = '1' then
              match_mode_on_write_r <= bus_wdata(0);
              match_mode_on_read_r <= bus_wdata(1);
            end if;
          when 13 =>   -- match_count
            rdata(31 downto 0) <= match_count;
          when 14 =>   -- match_time_lo
            rdata(31 downto 0) <= match_time_lo;
          when 15 =>   -- match_time_hi
            rdata(31 downto 0) <= match_time_hi;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_ack   <= ack;
  bus_err   <= err;
  bus_rdata <= rdata;
  route_to_la <= route_to_la_r;
  route_to_inject <= route_to_inject_r;
  command_fire <= command_fire_r;
  inject_ap <= inject_ap_r;
  inject_addr <= inject_addr_r;
  inject_value <= inject_value_r;
  match_addr <= match_addr_r;
  match_addr_mask <= match_addr_mask_r;
  match_data <= match_data_r;
  match_data_mask <= match_data_mask_r;
  match_mode_on_write <= match_mode_on_write_r;
  match_mode_on_read <= match_mode_on_read_r;
end architecture;
