-- CMSIS-DAP の設定系コマンド: Info / HostStatus / Connect / Disconnect / TransferConfig / SWD_Configure / Delay /
-- ResetTarget / SWJ_Clock / JTAG_Configure。設定を持ち、他のユニットと PHY に出す。
-- JTAG の装置番号（jtag_index、転送ユニットが選ぶ）からチェーン上の位置を出す（装置 0 が TDO 側）。
-- DAP_Info の文字列は ROM（dap_info_pkg の DAP_INFO_ROM。同期読み出しで BSRAM になる）から読む。
-- HAS_JTAG = false の構成（JTAG の PHY が無い）: DAP_Connect の JTAG は失敗で答え、Capabilities に JTAG を出さない。
-- HAS_SWO_MANCHESTER = false の構成（SWO の Manchester の受信回路が無い）: Capabilities に SWO（Manchester）を出さない。
-- 仕様: docs/superpowers/specs/2026-10-16-dap-processor-split-design.md §2
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.dap_pkg.all;
use work.dap_info_pkg.all;

entity dap_cmd_config is
  generic (
    CLK_HZ     : positive;
    SWO_BUFFER : positive;
    HAS_JTAG   : boolean := true;     -- JTAG の PHY がある（fprobe_core のジェネリック HAS_JTAG）
    HAS_SWO_MANCHESTER : boolean := true  -- SWO の Manchester 方式がある（fprobe_core のジェネリック HAS_SWO_MANCHESTER）
  );
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    packet_size : in  natural range 64 to 512;
    -- dap_packet との I/F（dap_pkg の説明を参照）
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
    -- 設定
    enable      : out std_logic;
    jtag        : out std_logic;
    half_period : out unsigned(15 downto 0);
    turnaround  : out natural range 1 to 4;
    data_phase  : out std_logic;
    idle_cycles : out natural range 0 to 255;
    wait_retry  : out unsigned(15 downto 0);
    match_retry : out unsigned(15 downto 0);
    -- JTAG のチェーン
    jtag_count  : out natural range 1 to JTAG_MAX_DEVICES;
    jtag_index  : in  natural range 0 to JTAG_MAX_DEVICES - 1;
    dr_before   : out natural range 0 to 7;
    dr_after    : out natural range 0 to 7;
    ir_before   : out natural range 0 to 63;
    ir_after    : out natural range 0 to 63;
    ir_len      : out natural range 1 to 32;
    -- 割り算器（dap_processor が共有する udiv。div_start の周期に div_num / div_den を取り込む）
    div_start : out std_logic := '0';
    div_num   : out unsigned(31 downto 0) := (others => '0');
    div_den   : out unsigned(31 downto 0) := (others => '0');
    div_done  : in  std_logic;
    div_quot  : in  unsigned(31 downto 0)
  );
end entity;

