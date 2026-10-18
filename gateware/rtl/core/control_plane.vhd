-- CONTROL チャンネルの要求（READ / WRITE / EXCHANGE）を解釈し、CSR バスのマスタとして実行して応答を返す。
-- 仕様: docs/superpowers/specs/2026-10-15-m3-control-plane-design.md §3
--
-- 入出力はバイトストリーム（in_last / out_last がメッセージの終わり）。要求ヘッダは 8 バイト
-- （op, tag, count(2), addr(4)、リトルエンディアン）。応答は READ のデータの後に末尾 4 バイトの状態
-- （tag, status, done(2)）。WRITE は 4 バイト揃うごとに書き、READ は読んだワードをそのまま流す
-- （どちらもバッファを持たない）。EXCHANGE は 4 バイト揃うごとに同じ番地へ書き、続けてその番地を読んで
-- 流す（番地は進めない。ビットバング・FIFO のように「書いて応答を読む」レジスタを 1 要求で何度も使う）。
-- エラーで止めた要求の残りは読み捨ててから状態を返す。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;
use work.csr_map_pkg.all;

entity control_plane is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    -- 要求
    in_valid  : in  std_logic;
    in_ready  : out std_logic;
    in_data   : in  std_logic_vector(7 downto 0);
    in_last   : in  std_logic;
    -- 応答
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(7 downto 0);
    out_last  : out std_logic;
    -- CSR バス（マスタ）
    bus_req   : out std_logic;
    bus_we    : out std_logic;
    bus_addr  : out csr_addr_t;
    bus_wdata : out csr_word_t;
    bus_ack   : in  std_logic;
    bus_err   : in  std_logic;
    bus_rdata : in  csr_word_t
  );
end entity;

architecture rtl of control_plane is
  type state_t is (S_HEADER, S_WDATA, S_WRITE, S_READ, S_READ_OUT, S_DRAIN, S_TRAILER);
  type header_t is array (0 to CTRL_REQUEST_HEADER_BYTES - 1) of std_logic_vector(7 downto 0);

  signal state   : state_t := S_HEADER;
  signal header  : header_t := (others => (others => '0'));
  signal hdr_n   : natural range 0 to CTRL_REQUEST_HEADER_BYTES - 1 := 0;
  signal status  : std_logic_vector(7 downto 0) := CTRL_STATUS_OK;
  signal done    : unsigned(15 downto 0) := (others => '0');
  signal word    : csr_word_t := (others => '0');   -- WRITE で組み立て中 / READ で送出中のワード
  signal byte_n  : natural range 0 to 3 := 0;
  signal ended   : std_logic := '0';                -- 要求メッセージを最後まで受け取った

  signal accept  : boolean;
  signal emit    : boolean;
  signal op      : std_logic_vector(7 downto 0);
  signal count   : unsigned(15 downto 0);
  signal last_n  : unsigned(15 downto 0) := (others => '0');   -- count - 1（最後のワードの番号）
  signal cur     : csr_addr_t := (others => '0');   -- 次にアクセスするワードのアドレス（バスへ直接）
  signal aligned : boolean;
  signal fixed   : boolean;                         -- EXCHANGE: 番地を進めない・書いた後に読む
