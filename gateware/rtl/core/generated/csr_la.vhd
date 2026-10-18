-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック la（0x00000400, 64 バイト）: ロジックアナライザの設定と状態（docs/superpowers/specs/2026-10-16-m7-logic-analyzer-design.md）
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_la is
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
    rate : out std_logic_vector(15 downto 0);   -- rate（RW）
    pretrigger : out std_logic_vector(31 downto 0);   -- pretrigger（RW）
    trig_mask : out std_logic_vector(15 downto 0);   -- trig_mask（RW）
    trig_value : out std_logic_vector(15 downto 0);   -- trig_value（RW）
    trig_rise : out std_logic_vector(15 downto 0);   -- trig_rise（RW）
    trig_fall : out std_logic_vector(15 downto 0);   -- trig_fall（RW）
    status_capturing : in  std_logic;   -- status.capturing（RO）
    status_triggered : in  std_logic;   -- status.triggered（RO）
    status_done : in  std_logic;   -- status.done（RO）
    start_index : in  std_logic_vector(31 downto 0);   -- start_index（RO）
    trigger_index : in  std_logic_vector(31 downto 0);   -- trigger_index（RO）
    trigger_time_lo : in  std_logic_vector(31 downto 0);   -- trigger_time_lo（RO）
    trigger_time_hi : in  std_logic_vector(31 downto 0);   -- trigger_time_hi（RO）
    filter_channels : out std_logic_vector(15 downto 0);   -- filter.channels（RW）
    filter_cycles : out std_logic_vector(7 downto 0);   -- filter.cycles（RW）
    pulse_mode : out std_logic_vector(1 downto 0);   -- pulse.mode（RW）
    pulse_level : out std_logic;   -- pulse.level（RW）
    pulse_channel : out std_logic_vector(3 downto 0);   -- pulse.channel（RW）
    pulse_min : out std_logic_vector(31 downto 0);   -- pulse_min（RW）
    pulse_max : out std_logic_vector(31 downto 0)   -- pulse_max（RW）
  );
end entity;

architecture rtl of csr_la is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal control_run_r : std_logic := '0';
  signal rate_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal pretrigger_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal trig_mask_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal trig_value_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal trig_rise_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal trig_fall_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal filter_channels_r : std_logic_vector(15 downto 0) := "0000000000000000";
  signal filter_cycles_r : std_logic_vector(7 downto 0) := "00000000";
  signal pulse_mode_r : std_logic_vector(1 downto 0) := "00";
  signal pulse_level_r : std_logic := '1';
  signal pulse_channel_r : std_logic_vector(3 downto 0) := "0000";
  signal pulse_min_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal pulse_max_r : std_logic_vector(31 downto 0) := "11111111111111111111111111111111";
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
        control_run_r <= '0';
        rate_r <= "0000000000000000";
        pretrigger_r <= "00000000000000000000000000000000";
        trig_mask_r <= "0000000000000000";
        trig_value_r <= "0000000000000000";
        trig_rise_r <= "0000000000000000";
        trig_fall_r <= "0000000000000000";
        filter_channels_r <= "0000000000000000";
        filter_cycles_r <= "00000000";
        pulse_mode_r <= "00";
        pulse_level_r <= '1';
        pulse_channel_r <= "0000";
        pulse_min_r <= "00000000000000000000000000000000";
        pulse_max_r <= "11111111111111111111111111111111";
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- control
            rdata(0) <= control_run_r;
            if bus_we = '1' then
              control_run_r <= bus_wdata(0);
            end if;
          when 1 =>   -- rate
            rdata(15 downto 0) <= rate_r;
            if bus_we = '1' then
              rate_r <= bus_wdata(15 downto 0);
            end if;
          when 2 =>   -- pretrigger
            rdata(31 downto 0) <= pretrigger_r;
            if bus_we = '1' then
              pretrigger_r <= bus_wdata(31 downto 0);
            end if;
          when 3 =>   -- trig_mask
            rdata(15 downto 0) <= trig_mask_r;
            if bus_we = '1' then
              trig_mask_r <= bus_wdata(15 downto 0);
            end if;
          when 4 =>   -- trig_value
            rdata(15 downto 0) <= trig_value_r;
            if bus_we = '1' then
              trig_value_r <= bus_wdata(15 downto 0);
            end if;
          when 5 =>   -- trig_rise
            rdata(15 downto 0) <= trig_rise_r;
            if bus_we = '1' then
              trig_rise_r <= bus_wdata(15 downto 0);
            end if;
          when 6 =>   -- trig_fall
            rdata(15 downto 0) <= trig_fall_r;
            if bus_we = '1' then
              trig_fall_r <= bus_wdata(15 downto 0);
            end if;
          when 7 =>   -- status
            rdata(0) <= status_capturing;
            rdata(1) <= status_triggered;
            rdata(2) <= status_done;
          when 8 =>   -- start_index
            rdata(31 downto 0) <= start_index;
          when 9 =>   -- trigger_index
            rdata(31 downto 0) <= trigger_index;
          when 10 =>   -- trigger_time_lo
            rdata(31 downto 0) <= trigger_time_lo;
          when 11 =>   -- trigger_time_hi
            rdata(31 downto 0) <= trigger_time_hi;
          when 12 =>   -- filter
            rdata(15 downto 0) <= filter_channels_r;
            rdata(23 downto 16) <= filter_cycles_r;
            if bus_we = '1' then
              filter_channels_r <= bus_wdata(15 downto 0);
              filter_cycles_r <= bus_wdata(23 downto 16);
            end if;
          when 13 =>   -- pulse
            rdata(1 downto 0) <= pulse_mode_r;
            rdata(4) <= pulse_level_r;
            rdata(11 downto 8) <= pulse_channel_r;
            if bus_we = '1' then
              pulse_mode_r <= bus_wdata(1 downto 0);
              pulse_level_r <= bus_wdata(4);
              pulse_channel_r <= bus_wdata(11 downto 8);
            end if;
          when 14 =>   -- pulse_min
            rdata(31 downto 0) <= pulse_min_r;
            if bus_we = '1' then
              pulse_min_r <= bus_wdata(31 downto 0);
            end if;
          when 15 =>   -- pulse_max
            rdata(31 downto 0) <= pulse_max_r;
            if bus_we = '1' then
              pulse_max_r <= bus_wdata(31 downto 0);
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
  control_run <= control_run_r;
  rate <= rate_r;
  pretrigger <= pretrigger_r;
  trig_mask <= trig_mask_r;
  trig_value <= trig_value_r;
  trig_rise <= trig_rise_r;
  trig_fall <= trig_fall_r;
  filter_channels <= filter_channels_r;
  filter_cycles <= filter_cycles_r;
  pulse_mode <= pulse_mode_r;
  pulse_level <= pulse_level_r;
  pulse_channel <= pulse_channel_r;
  pulse_min <= pulse_min_r;
  pulse_max <= pulse_max_r;
end architecture;
