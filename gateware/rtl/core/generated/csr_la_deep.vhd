-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック la_deep（0x00000700, 128 バイト）: ロジアナの深いバッファ（外部メモリ。docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §5）。前段の設定（rate・trig_*）は la ブロックと共通。番地・数は拍（MEM_BEAT_BYTES バイト）単位
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rsp の rdata・err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_la_deep is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    bus_req   : in  std_logic;
    bus_cmd   : in  csr_cmd_t;
    bus_rsp : out csr_rsp_t;
    control_run : out std_logic;   -- control.run（RW）
    control_rle : out std_logic;   -- control.rle（RW）
    control_stream : out std_logic;   -- control.stream（RW）
    control_ps_read : out std_logic;   -- control.ps_read（RW）
    control_pack : out std_logic_vector(2 downto 0);   -- control.pack（RW）
    control_step : out std_logic_vector(1 downto 0);   -- control.step（RW）
    control_first : out std_logic_vector(3 downto 0);   -- control.first（RW）
    control_gaps : out std_logic;   -- control.gaps（RW）
    control_rle_fmt : out std_logic;   -- control.rle_fmt（RW）
    control_rle_repeat : out std_logic;   -- control.rle_repeat（RW）
    post_beats : out std_logic_vector(31 downto 0);   -- post_beats（RW）
    status_capturing : in  std_logic;   -- status.capturing（RO）
    status_triggered : in  std_logic;   -- status.triggered（RO）
    status_done : in  std_logic;   -- status.done（RO）
    status_overrun : in  std_logic;   -- status.overrun（RO）
    status_reading : in  std_logic;   -- status.reading（RO）
    status_gap_open : in  std_logic;   -- status.gap_open（RO）
    status_trigger_slot : in  std_logic_vector(2 downto 0);   -- status.trigger_slot（RO）
    trigger_beat : in  std_logic_vector(31 downto 0);   -- trigger_beat（RO）
    written : in  std_logic_vector(31 downto 0);   -- written（RO）
    trigger_time_lo : in  std_logic_vector(31 downto 0);   -- trigger_time_lo（RO）
    trigger_time_hi : in  std_logic_vector(31 downto 0);   -- trigger_time_hi（RO）
    read_first : out std_logic_vector(31 downto 0);   -- read_first（RW）
    read_count : out std_logic_vector(31 downto 0);   -- read_count（RW）
    command_read : out std_logic;   -- command.read（PULSE: 1 を書いた次の周期に 1）
    command_read_ps : out std_logic;   -- command.read_ps（PULSE: 1 を書いた次の周期に 1）
    read_region : out std_logic;   -- read_region（RW）
    ring_beats : in  std_logic_vector(31 downto 0);   -- ring_beats（RO）
    stream_beats : in  std_logic_vector(31 downto 0);   -- stream_beats（RO）
    la_written : in  std_logic_vector(31 downto 0);   -- la_written（RO）
    ring_unread : in  std_logic_vector(31 downto 0);   -- ring_unread（RO）
    ring_peak : in  std_logic_vector(31 downto 0);   -- ring_peak（RO）
    start_time_lo : in  std_logic_vector(31 downto 0);   -- start_time_lo（RO）
    start_time_hi : in  std_logic_vector(31 downto 0);   -- start_time_hi（RO）
    info_lanes : in  std_logic_vector(3 downto 0);   -- info.lanes（RO）
    info_latency : in  std_logic_vector(7 downto 0);   -- info.latency（RO）
    info_start_valid : in  std_logic;   -- info.start_valid（RO）
    gap_last_beat : in  std_logic_vector(31 downto 0);   -- gap_last_beat（RO）
    gaps : in  std_logic_vector(31 downto 0);   -- gaps（RO）
    tick : out std_logic_vector(31 downto 0);   -- tick（RW）
    pad_bytes : in  std_logic_vector(31 downto 0);   -- pad_bytes（RO）
    keyframes : in  std_logic_vector(31 downto 0)   -- keyframes（RO）
  );
end entity;

architecture rtl of csr_la_deep is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 31;
  signal control_run_r : std_logic := '0';
  signal control_rle_r : std_logic := '0';
  signal control_stream_r : std_logic := '0';
  signal control_ps_read_r : std_logic := '0';
  signal control_pack_r : std_logic_vector(2 downto 0) := (others => '0');
  signal control_step_r : std_logic_vector(1 downto 0) := (others => '0');
  signal control_first_r : std_logic_vector(3 downto 0) := (others => '0');
  signal control_gaps_r : std_logic := '0';
  signal control_rle_fmt_r : std_logic := '0';
  signal control_rle_repeat_r : std_logic := '0';
  signal post_beats_r : std_logic_vector(31 downto 0) := (others => '0');
  signal read_first_r : std_logic_vector(31 downto 0) := (others => '0');
  signal read_count_r : std_logic_vector(31 downto 0) := (others => '0');
  signal command_read_r : std_logic := '0';
  signal command_read_ps_r : std_logic := '0';
  signal read_region_r : std_logic := '0';
  signal tick_r : std_logic_vector(31 downto 0) := (others => '0');
