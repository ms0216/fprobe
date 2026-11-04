-- CMSIS-DAP のパケットの枠: 要求パケットの先頭（コマンド ID）を読んで担当ユニットを起動し、ユニットに引数の読み取り
-- （get）と応答への追記（put）を提供する。ユニットが終わったら応答の先頭を書き直し（header）、要求の残りを読み捨てて、
-- 応答バッファから送る。仕様: docs/superpowers/specs/2026-10-16-dap-processor-split-design.md §2
-- 応答バッファは 2 面（512 バイトずつ）。1 面を送っている間に、次のコマンドをもう 1 面に処理する（送り終えるのを待たない。
-- 応答の順は要求の順のまま: 処理し終えた面は、前の面を送り終えてから送る）。送出は 1 周期に 1 バイト（読みを 1 つ先に出す）。
--
-- 応答の先頭はコマンド ID。担当のないコマンドは 0xFF の 1 バイトを返す。TransferAbort は応答を返さない。
-- 足りない引数は 0（パケットが尽きた後の get は 0 を返す）。応答は packet_size バイトを超えた分を捨てる。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.dap_pkg.all;

entity dap_packet is
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    packet_size : in  natural range 64 to 512;
    -- 要求 / 応答（last はパケットの終わり）
    in_valid    : in  std_logic;
    in_ready    : out std_logic;
    in_data     : in  std_logic_vector(7 downto 0);
    in_last     : in  std_logic;
    out_valid   : out std_logic;
    out_ready   : in  std_logic;
    out_data    : out std_logic_vector(7 downto 0);
    out_last    : out std_logic;
    busy        : out std_logic;                          -- コマンドを処理中
    -- ユニット側
    unit        : out dap_unit_t;                         -- 処理中のコマンドの担当（start から done まで有効）
    command     : out natural range 0 to 255;
    start       : out std_logic;
    done        : in  std_logic;
    header      : in  std_logic_vector(23 downto 0);      -- done の周期に: 応答の [1..header_n] に書く値
    header_n    : in  natural range 0 to 3;
    get_req     : in  std_logic;
    get_n       : in  natural range 1 to 8;
    get_ack     : out std_logic;
    arg         : out std_logic_vector(63 downto 0);      -- get_ack から次の get_req まで有効
    put_req     : in  std_logic;
    put_n       : in  natural range 1 to 8;
    put_val     : in  std_logic_vector(63 downto 0);
    put_ack     : out std_logic
  );
end entity;

architecture rtl of dap_packet is
  type state_t is (S_COMMAND, S_START, S_RUN, S_GET, S_PUT, S_PATCH, S_DRAIN, S_SEND);
  signal state    : state_t := S_COMMAND;
  signal unit_r   : dap_unit_t := U_NONE;
  signal command_r : natural range 0 to 255 := 0;
  signal ended    : std_logic := '0';                     -- 要求パケットを最後まで読んだ
  signal silent   : std_logic := '0';                     -- 応答を返さない（TransferAbort）
  signal accept   : boolean;
  -- get / put
  signal n        : natural range 1 to 8 := 1;
  signal i        : natural range 0 to 8 := 0;
  signal more     : std_logic := '0';                     -- まだバイトが要る / 書くバイトがある（前もって保持）
  signal arg_r    : std_logic_vector(63 downto 0) := (others => '0');
  signal val      : std_logic_vector(63 downto 0) := (others => '0');
  signal hdr      : std_logic_vector(23 downto 0) := (others => '0');
  signal hdr_n    : natural range 0 to 3 := 0;
  -- 応答バッファ 2 面（書き込みは 1 ポート、読み出しは同期読み出し。BSRAM 1 つに収める）。番地 = 面 × 512 + 位置
  type buffer_t is array (0 to 1023) of std_logic_vector(7 downto 0);
  signal rbuf     : buffer_t;
  signal buf_we   : std_logic := '0';
  signal buf_waddr : natural range 0 to 511 := 0;
  signal buf_wdata : std_logic_vector(7 downto 0) := (others => '0');
  signal wbank    : natural range 0 to 1 := 0;            -- 処理中のコマンドの応答を書く面
  signal rd_q     : std_logic_vector(7 downto 0) := (others => '0');
  signal wp       : natural range 0 to 512 := 0;          -- 書き込み位置 = 応答の長さ
  -- 送出部: 面 sbank の 0 .. slen-1 を送る。rd_q = 位置 sp のバイト（q_valid のとき）
  signal sbank    : natural range 0 to 1 := 0;
  signal slen     : natural range 0 to 512 := 0;
  signal sp       : natural range 0 to 512 := 0;
  signal q_valid  : std_logic := '0';
  signal move     : std_logic;                            -- この周期に rd_q を出力のレジスタへ移す
  signal raddr    : natural range 0 to 1023;
  signal out_reg  : std_logic_vector(7 downto 0) := (others => '0');
  signal out_full : std_logic := '0';
  signal out_end  : std_logic := '0';
  signal send_go  : std_logic := '0';
  signal sending  : std_logic := '0';
  signal get_ack_r, put_ack_r, start_r : std_logic := '0';
