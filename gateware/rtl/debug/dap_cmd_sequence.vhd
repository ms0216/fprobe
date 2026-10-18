-- CMSIS-DAP のピン・ビット列のコマンド: SWJ_Pins / SWJ_Sequence / SWD_Sequence / JTAG_Sequence。
-- nRESET（オープンドレイン）を持つ。仕様: docs/superpowers/specs/2026-10-16-dap-processor-split-design.md
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.dap_pkg.all;

entity dap_cmd_sequence is
  generic (
    CLK_HZ : positive
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    -- dap_packet との I/F
    command      : in  natural range 0 to 255;
    start        : in  std_logic;
    done         : out std_logic;
    get_req      : out std_logic;
    get_n        : out natural range 1 to 8;
    get_ack      : in  std_logic;
    arg          : in  std_logic_vector(63 downto 0);
    put_req      : out std_logic;
    put_n        : out natural range 1 to 8;
    put_val      : out std_logic_vector(63 downto 0);
    put_ack      : in  std_logic;
    -- dap_link
    link_req     : out std_logic;
    link_op      : out std_logic_vector(1 downto 0);
    link_count   : out natural range 1 to 64;
    link_request : out std_logic_vector(3 downto 0);
    link_data    : out std_logic_vector(63 downto 0);
    link_ack     : in  std_logic;
    link_val     : in  std_logic_vector(63 downto 0);
    -- nRESET
    nreset_low   : out std_logic;                         -- '1' で Low を駆動
    nreset_in    : in  std_logic                          -- ピンの値（非同期。中で同期化する）
  );
end entity;

architecture rtl of dap_cmd_sequence is
  constant CYCLES_PER_US : positive := (CLK_HZ + 500_000) / 1_000_000;

  type state_t is (S_IDLE, S_DISPATCH, S_WAIT_GET, S_WAIT_PUT, S_WAIT_LINK, S_DONE,
                   S_PINS, S_PINS_WAIT, S_PINS_DONE,
                   S_SEQ_COUNT, S_SEQ_CHUNK, S_SEQ_DATA,
                   S_SWDSEQ_COUNT, S_SWDSEQ_NEXT, S_SWDSEQ_INFO, S_SWDSEQ_RUN, S_SWDSEQ_PUT,
                   S_JSEQ_COUNT, S_JSEQ_START, S_JSEQ_NEXT, S_JSEQ_INFO, S_JSEQ_RUN, S_JSEQ_PUT);
  signal state, ret : state_t := S_IDLE;
  signal get_req_r, put_req_r, link_req_r, done_r : std_logic := '0';
  signal get_n_r, put_n_r : natural range 1 to 8 := 1;
  signal put_val_r  : std_logic_vector(63 downto 0) := (others => '0');
  signal op_r       : std_logic_vector(1 downto 0) := OP_SEQ_OUT;
  signal count_r    : natural range 1 to 64 := 1;
  signal request_r  : std_logic_vector(3 downto 0) := (others => '0');
  signal data_r     : std_logic_vector(63 downto 0) := (others => '0');

  signal nreset_low_r : std_logic := '0';
  signal nreset_s1, nreset_s : std_logic := '1';
  signal pins_value  : std_logic_vector(7 downto 0) := (others => '0');
  signal pins_select : std_logic_vector(7 downto 0) := (others => '0');
  signal us_left     : unsigned(31 downto 0) := (others => '0');
  signal us_zero     : boolean := true;                     -- us_left = 0（前もって保持）
  signal cycles      : natural range 0 to CYCLES_PER_US := 0;
  signal seq_left    : natural range 0 to 256 := 0;         -- 残りビット数（SWJ_Sequence）
  signal seq_bits    : natural range 1 to 64 := 1;          -- 今の塊のビット数
  signal seq_count   : natural range 0 to 255 := 0;         -- 残りシーケンス数
  signal seq_input   : std_logic := '0';                    -- SWD_Sequence: 入力 / JTAG_Sequence: TDO を返す
