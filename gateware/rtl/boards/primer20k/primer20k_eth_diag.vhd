-- Primer 20K の EthernetLink 版（primer20k_fprobe_eth_top）の診断: 0.1 秒ごとに、リンクの状態（udp_stack・frame_mux の diag）・
-- チャンネルの valid / ready・メッセージの数・CONTROL の要求の長さ・DAP の要求の末尾 15 バイトを、UART（115200 bps）に 2 行の
-- 文字で出す（BL702 の COM ポートで見る。立ち上げの調べもの用）。観測するだけで、どの信号も駆動しない。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.text_report_pkg.all;

entity primer20k_eth_diag is
  generic (
    CLK_HZ : positive
  );
  port (
    clk, rst   : in  std_logic;
    diag       : in  std_logic_vector(15 downto 0);    -- eth_link の diag
    -- 見るチャンネル（eth_link ⇄ fprobe_core）
    req_valid, req_ready, req_last : in std_logic;
    req_data   : in  std_logic_vector(7 downto 0);
    rsp_valid, rsp_ready, rsp_last : in std_logic;
    dreq_valid, dreq_ready, dreq_last : in std_logic;
    dreq_data  : in  std_logic_vector(7 downto 0);
    drsp_valid, drsp_ready, drsp_last : in std_logic;
    st_valid, st_ready, swo_valid, swo_ready, uo_valid, uo_ready, ui_valid, ui_ready : in std_logic;
    uart_tx    : out std_logic
  );
end entity;

architecture rtl of primer20k_eth_diag is
  constant DIAG_MSG : template_t := lit("L") & hex(0) & hex(1) & lit(" C") & hex(2) & lit(" S") & hex(3)
                                   & lit(" q") & hex(4) & lit(" r") & hex(5) & lit(" d") & hex(6) & hex(7)
                                   & lit(" n") & hex(8) & hex(9) & lit(" b") & hex(10) & lit(" t") & hex(11) & hex(12)
                                   & lit("" & CR & LF);
  constant DAP_MSG : template_t := lit("D") & hex(0) & lit(" ") & hex(1) & hex(2) & hex(3) & hex(4) & hex(5)
                                  & hex(6) & hex(7) & hex(8) & hex(9) & hex(10) & hex(11) & hex(12) & hex(13)
                                  & hex(14) & hex(15) & lit("" & CR & LF);
  type tail_t is array (0 to 14) of std_logic_vector(7 downto 0);
  signal dtail, dtail_last : tail_t := (others => (others => '0'));
  signal dbytes, dlen : unsigned(7 downto 0) := (others => '0');
  signal dfields : fields_t := (others => (others => '0'));
  signal r2_valid, r2_ready, r1_busy : std_logic := '0';
  signal r1_valid, r1_ready, d_valid2 : std_logic;
  signal r1_data, r2_data : std_logic_vector(7 downto 0);
  signal fields : fields_t := (others => (others => '0'));
  signal req_n, rsp_n, dreq_n, drsp_n : unsigned(7 downto 0) := (others => '0');
  signal req_bytes, req_len, req_len_prev, odd_n : unsigned(7 downto 0) := (others => '0');
  signal req_first, req_first_prev : std_logic_vector(7 downto 0) := (others => '0');
  constant TICKS : positive := CLK_HZ / 10;            -- 0.1 秒
  signal tick : natural range 0 to TICKS - 1 := 0;
  signal report_start, d_valid, d_ready : std_logic := '0';
  signal d_byte : std_logic_vector(7 downto 0);
