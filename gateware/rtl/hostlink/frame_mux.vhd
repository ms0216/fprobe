-- フレームのリンク（SerialLink・EthernetLink）に共通の、論理チャンネルの多重化（ボード・リンク非依存、clk のドメイン）。
-- 仕様: docs/superpowers/specs/2026-10-17-m11-serial-link-design.md §2、2026-10-17-m11-ethernet-link-design.md §3
--
-- フレーム = ch(1) | payload(0..MAX_PAYLOAD)。ch: 0 = CONTROL、1 = DAP（1 フレーム = 1 パケット）、2 = SWO、3 = UART0、
-- 4 = STREAM（バイト列）、5 = LINE_CODING（PC → FPGA のみ。payload = ボーレート u32 LE）。
-- リンクはフレームをどう運ぶか（COBS + CRC の UART、UDP 等）だけを受け持ち、検査済みのフレームをここと受け渡す。
--
-- 受信: リンクが正しいフレームを受信バッファ（番地 0 = ch、1〜 = payload）に書き、rx_commit（1 周期、登録出力から）と
--   rx_len（ch を含むバイト数）で渡す。渡したフレームを流し終えるまで rx_busy = 1（commit の周期から）で、その間
--   リンクは書き込み・commit をしない。payload の無いフレーム・未知のチャンネルは捨てる。
-- 送信: 優先順（CONTROL > DAP > UART0 > SWO > STREAM）で 1 チャンネルを選び、1 フレーム分（パケットなら last まで、
--   バイト列なら最大 MAX_PAYLOAD バイトか、IDLE_CYCLES 来なくなるまで）を送信バッファに組み立て、tx_valid と
--   tx_len（ch を含む）で知らせる。リンクは tx_raddr で読み（tx_rdata は 1 周期後）、送り終えたら tx_done（1 周期）。
--   CONTROL の応答は USB の EP1 と同じく MAX_PAYLOAD のフレームを続け、それ未満のフレームで終える（ちょうど倍数で
--   終わったら空のフレームを足す）。
-- PC → ターゲットの UART0 は UART_DEPTH バイトの FIFO で受ける（ターゲットのボーレートで少しずつ出ていく）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity frame_mux is
  generic (
    MAX_PAYLOAD : positive := 512;
    RAM_BITS    : positive := 10;         -- 受信・送信バッファの番地幅（2**RAM_BITS ≥ リンクが書く最大バイト数）
    IDLE_CYCLES : positive := 64;         -- バイト列のフレームを閉じる、入力が途切れた周期数
    UART_DEPTH  : positive := 512         -- PC → ターゲットの UART0 の FIFO
  );
  port (
    clk             : in  std_logic;
    rst             : in  std_logic;
    -- リンク側: 受信バッファ
    rx_we           : in  std_logic;
    rx_waddr        : in  unsigned(RAM_BITS - 1 downto 0);
    rx_wdata        : in  std_logic_vector(7 downto 0);
    rx_commit       : in  std_logic;
    rx_len          : in  natural range 0 to MAX_PAYLOAD + 1;
    rx_busy         : out std_logic;
    -- リンク側: 送信バッファ
    tx_valid        : out std_logic;
    tx_len          : out natural range 1 to MAX_PAYLOAD + 1;
    tx_raddr        : in  unsigned(RAM_BITS - 1 downto 0);
    tx_rdata        : out std_logic_vector(7 downto 0);
    tx_done         : in  std_logic;
    -- CONTROL（ctrl_out: PC からの要求、ctrl_in: 応答）
    ctrl_out_valid  : out std_logic;
    ctrl_out_ready  : in  std_logic;
    ctrl_out_data   : out std_logic_vector(7 downto 0);
    ctrl_out_last   : out std_logic;
    ctrl_in_valid   : in  std_logic;
    ctrl_in_ready   : out std_logic;
    ctrl_in_data    : in  std_logic_vector(7 downto 0);
    ctrl_in_last    : in  std_logic;
    -- CMSIS-DAP
    dap_out_valid   : out std_logic;
    dap_out_ready   : in  std_logic;
    dap_out_data    : out std_logic_vector(7 downto 0);
    dap_out_last    : out std_logic;
    dap_in_valid    : in  std_logic;
    dap_in_ready    : out std_logic;
    dap_in_data     : in  std_logic_vector(7 downto 0);
    dap_in_last     : in  std_logic;
    swo_in_valid    : in  std_logic;
    swo_in_ready    : out std_logic;
    swo_in_data     : in  std_logic_vector(7 downto 0);
    -- ストリーム
    stream_in_valid : in  std_logic;
    stream_in_ready : out std_logic;
    stream_in_data  : in  std_logic_vector(7 downto 0);
    -- UART0（ターゲットの UART）
    uart_out_valid  : out std_logic;
    uart_out_ready  : in  std_logic;
    uart_out_data   : out std_logic_vector(7 downto 0);
    uart_in_valid   : in  std_logic;
    uart_in_ready   : out std_logic;
    uart_in_data    : in  std_logic_vector(7 downto 0);
    line_rate       : out unsigned(31 downto 0);
    line_changed    : out std_logic;
    -- 状態（診断用）: [7] 受信中、[6:5] 送信の状態、[4:2] 送信のチャンネル、[1] 空フレーム待ち
    diag            : out std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of frame_mux is
  constant FRAME_MAX  : positive := MAX_PAYLOAD + 1;        -- ch + payload
  constant CH_CONTROL : natural := 0;
  constant CH_DAP     : natural := 1;
  constant CH_SWO     : natural := 2;
  constant CH_UART    : natural := 3;
  constant CH_STREAM  : natural := 4;
  constant CH_LINE    : natural := 5;

  -- 受信
  type rx_state_t is (R_IDLE, R_DELIVER);
  signal rx_state  : rx_state_t := R_IDLE;
  signal rx_ch     : natural range 0 to 255 := 0;
  signal rd_idx    : natural range 0 to FRAME_MAX := 0;     -- 0 = ch、1〜rd_last = payload
  signal rd_last   : natural range 0 to FRAME_MAX := 0;
  signal rd_data   : std_logic_vector(7 downto 0) := (others => '0');
  signal ra        : unsigned(RAM_BITS - 1 downto 0) := (others => '0');
  signal rq        : std_logic_vector(7 downto 0);
  signal dphase    : natural range 0 to 3 := 0;             -- 1 バイトの手順: 番地 → 待ち → 取る → 受理待ち
  signal line_sr   : std_logic_vector(31 downto 0) := (others => '0');
  signal out_valid : std_logic := '0';
  signal out_ready : std_logic;
  signal line_r    : unsigned(31 downto 0) := (others => '0');
  signal line_p    : std_logic := '0';
  signal u_valid, u_ready : std_logic;

  -- 送信
  signal tx_we : std_logic := '0';
  signal tx_wa : unsigned(RAM_BITS - 1 downto 0) := (others => '0');
  signal tx_wd : std_logic_vector(7 downto 0) := (others => '0');
  type tx_state_t is (T_IDLE, T_FILL, T_SETTLE, T_READY);
  signal tx_state  : tx_state_t := T_IDLE;
  signal tx_ch     : natural range 0 to 7 := 0;
  signal tx_n      : natural range 1 to FRAME_MAX := 1;
  signal idle_cnt  : natural range 0 to IDLE_CYCLES := 0;
  signal src_valid, src_last, take : std_logic;
  signal src_data  : std_logic_vector(7 downto 0);
  signal zlp_due   : boolean := false;                      -- CONTROL の空フレーム（終わりの印）を送る
  -- 送信の入口のレジスタスライス（コア側の valid・ready と、チャンネルの選択・take の組み合わせ回路を切る）
  signal xctrl_in_valid, xctrl_in_ready : std_logic;
  signal xctrl_in_data : std_logic_vector(7 downto 0);
  signal xctrl_in_last : std_logic;
  signal xctrl_in_word : std_logic_vector(8 downto 0);
  signal xdap_in_valid, xdap_in_ready : std_logic;
  signal xdap_in_data : std_logic_vector(7 downto 0);
  signal xdap_in_last : std_logic;
  signal xdap_in_word : std_logic_vector(8 downto 0);
  signal xuart_in_valid, xuart_in_ready : std_logic;
  signal xuart_in_data : std_logic_vector(7 downto 0);
  signal xswo_in_valid, xswo_in_ready : std_logic;
  signal xswo_in_data : std_logic_vector(7 downto 0);
  signal xstream_in_valid, xstream_in_ready : std_logic;
  signal xstream_in_data : std_logic_vector(7 downto 0);

begin

  ---------------------------------------------------------------------------------------------
  -- 送信の入口: チャンネルごとのレジスタスライス（コアの出力の組み合わせ回路が take を通って他のチャンネルの
  -- ready までつながると、sys の最も長い経路になる。2026-10-17 に Ethernet 版で配置次第の不具合として表れた）
  ---------------------------------------------------------------------------------------------
  ctrl_in_slice_i : entity work.stream_slice
    generic map (WIDTH => 9)
    port map (clk => clk, rst => rst, in_valid => ctrl_in_valid, in_ready => ctrl_in_ready,
              in_data => ctrl_in_last & ctrl_in_data, out_valid => xctrl_in_valid, out_ready => xctrl_in_ready,
              out_data => xctrl_in_word);
  xctrl_in_data <= xctrl_in_word(7 downto 0);
  xctrl_in_last <= xctrl_in_word(8);
  dap_in_slice_i : entity work.stream_slice
    generic map (WIDTH => 9)
    port map (clk => clk, rst => rst, in_valid => dap_in_valid, in_ready => dap_in_ready,
              in_data => dap_in_last & dap_in_data, out_valid => xdap_in_valid, out_ready => xdap_in_ready,
              out_data => xdap_in_word);
  xdap_in_data <= xdap_in_word(7 downto 0);
  xdap_in_last <= xdap_in_word(8);
  uart_in_slice_i : entity work.stream_slice
    generic map (WIDTH => 8)
    port map (clk => clk, rst => rst, in_valid => uart_in_valid, in_ready => uart_in_ready,
              in_data => uart_in_data, out_valid => xuart_in_valid, out_ready => xuart_in_ready,
              out_data => xuart_in_data);
  swo_in_slice_i : entity work.stream_slice
    generic map (WIDTH => 8)
    port map (clk => clk, rst => rst, in_valid => swo_in_valid, in_ready => swo_in_ready,
              in_data => swo_in_data, out_valid => xswo_in_valid, out_ready => xswo_in_ready,
              out_data => xswo_in_data);
  stream_in_slice_i : entity work.stream_slice
    generic map (WIDTH => 8)
    port map (clk => clk, rst => rst, in_valid => stream_in_valid, in_ready => stream_in_ready,
              in_data => stream_in_data, out_valid => xstream_in_valid, out_ready => xstream_in_ready,
              out_data => xstream_in_data);
  line_rate    <= line_r;
  process (all)
  begin
    diag <= '0' & std_logic_vector(to_unsigned(tx_state_t'pos(tx_state), 2)) & std_logic_vector(to_unsigned(tx_ch, 3))
            & "00";
    if rx_state = R_DELIVER then
      diag(7) <= '1';
    end if;
    if zlp_due then
      diag(1) <= '1';
    end if;
  end process;
  line_changed <= line_p;
  rx_busy      <= '1' when rx_state /= R_IDLE or rx_commit = '1' else '0';

  ---------------------------------------------------------------------------------------------
  -- 受信: バッファのフレームを該当チャンネルへ流す
  ---------------------------------------------------------------------------------------------
  process (clk)
  begin
    if rising_edge(clk) then
      line_p <= '0';
      if rst = '1' then
        rx_state  <= R_IDLE;
        out_valid <= '0';
      else
        case rx_state is
          when R_IDLE =>
            if rx_commit = '1' and rx_len >= 2 then
              rd_last  <= rx_len - 1;
              rd_idx   <= 0;
              dphase   <= 0;
              rx_state <= R_DELIVER;
            end if;

          when R_DELIVER =>
            case dphase is
              when 0 =>
                ra     <= to_unsigned(rd_idx, RAM_BITS);
                dphase <= 1;
              when 1 =>                                        -- rq に読み出される
                dphase <= 2;
              when 2 =>
                if rd_idx = 0 then                             -- ch
                  rx_ch  <= to_integer(unsigned(rq));
                  rd_idx <= 1;
                  dphase <= 0;
                  if to_integer(unsigned(rq)) > CH_LINE then
                    rx_state <= R_IDLE;                        -- 未知のチャンネル
                  end if;
                elsif rx_ch = CH_LINE then                     -- 流さずにボーレートを取り込む（1〜4 バイト目、LE）
                  if rd_idx <= 4 then
                    line_sr <= rq & line_sr(31 downto 8);
                  end if;
                  if rd_idx = rd_last then
                    if rd_last = 4 then
                      line_r <= unsigned(rq & line_sr(31 downto 8));
                      line_p <= '1';
                    elsif rd_last > 4 then
                      line_r <= unsigned(line_sr);
                      line_p <= '1';
                    end if;
                    rx_state <= R_IDLE;
                  else
                    rd_idx <= rd_idx + 1;
                    dphase <= 0;
                  end if;
                else
                  rd_data   <= rq;
                  out_valid <= '1';
                  dphase    <= 3;
                end if;
              when others =>
                if out_ready = '1' then
                  out_valid <= '0';
                  dphase    <= 0;
                  if rd_idx = rd_last then
                    rx_state <= R_IDLE;
                  else
                    rd_idx <= rd_idx + 1;
                  end if;
                end if;
            end case;
        end case;
      end if;
    end if;
  end process;

  rx_ram_i : entity work.sdp_ram
    generic map (WIDTH => 8, DEPTH => 2 ** RAM_BITS)
    port map (clk => clk, we => rx_we, waddr => rx_waddr, wdata => rx_wdata, raddr => ra, rdata => rq);

  ctrl_out_valid <= out_valid when rx_ch = CH_CONTROL else '0';
  dap_out_valid  <= out_valid when rx_ch = CH_DAP else '0';
  u_valid        <= out_valid when rx_ch = CH_UART else '0';
  uart_fifo_i : entity work.sync_fifo
    generic map (WIDTH => 8, DEPTH => UART_DEPTH)
    port map (clk => clk, rst => rst, in_valid => u_valid, in_ready => u_ready, in_data => rd_data,
              out_valid => uart_out_valid, out_ready => uart_out_ready, out_data => uart_out_data, level => open);
  ctrl_out_data  <= rd_data;
  dap_out_data   <= rd_data;
  ctrl_out_last  <= '1' when rd_idx = rd_last else '0';
  dap_out_last   <= '1' when rd_idx = rd_last else '0';
  with rx_ch select out_ready <=
    ctrl_out_ready when CH_CONTROL,
    dap_out_ready  when CH_DAP,
    u_ready        when CH_UART,
    '1'            when others;                                -- PC → FPGA の無いチャンネルは捨てる

  ---------------------------------------------------------------------------------------------
  -- 送信: 1 フレーム分をバッファに組み立ててリンクに渡す
  ---------------------------------------------------------------------------------------------
  with tx_ch select src_valid <=
    xctrl_in_valid   when CH_CONTROL,
    xdap_in_valid    when CH_DAP,
    xuart_in_valid   when CH_UART,
    xswo_in_valid    when CH_SWO,
    xstream_in_valid when others;
  with tx_ch select src_data <=
    xctrl_in_data    when CH_CONTROL,
    xdap_in_data     when CH_DAP,
    xuart_in_data    when CH_UART,
    xswo_in_data     when CH_SWO,
    xstream_in_data  when others;
  with tx_ch select src_last <=
    xctrl_in_last    when CH_CONTROL,
    xdap_in_last     when CH_DAP,
    '0'             when others;
  take <= '1' when tx_state = T_FILL and src_valid = '1' and tx_n < FRAME_MAX and not zlp_due else '0';
  xctrl_in_ready   <= take when tx_ch = CH_CONTROL else '0';
  xdap_in_ready    <= take when tx_ch = CH_DAP else '0';
  xuart_in_ready   <= take when tx_ch = CH_UART else '0';
  xswo_in_ready    <= take when tx_ch = CH_SWO else '0';
  xstream_in_ready <= take when tx_ch = CH_STREAM else '0';
  tx_valid <= '1' when tx_state = T_READY else '0';
  tx_len   <= tx_n;

  process (clk)
    variable we : boolean;
    variable wa : natural range 0 to FRAME_MAX - 1;
    variable wd : std_logic_vector(7 downto 0);
    variable ch : natural range 0 to 7;
  begin
    if rising_edge(clk) then
      we := false;
      wa := 0;
      wd := (others => '0');
      if rst = '1' then
        tx_state <= T_IDLE;
        zlp_due  <= false;
      else
        case tx_state is
          when T_IDLE =>
            tx_n     <= 1;
            idle_cnt <= 0;
            -- 優先順: CONTROL > DAP > UART0 > SWO > STREAM
            if zlp_due or xctrl_in_valid = '1' then
              ch := CH_CONTROL;                                -- zlp_due: 空の CONTROL フレーム
            elsif xdap_in_valid = '1' then
              ch := CH_DAP;
            elsif xuart_in_valid = '1' then
              ch := CH_UART;
            elsif xswo_in_valid = '1' then
              ch := CH_SWO;
            else
              ch := CH_STREAM;
            end if;
            tx_ch <= ch;
            we := true;                                        -- 先頭はチャンネル番号
            wd := std_logic_vector(to_unsigned(ch, 8));
            if zlp_due then
              zlp_due  <= false;
              tx_state <= T_SETTLE;                            -- ch だけのフレーム
            elsif (xctrl_in_valid or xdap_in_valid or xuart_in_valid or xswo_in_valid or xstream_in_valid) = '1' then
              tx_state <= T_FILL;
            end if;

          when T_FILL =>
            if take = '1' then
              we := true;
              wa := tx_n;
              wd := src_data;
              tx_n     <= tx_n + 1;
              idle_cnt <= 0;
              if src_last = '1' or tx_n + 1 = FRAME_MAX then
                tx_state <= T_SETTLE;
              end if;
              if src_last = '1' and tx_n + 1 = FRAME_MAX and tx_ch = CH_CONTROL then
                zlp_due <= true;                               -- ちょうど MAX_PAYLOAD で終わった
              end if;
            elsif tx_ch >= CH_SWO and tx_n > 1 then
              if idle_cnt = IDLE_CYCLES then                   -- バイト列: 入力が途切れたら閉じる
                tx_state <= T_SETTLE;
              else
                idle_cnt <= idle_cnt + 1;
              end if;
            end if;

          when T_SETTLE =>                                     -- 最後の書き込み（1 周期遅れ）がバッファに入るのを待つ
            tx_state <= T_READY;

          when T_READY =>
            if tx_done = '1' then
              tx_state <= T_IDLE;
            end if;
        end case;
      end if;
      tx_we <= '1' when we else '0';
      tx_wa <= to_unsigned(wa, RAM_BITS);
      tx_wd <= wd;
    end if;
  end process;

  tx_ram_i : entity work.sdp_ram
    generic map (WIDTH => 8, DEPTH => 2 ** RAM_BITS)
    port map (clk => clk, we => tx_we, waddr => tx_wa, wdata => tx_wd, raddr => tx_raddr, rdata => tx_rdata);
end architecture;