begin
  op      <= header(0);
  count   <= unsigned(header(3)) & unsigned(header(2));
  aligned <= header(4)(1 downto 0) = "00";
  fixed   <= op = CTRL_OP_EXCHANGE;

  in_ready <= '1' when state = S_HEADER or state = S_WDATA or state = S_DRAIN else '0';
  accept   <= in_valid = '1' and (state = S_HEADER or state = S_WDATA or state = S_DRAIN);

  bus_req   <= '1' when state = S_WRITE or state = S_READ else '0';
  bus_we    <= '1' when state = S_WRITE else '0';
  bus_addr  <= cur;
  bus_wdata <= word;

  -- 応答のバイト（READ のデータ、続いて末尾の状態）
  out_valid <= '1' when state = S_READ_OUT or state = S_TRAILER else '0';
  emit      <= out_ready = '1' and (state = S_READ_OUT or state = S_TRAILER);
  process (all)
  begin
    out_last <= '0';
    if state = S_READ_OUT then
      out_data <= word(8 * byte_n + 7 downto 8 * byte_n);
    else
      case byte_n is
        when 0      => out_data <= header(1);                     -- tag（無ければ 0）
        when 1      => out_data <= status;
        when 2      => out_data <= std_logic_vector(done(7 downto 0));
        when others => out_data <= std_logic_vector(done(15 downto 8));
                       out_last <= '1';
      end case;
    end if;
  end process;

  process (clk)
    variable addr : std_logic_vector(31 downto 0);
    -- 状態を確定して応答へ。要求の残りがあれば先に読み捨てる
    procedure respond(s : std_logic_vector(7 downto 0); finished : boolean) is
    begin
      status <= s;
      byte_n <= 0;
      if finished then
        state <= S_TRAILER;
      else
        state <= S_DRAIN;
      end if;
    end procedure;
  begin
    if rising_edge(clk) then
      if rst = '1' then
        state  <= S_HEADER;
        hdr_n  <= 0;
        header <= (others => (others => '0'));
      else
        case state is
          when S_HEADER =>
            if accept then
              header(hdr_n) <= in_data;
              done          <= (others => '0');
              byte_n        <= 0;
              ended         <= in_last;
              if hdr_n /= CTRL_REQUEST_HEADER_BYTES - 1 then
                hdr_n <= hdr_n + 1;
                if in_last = '1' then
                  respond(CTRL_STATUS_BAD_LENGTH, true);          -- ヘッダの途中で終わった
                end if;
              else
                -- ヘッダが揃った（op / count / addr の整列は格納済みの 0〜4 バイト目で決まる）
                hdr_n  <= 0;
                last_n <= count - 1;
                addr   := in_data & header(6) & header(5) & header(4);   -- 最後のバイトはこの周期の入力
                cur    <= unsigned(addr(31 downto 2));
                if op /= CTRL_OP_READ and op /= CTRL_OP_WRITE and op /= CTRL_OP_EXCHANGE then
                  respond(CTRL_STATUS_BAD_OP, in_last = '1');
                elsif count = 0 or not aligned then
                  respond(CTRL_STATUS_BAD_LENGTH, in_last = '1');
                elsif op = CTRL_OP_READ then
                  if in_last = '1' then
                    state <= S_READ;
                  else
                    respond(CTRL_STATUS_BAD_LENGTH, false);       -- READ にデータが付いている
                  end if;
                elsif in_last = '1' then
                  respond(CTRL_STATUS_BAD_LENGTH, true);          -- WRITE / EXCHANGE にデータが無い
                else
                  state <= S_WDATA;
                end if;
              end if;
            end if;

          when S_WDATA =>
            if accept then
              word(8 * byte_n + 7 downto 8 * byte_n) <= in_data;
              ended <= in_last;
              if byte_n = 3 then
                byte_n <= 0;
                state  <= S_WRITE;
              elsif in_last = '1' then
                respond(CTRL_STATUS_BAD_LENGTH, true);            -- ワードの途中で終わった
              else
                byte_n <= byte_n + 1;
              end if;
            end if;

          when S_WRITE =>
            if bus_ack = '1' then
              if not fixed then
                cur <= cur + 1;
              end if;
              if bus_err = '1' then
                respond(CTRL_STATUS_BUS_ERROR, ended = '1');
              elsif fixed then
                state <= S_READ;                                  -- EXCHANGE: 同じ番地を読む
              elsif done = last_n then
                done <= done + 1;
                if ended = '1' then
                  respond(CTRL_STATUS_OK, true);
                else
                  respond(CTRL_STATUS_BAD_LENGTH, false);         -- データが余った
                end if;
              else
                done <= done + 1;
                if ended = '1' then
                  respond(CTRL_STATUS_BAD_LENGTH, true);          -- データが足りない
                else
                  state <= S_WDATA;
                end if;
              end if;
            end if;

          when S_READ =>
            if bus_ack = '1' then
              if bus_err = '1' then
                respond(CTRL_STATUS_BUS_ERROR, ended = '1');
              else
                word   <= bus_rdata;
                if not fixed then
                  cur <= cur + 1;
                end if;
                byte_n <= 0;
                state  <= S_READ_OUT;
              end if;
            end if;

          when S_READ_OUT =>
            if emit then
              if byte_n = 3 then
                done <= done + 1;
                if not fixed then
                  if done = last_n then
                    respond(CTRL_STATUS_OK, true);
                  else
                    state <= S_READ;
                  end if;
                elsif done = last_n then                          -- EXCHANGE: WRITE と同じ長さの検査
                  if ended = '1' then
                    respond(CTRL_STATUS_OK, true);
                  else
                    respond(CTRL_STATUS_BAD_LENGTH, false);       -- データが余った
                  end if;
                elsif ended = '1' then
                  respond(CTRL_STATUS_BAD_LENGTH, true);          -- データが足りない
                else
                  byte_n <= 0;
                  state  <= S_WDATA;
                end if;
              else
                byte_n <= byte_n + 1;
              end if;
            end if;

          when S_DRAIN =>
            if accept and in_last = '1' then
              byte_n <= 0;
              state  <= S_TRAILER;
            end if;

          when S_TRAILER =>
            if emit then
              if byte_n = 3 then
                hdr_n  <= 0;
                header <= (others => (others => '0'));
                state  <= S_HEADER;
              else
                byte_n <= byte_n + 1;
              end if;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