begin
  -- 診断: 0.1 秒ごとに状態（frame_mux・udp_stack・チャンネルの valid/ready・メッセージの数）を出す
  process (clk)
  begin
    if rising_edge(clk) then
      report_start <= '0';
      if tick = TICKS - 1 then
        tick <= 0;
        report_start <= '1';
      else
        tick <= tick + 1;
      end if;
      if req_valid = '1' and req_ready = '1' and req_last = '1' then req_n <= req_n + 1; end if;
      -- コアが受け取った CONTROL の要求の長さ（8 = 読み、12 = 1 語の書き。それ以外を数える）
      if req_valid = '1' and req_ready = '1' then
        if req_bytes = 0 then
          req_first <= req_data;
        end if;
        if req_last = '1' then
          req_len      <= req_bytes + 1;
          req_len_prev <= req_len;
          req_first_prev <= req_first;
          req_bytes    <= (others => '0');
          if req_bytes + 1 /= 8 and req_bytes + 1 /= 12 then
            odd_n <= odd_n + 1;
          end if;
        else
          req_bytes <= req_bytes + 1;
        end if;
      end if;
      if rsp_valid = '1' and rsp_ready = '1' and rsp_last = '1' then rsp_n <= rsp_n + 1; end if;
      if dreq_valid = '1' and dreq_ready = '1' and dreq_last = '1' then dreq_n <= dreq_n + 1; end if;
      if drsp_valid = '1' and drsp_ready = '1' and drsp_last = '1' then drsp_n <= drsp_n + 1; end if;
    end if;
  end process;
  fields(0) <= diag(15 downto 8);
  fields(1) <= diag(7 downto 0);
  fields(2) <= req_valid & req_ready & rsp_valid & rsp_ready & dreq_valid & dreq_ready & drsp_valid & drsp_ready;
  fields(3) <= st_valid & st_ready & swo_valid & swo_ready & uo_valid & uo_ready & ui_valid & ui_ready;
  fields(4) <= std_logic_vector(req_n);
  fields(5) <= std_logic_vector(rsp_n);
  fields(6) <= std_logic_vector(dreq_n);
  fields(7) <= std_logic_vector(drsp_n);
  fields(8)  <= std_logic_vector(req_len_prev);
  fields(9)  <= std_logic_vector(req_len);
  fields(10) <= std_logic_vector(odd_n);
  fields(11) <= req_first_prev;
  fields(12) <= req_first;
  fields(13 to fields'high) <= (others => (others => '0'));
  -- DAP の要求の末尾 15 バイトと長さ（コアの入口で）
  process (clk)
  begin
    if rising_edge(clk) then
      if dreq_valid = '1' and dreq_ready = '1' then
        dtail <= dtail(1 to 14) & dreq_data;
        if dreq_last = '1' then
          dlen <= dbytes + 1;
          dbytes <= (others => '0');
          dtail_last <= dtail(1 to 14) & dreq_data;
        else
          dbytes <= dbytes + 1;
        end if;
      end if;
    end if;
  end process;
  dfields(0) <= std_logic_vector(dlen);
  tail_fields : for k in 0 to 14 generate
    dfields(k + 1) <= dtail_last(k);
  end generate;
  report_i : entity work.text_report
    generic map (TEMPLATE => DIAG_MSG)
    port map (clk => clk, rst => rst, start => report_start, fields => fields, busy => r1_busy,
              out_valid => r1_valid, out_ready => r1_ready, out_data => r1_data);
  -- 1 行目を出し終えたら 2 行目（busy の立ち下がり）
  process (clk)
    variable prev : std_logic := '0';
  begin
    if rising_edge(clk) then
      r2_valid <= '0';
      if prev = '1' and r1_busy = '0' then
        r2_valid <= '1';
      end if;
      prev := r1_busy;
    end if;
  end process;
  report2_i : entity work.text_report
    generic map (TEMPLATE => DAP_MSG)
    port map (clk => clk, rst => rst, start => r2_valid, fields => dfields, busy => open,
              out_valid => d_valid2, out_ready => r2_ready, out_data => r2_data);
  d_valid  <= r1_valid or d_valid2;
  d_byte   <= r1_data when r1_valid = '1' else r2_data;
  r1_ready <= d_ready;
  r2_ready <= d_ready and not r1_valid;
  diag_uart_i : entity work.uart_tx
    port map (clk => clk, rst => rst, divisor => to_unsigned(baud_divisor(CLK_HZ, 115_200), 16),
              in_valid => d_valid, in_ready => d_ready, in_data => d_byte, tx => uart_tx, busy => open);

end architecture;