begin
  accept   <= in_valid = '1' and ended = '0' and
              (state = S_COMMAND or state = S_DRAIN or (state = S_GET and more = '1'));
  in_ready <= '1' when accept else '0';
  busy     <= '0' when state = S_COMMAND else '1';
  unit     <= unit_r;
  command  <= command_r;
  start    <= start_r;
  get_ack  <= get_ack_r;
  put_ack  <= put_ack_r;
  arg      <= arg_r;

  process (clk)
  begin
    if rising_edge(clk) then
      if buf_we = '1' then
        rbuf(wbank * 512 + buf_waddr) <= buf_wdata;
      end if;
      rd_q <= rbuf(raddr);
    end if;
  end process;

  process (clk)
    procedure buf_write(addr : natural; b : std_logic_vector(7 downto 0)) is
    begin
      buf_we    <= '1';
      buf_waddr <= addr;
      buf_wdata <= b;
    end procedure;
  begin
    if rising_edge(clk) then
      buf_we    <= '0';
      send_go   <= '0';
      start_r   <= '0';
      get_ack_r <= '0';
      put_ack_r <= '0';

      if rst = '1' then
        state  <= S_COMMAND;
        unit_r <= U_NONE;
        ended  <= '0';
        wp     <= 0;
      else
        case state is
          when S_COMMAND =>
            silent <= '0';
            if accept then
              command_r <= to_integer(unsigned(in_data));
              unit_r    <= unit_of(to_integer(unsigned(in_data)));
              ended     <= in_last;
              buf_write(0, in_data);                              -- 応答の先頭はコマンド ID
              wp        <= 1;
              state     <= S_START;
            end if;

          when S_START =>
            if command_r = ID_TRANSFER_ABORT then
              silent <= '1';
              state  <= S_DRAIN;
            elsif unit_r = U_NONE then
              buf_write(0, DAP_ERROR);                            -- 未対応: 0xFF の 1 バイト
              state <= S_DRAIN;
            else
              start_r <= '1';
              state   <= S_RUN;
            end if;

          when S_RUN =>
            if done = '1' then
              hdr   <= header;
              hdr_n <= header_n;
              i     <= 0;
              state <= S_PATCH;
            elsif get_req = '1' then
              n     <= get_n;
              i     <= 0;
              more  <= '1';
              arg_r <= (others => '0');
              state <= S_GET;
            elsif put_req = '1' then
              n     <= put_n;
              i     <= 0;
              more  <= '1';
              val   <= put_val;
              state <= S_PUT;
            end if;

          when S_GET =>
            if more = '0' then
              get_ack_r <= '1';
              state     <= S_RUN;
            elsif ended = '1' then
              i <= i + 1;                                         -- 足りない引数は 0
              if i + 1 = n then
                more <= '0';
              end if;
            elsif accept then
              if i + 1 = n then
                more <= '0';
              end if;
              for k in 0 to 7 loop
                if k = i then
                  arg_r(8 * k + 7 downto 8 * k) <= in_data;
                end if;
              end loop;
              ended <= in_last;
              i     <= i + 1;
            end if;

          when S_PUT =>
            if more = '0' then
              put_ack_r <= '1';
              state     <= S_RUN;
            else
              if wp < packet_size then
                buf_write(wp, byte_of(val, i));
                wp <= wp + 1;
              end if;
              i <= i + 1;
              if i + 1 = n then
                more <= '0';
              end if;
            end if;

          when S_PATCH =>
            if i = hdr_n then
              state <= S_DRAIN;
            else
              buf_write(i + 1, byte_of(x"0000000000" & hdr, i));
              i <= i + 1;
            end if;

          when S_DRAIN =>
            if ended = '1' then
              ended  <= '0';
              unit_r <= U_NONE;
              if silent = '1' then
                state <= S_COMMAND;
              else
                state <= S_SEND;
              end if;
            elsif accept then
              ended <= in_last;
            end if;

          when S_SEND =>
            -- 前の応答を送り終えたら（send_go を出した次の周期から sending が立つ）、この面を送出部へ渡し、次のコマンドは
            -- もう 1 面に処理する
            if send_go = '0' and sending = '0' then
              send_go <= '1';
              wbank   <= 1 - wbank;
              state   <= S_COMMAND;
            end if;
        end case;
      end if;
    end if;
  end process;

  -- 送出部: 面 sbank の 0 .. slen-1 を順に出す。読みの番地は次に要る位置（移す周期は sp + 1）なので、出力のレジスタが
  -- 受けられれば 1 周期に 1 バイト
  move  <= '1' when sending = '1' and q_valid = '1' and (out_full = '0' or out_ready = '1') else '0';
  raddr <= sbank * 512 + ((sp + 1) mod 512) when move = '1' else sbank * 512 + (sp mod 512);

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        sending  <= '0';
        out_full <= '0';
        q_valid  <= '0';
      elsif send_go = '1' then
        sending <= '1';
        sbank   <= 1 - wbank;                                 -- 渡された面（send_go を出した辺で wbank はもう 1 面に切り替わっている）
        slen    <= wp;
        sp      <= 0;
        q_valid <= '0';
      elsif sending = '1' then
        if move = '1' then
          out_reg  <= rd_q;
          out_end  <= '1' when sp = slen - 1 else '0';
          out_full <= '1';
          sp       <= sp + 1;
          q_valid  <= '1' when sp + 1 < slen else '0';      -- 次の周期の rd_q は位置 sp + 1
        else
          if out_full = '1' and out_ready = '1' then
            out_full <= '0';
          end if;
          if q_valid = '0' then
            if sp < slen then
              q_valid <= '1';                                 -- この周期の読み（位置 sp）が次の周期の rd_q
            elsif out_full = '0' or out_ready = '1' then
              sending <= '0';                                 -- 送り終えた
            end if;
          end if;
        end if;
      end if;
    end if;
  end process;

  out_valid <= out_full;
  out_data  <= out_reg;
  out_last  <= out_end;
end architecture;