architecture rtl of dap_cmd_config is
  constant CYCLES_PER_US : positive := (CLK_HZ + 500_000) / 1_000_000;
  constant HALF_CLOCK_HZ : natural := CLK_HZ / 2;          -- SWCLK = HALF_CLOCK_HZ / half_period
  -- SWJ_Clock は要求を 1% まで超えてよい（sys 100.29 MHz で 25 MHz の要求に 16.7 MHz でなく 25.07 MHz を選ぶ）
  constant HALF_CLOCK_TOL_HZ : natural := HALF_CLOCK_HZ / 101 * 100;

  type state_t is (S_IDLE, S_DISPATCH, S_WAIT_GET, S_WAIT_PUT,
                   S_INFO, S_INFO_FETCH, S_INFO_LENGTH, S_INFO_STRING, S_CONNECT, S_TRANSFER_CONFIG, S_SWD_CONFIGURE, S_DELAY, S_DELAY_WAIT,
                   S_CLOCK, S_CLOCK_DIVIDE, S_CLOCK_DONE, S_JCONF_COUNT, S_JCONF_NEXT, S_JCONF_LEN, S_DONE);
  signal state, ret : state_t := S_IDLE;
  signal get_req_r, put_req_r, done_r : std_logic := '0';
  signal get_n_r, put_n_r : natural range 1 to 8 := 1;
  signal put_val_r  : std_logic_vector(63 downto 0) := (others => '0');

  signal enable_r     : std_logic := '0';
  signal jtag_r       : std_logic := '0';
  signal half_r       : unsigned(15 downto 0) := to_unsigned(HALF_CLOCK_HZ / 1_000_000, 16);   -- 1 MHz
  signal turnaround_r : natural range 1 to 4 := 1;
  signal data_phase_r : std_logic := '0';
  signal idle_r       : natural range 0 to 255 := 0;
  signal wait_retry_r : unsigned(15 downto 0) := to_unsigned(100, 16);
  signal match_retry_r : unsigned(15 downto 0) := (others => '0');

  function info_capabilities(with_jtag, with_manchester : boolean) return std_logic_vector is
    variable caps : std_logic_vector(7 downto 0) := DAP_CAPABILITIES;
  begin
    if with_jtag then
      caps := caps or DAP_CAPABILITY_JTAG;
    end if;
    if with_manchester then
      caps := caps or DAP_CAPABILITY_SWO_MANCHESTER;
    end if;
    return caps;
  end function;
  constant CAPS_INFO : std_logic_vector(7 downto 0) := info_capabilities(HAS_JTAG, HAS_SWO_MANCHESTER);
  signal rom_addr : unsigned(DAP_INFO_ADDR_BITS - 1 downto 0) := (others => '0');   -- 折り返す（枠の終わりの次）
  signal rom_q    : std_logic_vector(7 downto 0) := (others => '0');
  signal info_len : natural range 0 to DAP_INFO_SLOT_BYTES - 1 := 0;     -- 文字列の残り（NUL 込み）
  signal us_left  : unsigned(15 downto 0) := (others => '0');
  signal cycles   : natural range 0 to CYCLES_PER_US := 0;

  -- JTAG のチェーン（IR 32 ビットまで。構成時に IR の累積和を作り、位置は加算器の連鎖なしで求める）
  type ir_lengths_t is array (0 to JTAG_MAX_DEVICES - 1) of natural range 1 to 32;
  type ir_sums_t is array (0 to JTAG_MAX_DEVICES - 1) of natural range 0 to 255;
  signal count_r   : natural range 1 to JTAG_MAX_DEVICES := 1;
  signal irlen     : ir_lengths_t := (others => 4);
  signal ir_prefix : ir_sums_t := (others => 0);             -- 装置 i より TDO 側の IR の合計
  signal ir_total  : natural range 0 to 255 := 4;           -- 全装置の IR の合計（構成中は積算）
  signal conf_i    : natural range 0 to JTAG_MAX_DEVICES := 0;