begin
  get_req      <= get_req_r;
  get_n        <= get_n_r;
  put_req      <= put_req_r;
  put_n        <= put_n_r;
  put_val      <= put_val_r;
  link_req     <= link_req_r;
  link_op      <= op_r;
  link_count   <= count_r;
  link_request <= request_r;
  link_data    <= data_r;
  done         <= done_r;
  nreset_low   <= nreset_low_r;

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

    procedure phy(op : std_logic_vector(1 downto 0); n : natural; data : std_logic_vector(63 downto 0);
                  back : state_t) is
    begin
      link_req_r <= '1';
      op_r       <= op;
      count_r    <= n;
      data_r     <= data;
      ret        <= back;
      state      <= S_WAIT_LINK;
    end procedure;
  begin
    if rising_edge(clk) then
      get_req_r  <= '0';
      put_req_r  <= '0';
      link_req_r <= '0';
      done_r     <= '0';
      nreset_s1  <= nreset_in;
      nreset_s   <= nreset_s1;

      if rst = '1' then
        state        <= S_IDLE;
        nreset_low_r <= '0';
      else
        case state is
          when S_IDLE =>
            if start = '1' then
              state <= S_DISPATCH;
            end if;

          when S_DISPATCH =>
            case command is
              when ID_SWJ_PINS     => get(6, S_PINS);
              when ID_SWJ_SEQUENCE => get(1, S_SEQ_COUNT);
              when ID_SWD_SEQUENCE => get(1, S_SWDSEQ_COUNT);
              when others          => put(1, x"00000000000000" & DAP_OK, S_JSEQ_COUNT);   -- JTAG_Sequence: 状態
            end case;

          when S_WAIT_GET =>
            if get_ack = '1' then
              state <= ret;
            end if;

          when S_WAIT_PUT =>
            if put_ack = '1' then
              state <= ret;
            end if;

          when S_WAIT_LINK =>
            if link_ack = '1' then
              state <= ret;
            end if;

          when S_DONE =>
            done_r <= '1';
            state  <= S_IDLE;

          ------------------------------------------------------------------------------
          -- SWJ_Pins: bit0 SWCLK / bit1 SWDIO / bit7 nRESET。選んだ nRESET が値に達するか待ち時間が尽きるまで待つ
          ------------------------------------------------------------------------------
          when S_PINS =>
            pins_value  <= arg(7 downto 0);
            pins_select <= arg(15 downto 8);
            us_left     <= unsigned(arg(47 downto 16));
            us_zero     <= unsigned(arg(47 downto 16)) = 0;
            cycles      <= 0;
            if arg(15) = '1' then
              nreset_low_r <= not arg(7);
            end if;
            phy(OP_PINS, 1, (63 downto 10 => '0') & arg(9 downto 8) & "000000" & arg(1 downto 0), S_PINS_WAIT);

          when S_PINS_WAIT =>
            if us_zero or pins_select(7) = '0' or nreset_s = pins_value(7) then
              phy(OP_PINS, 1, (others => '0'), S_PINS_DONE);        -- 変えずに現在の値を読む
            elsif cycles = CYCLES_PER_US - 1 then
              cycles  <= 0;
              us_left <= us_left - 1;
              us_zero <= us_left = 1;
            else
              cycles <= cycles + 1;
            end if;

          when S_PINS_DONE =>
            put(1, x"00000000000000" & nreset_s & "00000" & link_val(1 downto 0), S_DONE);

          ------------------------------------------------------------------------------
          -- SWJ_Sequence: count（0 = 256）ビットを 64 ビットずつ
          ------------------------------------------------------------------------------
          when S_SEQ_COUNT =>
            if arg(7 downto 0) = x"00" then
              seq_left <= 256;
            else
              seq_left <= to_integer(unsigned(arg(7 downto 0)));
            end if;
            state <= S_SEQ_CHUNK;

          when S_SEQ_CHUNK =>
            if seq_left = 0 then
              put(1, x"00000000000000" & DAP_OK, S_DONE);
            elsif seq_left > 64 then
              seq_bits <= 64;
              get(8, S_SEQ_DATA);
            else
              seq_bits <= seq_left;
              get(bytes_for(seq_left), S_SEQ_DATA);
            end if;

          when S_SEQ_DATA =>
            seq_left <= seq_left - seq_bits;
            phy(OP_SEQ_OUT, seq_bits, arg, S_SEQ_CHUNK);

          ------------------------------------------------------------------------------
          -- SWD_Sequence: 各シーケンスの info（bit5..0 ビット数（0 = 64）、bit7 入力）
          ------------------------------------------------------------------------------
          when S_SWDSEQ_COUNT =>
            seq_count <= to_integer(unsigned(arg(7 downto 0)));
            put(1, x"00000000000000" & DAP_OK, S_SWDSEQ_NEXT);       -- 状態 OK

          when S_SWDSEQ_NEXT =>
            if seq_count = 0 then
              state <= S_DONE;
            else
              seq_count <= seq_count - 1;
              get(1, S_SWDSEQ_INFO);
            end if;

          when S_SWDSEQ_INFO =>
            if arg(5 downto 0) = "000000" then
              seq_bits <= 64;
            else
              seq_bits <= to_integer(unsigned(arg(5 downto 0)));
            end if;
            seq_input <= arg(7);
            if arg(7) = '1' then
              state <= S_SWDSEQ_RUN;
            elsif arg(5 downto 0) = "000000" then
              get(8, S_SWDSEQ_RUN);
            else
              get(bytes_for(to_integer(unsigned(arg(5 downto 0)))), S_SWDSEQ_RUN);
            end if;

          when S_SWDSEQ_RUN =>
            if seq_input = '1' then
              phy(OP_SEQ_IN, seq_bits, (others => '0'), S_SWDSEQ_PUT);
            else
              phy(OP_SEQ_OUT, seq_bits, arg, S_SWDSEQ_NEXT);
            end if;

          when S_SWDSEQ_PUT =>
            put(bytes_for(seq_bits), link_val, S_SWDSEQ_NEXT);

          ------------------------------------------------------------------------------
          -- JTAG_Sequence: 各シーケンス = info（bit5-0 TCK 数、0 = 64 / bit6 TMS / bit7 TDO を取る）+ TDI
          ------------------------------------------------------------------------------
          when S_JSEQ_COUNT =>
            get(1, S_JSEQ_START);

          when S_JSEQ_START =>
            seq_count <= to_integer(unsigned(arg(7 downto 0)));
            state     <= S_JSEQ_NEXT;

          when S_JSEQ_NEXT =>
            if seq_count = 0 then
              state <= S_DONE;
            else
              seq_count <= seq_count - 1;
              get(1, S_JSEQ_INFO);
            end if;

          when S_JSEQ_INFO =>
            if arg(5 downto 0) = "000000" then
              seq_bits <= 64;
              get(8, S_JSEQ_RUN);
            else
              seq_bits <= to_integer(unsigned(arg(5 downto 0)));
              get(bytes_for(to_integer(unsigned(arg(5 downto 0)))), S_JSEQ_RUN);
            end if;
            request_r <= "000" & arg(6);                            -- jtag_phy の OP_SEQ_IN: TMS
            seq_input <= arg(7);

          when S_JSEQ_RUN =>
            phy(OP_SEQ_IN, seq_bits, arg, S_JSEQ_PUT);

          when S_JSEQ_PUT =>
            if seq_input = '1' then
              put(bytes_for(seq_bits), link_val, S_JSEQ_NEXT);
            else
              state <= S_JSEQ_NEXT;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
