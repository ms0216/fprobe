-- SerialLink: 論理チャンネルを UART 1 本で多重化するホストリンク（ボード非依存、clk のドメイン）。
-- 仕様: docs/superpowers/specs/2026-10-17-m11-serial-link-design.md
--
-- 線上: … 0x00 | COBS( ch | payload(0..MAX_PAYLOAD) | crc16 LE ) | 0x00 …（CRC-16/CCITT-FALSE、ch と payload に対して）
-- ch: 0 = CONTROL、1 = DAP（1 フレーム = 1 パケット）、2 = SWO、3 = UART0、4 = STREAM（バイト列）、
--     5 = LINE_CODING（PC → FPGA のみ。payload = ボーレート u32 LE。CDC の SET_LINE_CODING の代わり）。
-- コア側の I/F は usb_device と同じ（ctrl / dap はパケットの終わりを last で示す）。
--
-- チャンネルの多重化・フレームのバッファは frame_mux（EthernetLink と共通）。ここは COBS + CRC16 + UART だけを受け持つ。
-- 受信: UART → FIFO → COBS の復号 → frame_mux の受信バッファ（CRC が合ったフレームだけ渡す）。frame_mux が流している
--   間は復号を止める（FIFO が受け持つ）。合わないフレーム・長すぎるフレームは捨てて数える。
-- 送信: frame_mux が組み立てたフレームの CRC を計算し（1 回読む）、ch・payload・CRC を COBS で送る。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity serial_link is
  generic (
    CLK_HZ      : positive;
    BAUD        : positive := 2_000_000;
    MAX_PAYLOAD : positive := 512;
    IDLE_CYCLES : positive := 64;         -- バイト列のフレームを閉じる、入力が途切れた周期数
    UART_DEPTH  : positive := 512         -- PC → ターゲットの UART0 の FIFO
  );
  port (
    clk             : in  std_logic;
    rst             : in  std_logic;
    rx              : in  std_logic;
    tx              : out std_logic;
    -- 状態
    bad_frames      : out unsigned(15 downto 0);           -- CRC・長さの異常で捨てたフレームの数
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
    line_changed    : out std_logic
  );
end entity;

architecture rtl of serial_link is
  constant DIVISOR_BITS : positive := 16;
  constant DIVISOR      : unsigned(DIVISOR_BITS - 1 downto 0) := to_unsigned((CLK_HZ + BAUD / 2) / BAUD, DIVISOR_BITS);
  constant FRAME_MAX    : positive := MAX_PAYLOAD + 3;      -- ch + payload + crc
  constant RAM_BITS     : positive := clog2(FRAME_MAX);

  -- CRC-16/CCITT-FALSE の 1 バイト分
  function crc_next(crc : std_logic_vector(15 downto 0); b : std_logic_vector(7 downto 0)) return std_logic_vector is
    variable c : std_logic_vector(15 downto 0) := crc;
  begin
    c(15 downto 8) := c(15 downto 8) xor b;
    for i in 0 to 7 loop
      if c(15) = '1' then
        c := (c(14 downto 0) & '0') xor x"1021";
      else
        c := c(14 downto 0) & '0';
      end if;
    end loop;
    return c;
  end function;

  -- 受信
  signal rxb_valid, rxb_ready : std_logic;
  signal rxb_data : std_logic_vector(7 downto 0);
  signal rxf_valid, rxf_ready : std_logic;
  signal rxf_data : std_logic_vector(7 downto 0);
  signal rx_we : std_logic := '0';                          -- 受信バッファへの書き込み（1 周期遅れで書く）
  signal rx_wa : unsigned(RAM_BITS - 1 downto 0) := (others => '0');
  signal rx_wd : std_logic_vector(7 downto 0) := (others => '0');
  signal rx_len : natural range 0 to FRAME_MAX := 0;        -- 復号したバイト数
  signal code_left : natural range 0 to 255 := 0;           -- 今のブロックの残り（0 = 次はコード）
  signal zero_due  : boolean := false;                      -- 次のブロックの前に 0 を入れる
  signal overflow  : boolean := false;
  signal rx_crc    : std_logic_vector(15 downto 0) := x"FFFF";
  signal d1, d2    : std_logic_vector(7 downto 0) := (others => '0');   -- 直近の 2 バイト（CRC に入れるのを遅らせる）
  signal bad_r     : unsigned(15 downto 0) := (others => '0');
  signal commit    : std_logic := '0';
  signal commit_len : natural range 0 to MAX_PAYLOAD + 1 := 0;
  signal rx_busy   : std_logic;

  -- 送信
  type tx_state_t is (T_IDLE, T_CRC_WAIT, T_CRC, T_WAIT, T_SCAN, T_CODE, T_DATA_WAIT, T_DATA, T_LAST_CODE, T_DELIM);
  signal tx_state  : tx_state_t := T_IDLE;
  signal f_valid   : std_logic;
  signal f_len     : natural range 1 to MAX_PAYLOAD + 1;    -- ch + payload（CRC を除く）
  signal f_q       : std_logic_vector(7 downto 0);          -- frame_mux の送信バッファの同期読み出し（番地 scan）
  signal f_done    : std_logic := '0';
  signal tx_len    : natural range 0 to FRAME_MAX := 0;     -- CRC を含むバイト数
  signal tx_crc    : std_logic_vector(15 downto 0) := x"FFFF";
  signal pos, scan, blk_end : natural range 0 to FRAME_MAX := 0;
  signal hit_zero  : boolean := false;
  signal tx_q      : std_logic_vector(7 downto 0);          -- フレーム(scan)。CRC の 2 バイトは末尾に足して見せる
  signal txb_valid, txb_ready : std_logic := '0';
  signal txb_data  : std_logic_vector(7 downto 0) := (others => '0');
