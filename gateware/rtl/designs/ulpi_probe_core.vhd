-- ULPI PHY の実機疎通確認（ボード非依存、ULPI クロックドメインで動く）。
--
-- PHY が使えるようになったら、次のレジスタ操作を順に行い、結果を UART で 1 行報告する:
--   VID/PID を読む → Scratch に A5 を書いて読む → 5A を書いて読む → Function Control を読む
--   "ulpi vid=0424 pid=0006 scratch=A5/5A fc=41\r\n"（USB3317 の正常値）
-- UART で '?' を受けると再実行する。link の NXT の扱いが 1 バイトずれていれば Scratch の値が崩れる。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.text_report_pkg.all;

entity ulpi_probe_core is
  generic (
    CLK_HZ             : positive := 60_000_000;
    BAUD               : positive := 115_200;
    STARTUP_STP_CYCLES : positive := 64
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    -- ULPI
    dir       : in  std_logic;
    nxt       : in  std_logic;
    data_i    : in  std_logic_vector(7 downto 0);
    data_o    : out std_logic_vector(7 downto 0);
    data_oe   : out std_logic;
    stp       : out std_logic;
    -- UART
    uart_rx   : in  std_logic;
    uart_tx   : out std_logic;
    -- 状態表示
    phy_ready : out std_logic
  );
end entity;

architecture rtl of ulpi_probe_core is
  constant DIVISOR : unsigned(15 downto 0) := to_unsigned(CLK_HZ / BAUD, 16);

  -- レジスタ操作の列。読み出し結果は results に順に格納する
  type op_t is record
    write : boolean;
    addr  : natural range 0 to 63;
    data  : natural range 0 to 255;
  end record;
  type op_array_t is array (natural range <>) of op_t;
  constant OPS : op_array_t := (
    (false, 16#01#, 0), (false, 16#00#, 0), (false, 16#03#, 0), (false, 16#02#, 0),
    (true, 16#16#, 16#A5#), (false, 16#16#, 0),
    (true, 16#16#, 16#5A#), (false, 16#16#, 0),
    (false, 16#04#, 0));
  constant NUM_RESULTS : natural := 7;

  constant MESSAGE : template_t :=
    lit("ulpi vid=") & hex(0) & hex(1) & lit(" pid=") & hex(2) & hex(3) &
    lit(" scratch=") & hex(4) & lit("/") & hex(5) & lit(" fc=") & hex(6) & lit(CR & LF);

  type state_t is (S_WAIT_READY, S_REG, S_REG_RELEASE, S_REPORT, S_REPORTING, S_WAIT_REQUEST);
  signal state     : state_t := S_WAIT_READY;
  signal op_index  : natural range 0 to OPS'length - 1 := 0;
  signal res_index : natural range 0 to NUM_RESULTS - 1 := 0;
  signal results   : fields_t := (others => (others => '0'));

  signal ready                : std_logic;
  signal reg_req, reg_done    : std_logic := '0';
  signal reg_rdata, reg_wdata : std_logic_vector(7 downto 0);
  signal reg_we               : std_logic;
  signal reg_addr             : std_logic_vector(5 downto 0);
  signal report_start         : std_logic := '0';
  signal report_busy          : std_logic;
  signal tx_valid, tx_ready   : std_logic;
  signal tx_byte              : std_logic_vector(7 downto 0);
  signal rx_valid             : std_logic;
  signal rx_data              : std_logic_vector(7 downto 0);
begin
  link_i : entity work.ulpi_link
    generic map (STARTUP_STP_CYCLES => STARTUP_STP_CYCLES)
    port map (
      clk => clk, rst => rst,
      dir => dir, nxt => nxt, data_i => data_i, data_o => data_o, data_oe => data_oe, stp => stp,
      phy_ready => ready, line_state => open, vbus_valid => open, rx_active => open, rx_error => open,
      rx_valid => open, rx_data => open,
      tx_valid => '0', tx_ready => open, tx_data => x"00", tx_last => '0',
      tx_aborted => open, chirp_k => '0',
      reg_req => reg_req, reg_we => reg_we, reg_addr => reg_addr, reg_wdata => reg_wdata,
      reg_done => reg_done, reg_rdata => reg_rdata);

  report_i : entity work.text_report
    generic map (TEMPLATE => MESSAGE)
    port map (clk => clk, rst => rst, start => report_start, fields => results,
              busy => report_busy, out_valid => tx_valid, out_ready => tx_ready, out_data => tx_byte);

  uart_tx_i : entity work.uart_tx
    port map (clk => clk, rst => rst, divisor => DIVISOR,
              in_valid => tx_valid, in_ready => tx_ready, in_data => tx_byte,
              tx => uart_tx, busy => open);

  uart_rx_i : entity work.uart_rx
    port map (clk => clk, rst => rst, divisor => DIVISOR, rx => uart_rx,
              out_valid => rx_valid, out_ready => '1', out_data => rx_data,
              frame_error => open, overrun => open);

  reg_we    <= '1' when OPS(op_index).write else '0';
  reg_addr  <= std_logic_vector(to_unsigned(OPS(op_index).addr, 6));
  reg_wdata <= std_logic_vector(to_unsigned(OPS(op_index).data, 8));

  process (clk)
  begin
    if rising_edge(clk) then
      report_start <= '0';
      if rst = '1' then
        state     <= S_WAIT_READY;
        reg_req   <= '0';
        op_index  <= 0;
        res_index <= 0;
        results   <= (others => (others => '0'));       -- 使わない欄も含めて明示的に（初期値は合成で無視される）
      else
        case state is
          when S_WAIT_READY =>
            if ready = '1' then
              op_index  <= 0;
              res_index <= 0;
              reg_req   <= '1';
              state     <= S_REG;
            end if;

          when S_REG =>
            if reg_done = '1' then
              reg_req <= '0';
              if not OPS(op_index).write then
                results(res_index) <= reg_rdata;
                if res_index /= NUM_RESULTS - 1 then
                  res_index <= res_index + 1;
                end if;
              end if;
              state <= S_REG_RELEASE;
            end if;

          when S_REG_RELEASE =>
            -- 4 フェーズ: reg_req を 1 周期下げてから次の操作へ
            if op_index = OPS'length - 1 then
              state <= S_REPORT;
            else
              op_index <= op_index + 1;
              reg_req  <= '1';
              state    <= S_REG;
            end if;

          when S_REPORT =>
            report_start <= '1';
            state        <= S_REPORTING;

          when S_REPORTING =>
            if report_busy = '0' and report_start = '0' then
              state <= S_WAIT_REQUEST;
            end if;

          when S_WAIT_REQUEST =>
            if rx_valid = '1' and rx_data = std_logic_vector(to_unsigned(character'pos('?'), 8)) then
              state <= S_WAIT_READY;
            end if;
        end case;
      end if;
    end if;
  end process;

  phy_ready <= ready;
end architecture;