begin
  start <= bus_req = '1' and ack = '0';
  index <= to_integer(bus_cmd.addr(4 downto 0));

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      err   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）
      command_read_r <= '0';
      command_read_ps_r <= '0';
      if rst = '1' then
        control_run_r <= '0';
        control_rle_r <= '0';
        control_stream_r <= '0';
        control_ps_read_r <= '0';
        control_pack_r <= (others => '0');
        control_step_r <= (others => '0');
        control_first_r <= (others => '0');
        control_gaps_r <= '0';
        control_rle_fmt_r <= '0';
        control_rle_repeat_r <= '0';
        post_beats_r <= (others => '0');
        read_first_r <= (others => '0');
        read_count_r <= (others => '0');
        read_region_r <= '0';
        tick_r <= (others => '0');
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- control
            rdata(0) <= control_run_r;
            rdata(1) <= control_rle_r;
            rdata(2) <= control_stream_r;
            rdata(3) <= control_ps_read_r;
            rdata(6 downto 4) <= control_pack_r;
            rdata(9 downto 8) <= control_step_r;
            rdata(15 downto 12) <= control_first_r;
            rdata(16) <= control_gaps_r;
            rdata(17) <= control_rle_fmt_r;
            rdata(18) <= control_rle_repeat_r;
            if bus_cmd.we = '1' then
              control_run_r <= bus_cmd.wdata(0);
              control_rle_r <= bus_cmd.wdata(1);
              control_stream_r <= bus_cmd.wdata(2);
              control_ps_read_r <= bus_cmd.wdata(3);
              control_pack_r <= bus_cmd.wdata(6 downto 4);
              control_step_r <= bus_cmd.wdata(9 downto 8);
              control_first_r <= bus_cmd.wdata(15 downto 12);
              control_gaps_r <= bus_cmd.wdata(16);
              control_rle_fmt_r <= bus_cmd.wdata(17);
              control_rle_repeat_r <= bus_cmd.wdata(18);
            end if;
          when 1 =>   -- post_beats
            rdata(31 downto 0) <= post_beats_r;
            if bus_cmd.we = '1' then
              post_beats_r <= bus_cmd.wdata(31 downto 0);
            end if;
          when 2 =>   -- status
            rdata(0) <= status_capturing;
            rdata(1) <= status_triggered;
            rdata(2) <= status_done;
            rdata(3) <= status_overrun;
            rdata(4) <= status_reading;
            rdata(5) <= status_gap_open;
            rdata(10 downto 8) <= status_trigger_slot;
          when 3 =>   -- trigger_beat
            rdata(31 downto 0) <= trigger_beat;
          when 4 =>   -- written
            rdata(31 downto 0) <= written;
          when 5 =>   -- trigger_time_lo
            rdata(31 downto 0) <= trigger_time_lo;
          when 6 =>   -- trigger_time_hi
            rdata(31 downto 0) <= trigger_time_hi;
          when 7 =>   -- read_first
            rdata(31 downto 0) <= read_first_r;
            if bus_cmd.we = '1' then
              read_first_r <= bus_cmd.wdata(31 downto 0);
            end if;
          when 8 =>   -- read_count
            rdata(31 downto 0) <= read_count_r;
            if bus_cmd.we = '1' then
              read_count_r <= bus_cmd.wdata(31 downto 0);
            end if;
          when 9 =>   -- command
            if bus_cmd.we = '1' then
              command_read_r <= bus_cmd.wdata(0);
              command_read_ps_r <= bus_cmd.wdata(1);
            end if;
          when 10 =>   -- read_region
            rdata(0) <= read_region_r;
            if bus_cmd.we = '1' then
              read_region_r <= bus_cmd.wdata(0);
            end if;
          when 11 =>   -- ring_beats
            rdata(31 downto 0) <= ring_beats;
          when 12 =>   -- stream_beats
            rdata(31 downto 0) <= stream_beats;
          when 13 =>   -- la_written
            rdata(31 downto 0) <= la_written;
          when 14 =>   -- ring_unread
            rdata(31 downto 0) <= ring_unread;
          when 15 =>   -- ring_peak
            rdata(31 downto 0) <= ring_peak;
          when 16 =>   -- start_time_lo
            rdata(31 downto 0) <= start_time_lo;
          when 17 =>   -- start_time_hi
            rdata(31 downto 0) <= start_time_hi;
          when 18 =>   -- info
            rdata(3 downto 0) <= info_lanes;
            rdata(15 downto 8) <= info_latency;
            rdata(16) <= info_start_valid;
          when 19 =>   -- gap_last_beat
            rdata(31 downto 0) <= gap_last_beat;
          when 20 =>   -- gaps
            rdata(31 downto 0) <= gaps;
          when 21 =>   -- tick
            rdata(31 downto 0) <= tick_r;
            if bus_cmd.we = '1' then
              tick_r <= bus_cmd.wdata(31 downto 0);
            end if;
          when 22 =>   -- pad_bytes
            rdata(31 downto 0) <= pad_bytes;
          when 23 =>   -- keyframes
            rdata(31 downto 0) <= keyframes;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_rsp.ack   <= ack;
  bus_rsp.err   <= err;
  bus_rsp.rdata <= rdata;
  control_run <= control_run_r;
  control_rle <= control_rle_r;
  control_stream <= control_stream_r;
  control_ps_read <= control_ps_read_r;
  control_pack <= control_pack_r;
  control_step <= control_step_r;
  control_first <= control_first_r;
  control_gaps <= control_gaps_r;
  control_rle_fmt <= control_rle_fmt_r;
  control_rle_repeat <= control_rle_repeat_r;
  post_beats <= post_beats_r;
  read_first <= read_first_r;
  read_count <= read_count_r;
  command_read <= command_read_r;
  command_read_ps <= command_read_ps_r;
  read_region <= read_region_r;
  tick <= tick_r;
end architecture;