begin
  bad_frames <= bad_r;

  mux_i : entity work.frame_mux
    generic map (MAX_PAYLOAD => MAX_PAYLOAD, RAM_BITS => RAM_BITS, IDLE_CYCLES => IDLE_CYCLES,
                 UART_DEPTH => UART_DEPTH)
    port map (clk => clk, rst => rst,
              rx_we => rx_we, rx_waddr => rx_wa, rx_wdata => rx_wd, rx_commit => commit, rx_len => commit_len,
              rx_busy => rx_busy,
              tx_valid => f_valid, tx_len => f_len, tx_raddr => to_unsigned(scan, RAM_BITS), tx_rdata => f_q,
              tx_done => f_done,
              ctrl_out_valid => ctrl_out_valid, ctrl_out_ready => ctrl_out_ready, ctrl_out_data => ctrl_out_data,
              ctrl_out_last => ctrl_out_last,
              ctrl_in_valid => ctrl_in_valid, ctrl_in_ready => ctrl_in_ready, ctrl_in_data => ctrl_in_data,
              ctrl_in_last => ctrl_in_last,
              dap_out_valid => dap_out_valid, dap_out_ready => dap_out_ready, dap_out_data => dap_out_data,
              dap_out_last => dap_out_last,
              dap_in_valid => dap_in_valid, dap_in_ready => dap_in_ready, dap_in_data => dap_in_data,
              dap_in_last => dap_in_last,
              swo_in_valid => swo_in_valid, swo_in_ready => swo_in_ready, swo_in_data => swo_in_data,
              stream_in_valid => stream_in_valid, stream_in_ready => stream_in_ready, stream_in_data => stream_in_data,
              uart_out_valid => uart_out_valid, uart_out_ready => uart_out_ready, uart_out_data => uart_out_data,
              uart_in_valid => uart_in_valid, uart_in_ready => uart_in_ready, uart_in_data => uart_in_data,
              line_rate => line_rate, line_changed => line_changed, diag => open);

  ---------------------------------------------------------------------------------------------
  -- 受信: UART → FIFO（frame_mux が流している間の受け持ち）→ COBS の復号 → 受信バッファ
  ---------------------------------------------------------------------------------------------
  rx_i : entity work.uart_rx
    generic map (DIVISOR_BITS => DIVISOR_BITS)
    port map (clk => clk, rst => rst, divisor => DIVISOR, rx => rx, out_valid => rxb_valid, out_ready => rxb_ready,
              out_data => rxb_data, frame_error => open, overrun => open);
  rx_fifo_i : entity work.sync_fifo
    generic map (WIDTH => 8, DEPTH => 64)
    port map (clk => clk, rst => rst, in_valid => rxb_valid, in_ready => rxb_ready, in_data => rxb_data,
              out_valid => rxf_valid, out_ready => rxf_ready, out_data => rxf_data, level => open);
  rxf_ready <= not rx_busy;

  process (clk)
    variable b : std_logic_vector(7 downto 0);

    procedure put(v : std_logic_vector(7 downto 0)) is
    begin
      if rx_len = FRAME_MAX then
        overflow <= true;
      else
        rx_we <= '1';
        rx_wa <= to_unsigned(rx_len, RAM_BITS);
        rx_wd <= v;
        rx_len <= rx_len + 1;
        if rx_len >= 2 then
          rx_crc <= crc_next(rx_crc, d2);                       -- 2 バイト前（最後の 2 バイト = CRC は入れない）
        end if;
        d2 <= d1;
        d1 <= v;
      end if;
    end procedure;

    procedure restart is
    begin
      rx_len    <= 0;
      code_left <= 0;
      zero_due  <= false;
      overflow  <= false;
      rx_crc    <= x"FFFF";
    end procedure;
  begin
    if rising_edge(clk) then
      rx_we  <= '0';
      commit <= '0';
      if rst = '1' then
        restart;
        bad_r <= (others => '0');
      elsif rxf_valid = '1' and rx_busy = '0' then
        b := rxf_data;
        if b = x"00" then                                      -- フレームの終わり
          if rx_len >= 3 and not overflow and rx_crc = d1 & d2 then
            commit     <= '1';
            commit_len <= rx_len - 2;
          elsif rx_len /= 0 or overflow then
            if bad_r /= x"FFFF" then
              bad_r <= bad_r + 1;
            end if;
          end if;
          restart;
        elsif code_left = 0 then                               -- ブロックの頭（コード）
          if zero_due then
            put(x"00");
          end if;
          code_left <= to_integer(unsigned(b)) - 1;
          zero_due  <= b /= x"FF";
        else
          put(b);
          code_left <= code_left - 1;
        end if;
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------------------------
  -- 送信: frame_mux のフレームの CRC を計算 → ch・payload・CRC を COBS で UART へ
  ---------------------------------------------------------------------------------------------
  -- COBS: 各ブロック = コード（ブロック内のデータ数 + 1）+ データ（0 を含まない、最大 254）。ブロックは 0 か 254 バイトか
  -- 終わりで切れる。0 で切れたら、その 0 は次のブロックのコードで表す（データとしては送らない）。
  -- ブロックの範囲を先に調べ（T_SCAN、バッファは 1 周期遅れで読める）、コード → データの順に送る。
  tx_q <= f_q                when scan < f_len else
          tx_crc(7 downto 0) when scan = f_len else
          tx_crc(15 downto 8);

  process (clk)
  begin
    if rising_edge(clk) then
      f_done <= '0';
      if txb_ready = '1' then
        txb_valid <= '0';
      end if;
      if rst = '1' then
        tx_state  <= T_IDLE;
        txb_valid <= '0';
      else
        case tx_state is
          when T_IDLE =>
            scan   <= 0;
            tx_crc <= x"FFFF";
            if f_valid = '1' and f_done = '0' then             -- f_done の次の周期はまだ前のフレームの valid
              tx_len   <= f_len + 2;
              tx_state <= T_CRC_WAIT;
            end if;

          when T_CRC_WAIT =>                                   -- バッファ(scan) が f_q に出るのを待つ
            tx_state <= T_CRC;

          when T_CRC =>
            tx_crc <= crc_next(tx_crc, f_q);
            if scan + 1 = f_len then
              pos      <= 0;
              scan     <= 0;
              tx_state <= T_WAIT;
            else
              scan     <= scan + 1;
              tx_state <= T_CRC_WAIT;
            end if;

          when T_WAIT =>                                       -- バッファ(scan) が tx_q に出るのを待つ
            tx_state <= T_SCAN;

          when T_SCAN =>
            -- tx_q = フレーム(scan)。scan がブロックの終わり（0・254 バイト・フレームの終わり）なら確定
            if scan = tx_len then
              blk_end  <= scan;
              hit_zero <= false;
              tx_state <= T_CODE;
            elsif tx_q = x"00" then
              blk_end  <= scan;
              hit_zero <= true;
              tx_state <= T_CODE;
            elsif scan - pos = 253 then
              blk_end  <= scan + 1;                            -- 254 バイトで切る（0 ではない）
              hit_zero <= false;
              tx_state <= T_CODE;
            else
              scan     <= scan + 1;
              tx_state <= T_WAIT;
            end if;

          when T_CODE =>
            if txb_valid = '0' or txb_ready = '1' then
              txb_data  <= std_logic_vector(to_unsigned(blk_end - pos + 1, 8));
              txb_valid <= '1';
              scan      <= pos;
              tx_state  <= T_DATA_WAIT;
            end if;

          when T_DATA_WAIT =>
            tx_state <= T_DATA;

          when T_DATA =>
            if scan = blk_end then                             -- ブロックのデータを送り終えた
              if hit_zero then
                pos  <= blk_end + 1;                           -- 0 は次のコードで表す
                scan <= blk_end + 1;
                if blk_end + 1 = tx_len then
                  tx_state <= T_LAST_CODE;                     -- 最後が 0: 空のブロックを足す
                else
                  tx_state <= T_WAIT;
                end if;
              elsif blk_end - pos = 254 and blk_end /= tx_len then
                pos      <= blk_end;                           -- 254 バイトで切った: 続きのブロック
                scan     <= blk_end;
                tx_state <= T_WAIT;
              else
                tx_state <= T_DELIM;
              end if;
            elsif txb_valid = '0' or txb_ready = '1' then
              txb_data  <= tx_q;
              txb_valid <= '1';
              scan      <= scan + 1;
              tx_state  <= T_DATA_WAIT;
            end if;

          when T_LAST_CODE =>
            if txb_valid = '0' or txb_ready = '1' then
              txb_data  <= x"01";
              txb_valid <= '1';
              tx_state  <= T_DELIM;
            end if;

          when T_DELIM =>
            if txb_valid = '0' or txb_ready = '1' then
              txb_data  <= x"00";
              txb_valid <= '1';
              f_done    <= '1';
              tx_state  <= T_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;

  tx_i : entity work.uart_tx
    generic map (DIVISOR_BITS => DIVISOR_BITS)
    port map (clk => clk, rst => rst, divisor => DIVISOR, in_valid => txb_valid, in_ready => txb_ready,
              in_data => txb_data, tx => tx, busy => open);
end architecture;