begin
  get_req <= get_req_r;
  get_n   <= get_n_r;
  put_req <= put_req_r;
  put_n   <= put_n_r;
  put_val <= put_val_r;
  done    <= done_r;

  enable      <= enable_r;
  jtag        <= jtag_r;
  half_period <= half_r;
  turnaround  <= turnaround_r;
  data_phase  <= data_phase_r;
  idle_cycles <= idle_r;
  wait_retry  <= wait_retry_r;
  match_retry <= match_retry_r;
  jtag_count  <= count_r;


  -- DAP_Info の文字列の ROM（同期読み出し）
  process (clk)
  begin
    if rising_edge(clk) then
      rom_q <= DAP_INFO_ROM(to_integer(rom_addr));
    end if;
  end process;

  process (clk)
  begin
    if rising_edge(clk) then
      ir_before <= minimum(ir_prefix(jtag_index), 63);
      ir_after  <= maximum(0, minimum(ir_total - ir_prefix(jtag_index) - irlen(jtag_index), 63));   -- 構成中は負になりうる
      ir_len    <= irlen(jtag_index);
      dr_before <= jtag_index;
      dr_after  <= maximum(0, count_r - 1 - jtag_index);           -- 構成を縮めた直後は負になりうる
    end if;
  end process;

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

    procedure reply_status(s : std_logic_vector(7 downto 0)) is
    begin
      put(1, x"00000000000000" & s, S_DONE);
    end procedure;
  begin
    if rising_edge(clk) then
      get_req_r <= '0';
      put_req_r <= '0';
      done_r    <= '0';
      div_start <= '0';

      if rst = '1' then
        state         <= S_IDLE;
        enable_r      <= '0';
        jtag_r        <= '0';
        half_r        <= to_unsigned(HALF_CLOCK_HZ / 1_000_000, 16);
        turnaround_r  <= 1;
        data_phase_r  <= '0';
        idle_r        <= 0;
        wait_retry_r  <= to_unsigned(100, 16);
        match_retry_r <= (others => '0');
        count_r       <= 1;
        irlen         <= (others => 4);
        ir_prefix     <= (others => 0);
        ir_total      <= 4;
      else
        case state is
          when S_IDLE =>
            if start = '1' then
              state <= S_DISPATCH;
            end if;

          when S_DISPATCH =>
            case command is
              when ID_INFO            => get(1, S_INFO);
              when ID_HOST_STATUS     => reply_status(DAP_OK);         -- 受け付けるだけ（引数は読み捨て）
              when ID_CONNECT         => get(1, S_CONNECT);
              when ID_DISCONNECT      => enable_r <= '0';
                                         reply_status(DAP_OK);
              when ID_TRANSFER_CONFIG => get(5, S_TRANSFER_CONFIG);
              when ID_DELAY           => get(2, S_DELAY);
              when ID_RESET_TARGET    => put(2, (others => '0'), S_DONE);    -- 状態 OK, execute = 0
              when ID_SWJ_CLOCK       => get(4, S_CLOCK);
              when ID_SWD_CONFIGURE   => get(1, S_SWD_CONFIGURE);
              when others             => get(1, S_JCONF_COUNT);        -- ID_JTAG_CONFIGURE
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

          -- DAP_Info
          when S_INFO =>
            case to_integer(unsigned(arg(7 downto 0))) is
              when 16#F0# =>
                put(2, x"000000000000" & CAPS_INFO & x"01", S_DONE);
              when 16#FD# =>
                put(5, x"000000" & std_logic_vector(to_unsigned(SWO_BUFFER, 32)) & x"04", S_DONE);
              when 16#FE# =>
                put(2, x"000000000000" & std_logic_vector(to_unsigned(DAP_PACKET_COUNT, 8)) & x"01", S_DONE);
              when 16#FF# =>
                put(3, x"0000000000" & std_logic_vector(to_unsigned(packet_size, 16)) & x"02", S_DONE);
              when 0 to DAP_INFO_SLOTS - 1 =>
                -- 文字列（NUL 込み）か、未知の ID（長さ 0）: ROM の枠の先頭（長さ）を読む
                rom_addr <= to_unsigned(to_integer(unsigned(arg(3 downto 0))) * DAP_INFO_SLOT_BYTES, DAP_INFO_ADDR_BITS);
                state    <= S_INFO_FETCH;
              when others =>
                put(1, (others => '0'), S_DONE);                    -- 文字列の ID ではない（長さ 0）
            end case;

          when S_INFO_FETCH =>
            state <= S_INFO_LENGTH;                                 -- ROM が rom_addr を読む

          when S_INFO_LENGTH =>
            info_len <= to_integer(unsigned(rom_q(4 downto 0)));
            rom_addr <= rom_addr + 1;
            put(1, x"00000000000000" & rom_q, S_INFO_STRING);

          when S_INFO_STRING =>
            -- rom_q は rom_addr の文字（put の応答待ちで 1 周期以上たっている）
            if info_len = 0 then
              state <= S_DONE;
            else
              info_len <= info_len - 1;
              rom_addr <= rom_addr + 1;
              put(1, x"00000000000000" & rom_q, S_INFO_STRING);
            end if;

          when S_CONNECT =>
            if arg(7 downto 0) = x"00" or arg(7 downto 0) = x"01" then
              enable_r <= '1';
              jtag_r   <= '0';
              reply_status(x"01");                                  -- SWD（既定も SWD）
            elsif arg(7 downto 0) = x"02" and HAS_JTAG then
              enable_r <= '1';
              jtag_r   <= '1';
              reply_status(x"02");                                  -- JTAG
            else
              reply_status(x"00");                                  -- 失敗
            end if;

          when S_TRANSFER_CONFIG =>
            idle_r        <= to_integer(unsigned(arg(7 downto 0)));
            wait_retry_r  <= unsigned(arg(23 downto 8));
            match_retry_r <= unsigned(arg(39 downto 24));
            reply_status(DAP_OK);

          when S_SWD_CONFIGURE =>
            turnaround_r <= to_integer(unsigned(arg(1 downto 0))) + 1;
            data_phase_r <= arg(2);
            reply_status(DAP_OK);

          -- DAP_Delay: arg µs 待つ
          when S_DELAY =>
            us_left <= unsigned(arg(15 downto 0));
            cycles  <= 0;
            state   <= S_DELAY_WAIT;

          when S_DELAY_WAIT =>
            if us_left = 0 then
              reply_status(DAP_OK);
            elsif cycles = CYCLES_PER_US - 1 then
              cycles  <= 0;
              us_left <= us_left - 1;
            else
              cycles <= cycles + 1;
            end if;

          -- SWJ_Clock: half_period = ceil(HALF_CLOCK_HZ / (1.01 * clock))、2〜65535
          when S_CLOCK =>
            if unsigned(arg(31 downto 0)) = 0 then
              reply_status(DAP_ERROR);
            else
              div_num   <= to_unsigned(HALF_CLOCK_TOL_HZ, 32) + unsigned(arg(31 downto 0)) - 1;  -- 切り上げ
              div_den   <= unsigned(arg(31 downto 0));
              div_start <= '1';
              state     <= S_CLOCK_DIVIDE;
            end if;

          when S_CLOCK_DIVIDE =>
            if div_done = '1' then
              state <= S_CLOCK_DONE;
            end if;

          when S_CLOCK_DONE =>
            if div_quot < 2 then
              half_r <= to_unsigned(2, 16);
            elsif div_quot > 65535 then
              half_r <= to_unsigned(65535, 16);
            else
              half_r <= div_quot(15 downto 0);
            end if;
            reply_status(DAP_OK);

          -- JTAG_Configure: count、IR 長 × count
          when S_JCONF_COUNT =>
            if arg(7 downto 0) = x"00" or unsigned(arg(7 downto 0)) > JTAG_MAX_DEVICES then
              reply_status(DAP_ERROR);
            else
              count_r  <= to_integer(unsigned(arg(7 downto 0)));
              conf_i   <= 0;
              ir_total <= 0;
              state    <= S_JCONF_NEXT;
            end if;

          when S_JCONF_NEXT =>
            if conf_i = count_r then
              reply_status(DAP_OK);
            else
              get(1, S_JCONF_LEN);
            end if;

          when S_JCONF_LEN =>
            if arg(7 downto 0) = x"00" or unsigned(arg(7 downto 0)) > 32 then
              reply_status(DAP_ERROR);
            else
              irlen(conf_i)     <= to_integer(unsigned(arg(7 downto 0)));
              ir_prefix(conf_i) <= ir_total;
              ir_total <= ir_total + to_integer(unsigned(arg(7 downto 0)));
              conf_i   <= conf_i + 1;
              state    <= S_JCONF_NEXT;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
