-- CMSIS-DAP の SWO コマンド: SWO_Transport / Mode / Baudrate / Control / Status / Data。
-- 転送はなし(0)・エンドポイント(2)、方式はなし(0)・UART(1)・Manchester(2。HAS_MANCHESTER の構成だけ)。
-- 受信中は設定を変えられない（Mode だけは受けて受信を止める）。
-- 仕様: docs/superpowers/specs/2026-10-16-m6b-swo-design.md、2026-10-16-dap-processor-split-design.md、
--       2026-10-19-u51-u52-swo-uart-design.md
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.dap_pkg.all;

entity dap_cmd_swo is
  generic (
    CLK_HZ         : positive;
    HAS_MANCHESTER : boolean := true      -- Manchester 方式の受信回路がある（swo_receiver の HAS_MANCHESTER と同じ値）
  );
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    -- dap_packet との I/F
    command     : in  natural range 0 to 255;
    start       : in  std_logic;
    done        : out std_logic;
    get_req     : out std_logic;
    get_n       : out natural range 1 to 8;
    get_ack     : in  std_logic;
    arg         : in  std_logic_vector(63 downto 0);
    put_req     : out std_logic;
    put_n       : out natural range 1 to 8;
    put_val     : out std_logic_vector(63 downto 0);
    put_ack     : in  std_logic;
    -- SWO 受信器
    swo_run     : out std_logic;
    swo_divisor : out unsigned(15 downto 0);                 -- 1 ビットの clk 周期数
    swo_manchester : out std_logic;                          -- 方式: '1' = Manchester、'0' = UART
    swo_error   : in  std_logic;
    swo_overrun : in  std_logic;
    -- 割り算器（dap_processor が共有する udiv。div_start の周期に div_num / div_den を取り込む）
    div_start : out std_logic := '0';
    div_num   : out unsigned(31 downto 0) := (others => '0');
    div_den   : out unsigned(31 downto 0) := (others => '0');
    div_done  : in  std_logic;
    div_quot  : in  unsigned(31 downto 0)
  );
end entity;

architecture rtl of dap_cmd_swo is
  type state_t is (S_IDLE, S_DISPATCH, S_WAIT_GET, S_WAIT_PUT, S_DONE,
                   S_TRANSPORT, S_MODE, S_BAUD, S_BAUD_DIVIDE, S_BAUD_ACTUAL, S_BAUD_DONE, S_CONTROL);
  signal state, ret : state_t := S_IDLE;
  signal get_req_r, put_req_r, done_r : std_logic := '0';
  signal get_n_r, put_n_r : natural range 1 to 8 := 1;
  signal put_val_r : std_logic_vector(63 downto 0) := (others => '0');
  signal run_r     : std_logic := '0';
  signal man_r     : std_logic := '0';
  signal mode_ok   : boolean;                                -- 引数の方式を受けられる（0・1、Manchester なら 2 も）
  signal div_r     : unsigned(15 downto 0) := to_unsigned(CLK_HZ / 115_200, 16);
  signal status    : std_logic_vector(7 downto 0);           -- bit0 受信中, bit6 異常, bit7 取りこぼし
begin
  get_req     <= get_req_r;
  get_n       <= get_n_r;
  put_req     <= put_req_r;
  put_n       <= put_n_r;
  put_val     <= put_val_r;
  done        <= done_r;
  swo_run     <= run_r;
  swo_divisor <= div_r;
  swo_manchester <= man_r;
  mode_ok     <= arg(7 downto 0) = x"00" or arg(7 downto 0) = x"01" or (HAS_MANCHESTER and arg(7 downto 0) = x"02");
  status      <= swo_overrun & swo_error & "00000" & run_r;


  process (clk)
    procedure get(n : natural; back : state_t) is
    begin
      get_req_r <= '1';
      get_n_r   <= n;
      ret       <= back;
      state     <= S_WAIT_GET;
    end procedure;

    procedure put(n : natural; v : std_logic_vector(63 downto 0); back : state_t) is
    begin
      put_req_r <= '1';
      put_n_r   <= n;
      put_val_r <= v;
      ret       <= back;
      state     <= S_WAIT_PUT;
    end procedure;

    procedure reply_status(ok : boolean) is
    begin
      if ok then
        put(1, x"00000000000000" & DAP_OK, S_DONE);
      else
        put(1, x"00000000000000" & DAP_ERROR, S_DONE);
      end if;
    end procedure;
  begin
    if rising_edge(clk) then
      get_req_r <= '0';
      put_req_r <= '0';
      done_r    <= '0';
      div_start <= '0';

      if rst = '1' then
        state <= S_IDLE;
        run_r <= '0';
        man_r <= '0';
      else
        case state is
          when S_IDLE =>
            if start = '1' then
              state <= S_DISPATCH;
            end if;

          when S_DISPATCH =>
            case command is
              when ID_SWO_TRANSPORT => get(1, S_TRANSPORT);
              when ID_SWO_MODE      => get(1, S_MODE);
              when ID_SWO_BAUDRATE  => get(4, S_BAUD);
              when ID_SWO_CONTROL   => get(1, S_CONTROL);
              when ID_SWO_STATUS    => put(5, x"000000" & x"00000000" & status, S_DONE);   -- 状態、バッファのバイト数 0
              when others           => put(3, x"0000000000" & x"0000" & status, S_DONE);   -- SWO_Data: 状態、0 バイト
            end case;

          when S_WAIT_GET =>
            if get_ack = '1' then
              state <= ret;
            end if;

          when S_WAIT_PUT =>
            if put_ack = '1' then
              state <= ret;
            end if;

          when S_DONE =>
            done_r <= '1';
            state  <= S_IDLE;

          when S_TRANSPORT =>
            reply_status(run_r = '0' and (arg(7 downto 0) = x"00" or arg(7 downto 0) = x"02"));

          -- 参照実装と同じく、受信中でも受けて受信を止める（pyOCD は設定し直す前に Mode OFF で止める）
          when S_MODE =>
            if mode_ok then
              run_r <= '0';
              man_r <= arg(1);
            end if;
            reply_status(mode_ok);

          -- SWO_Baudrate: divisor = round(CLK_HZ / baud)（4〜65535）、応答は実際のボーレート CLK_HZ / divisor
          when S_BAUD =>
            if run_r = '1' or unsigned(arg(31 downto 0)) = 0 then
              put(4, (others => '0'), S_DONE);                       -- 設定できない: 0
            else
              div_num   <= to_unsigned(CLK_HZ, 32) + shift_right(unsigned(arg(31 downto 0)), 1);
              div_den   <= unsigned(arg(31 downto 0));
              div_start <= '1';
              state     <= S_BAUD_DIVIDE;
            end if;

          when S_BAUD_DIVIDE =>
            if div_done = '1' then
              if div_quot < 4 then
                div_den <= to_unsigned(4, 32);
              elsif div_quot > 65535 then
                div_den <= to_unsigned(65535, 32);
              else
                div_den <= div_quot;
              end if;
              state <= S_BAUD_ACTUAL;
            end if;

          when S_BAUD_ACTUAL =>
            div_r     <= div_den(15 downto 0);
            div_num   <= to_unsigned(CLK_HZ, 32);
            div_start <= '1';
            state     <= S_BAUD_DONE;

          when S_BAUD_DONE =>
            if div_done = '1' then
              put(4, x"00000000" & std_logic_vector(div_quot), S_DONE);
            end if;

          when S_CONTROL =>
            run_r <= arg(0);
            reply_status(true);
        end case;
      end if;
    end if;
  end process;
end architecture;
