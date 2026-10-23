-- SWD の物理層: SWCLK / SWDIO を駆動し、ビット列と 1 トランザクションを実行する。
-- 仕様: docs/superpowers/specs/2026-10-15-m4-swd-cmsis-dap-design.md §3。振る舞いは ARM の CMSIS-DAP 参照実装
-- （SW_DP.c）に合わせる。
--
-- ビットのタイミング: 1 ビット = Low 相（SWCLK = 0）+ High 相（SWCLK = 1）、各 half_period 周期。
--   出力ビットは Low 相の頭で SWDIO に出す（ターゲットは立ち上がりで取り込む）。
--   入力ビットは Low 相の終わりで取り込む（ターゲットは立ち上がりの後に変える）。SWDIO はこちらの SWCLK に
--   同期して変わる信号なので、同期化は 1 段（I/O の FF）だけにする。サンプル点は Low 相の終わり（立ち上げる周期）
--   から sample_delay 周期後（往復遅延の補償。ピンの値は立ち上がりの後もターゲットの出力遅延の間は前のビットのまま）。
--   取り込みは Low 相の終わりで予約し、sample_delay + 1 周期後に din（1 周期前のピン）を取る（取り込み処理は 1 か所）。
--   遅れた取り込みが相の最後のビットの判定に間に合わないときは、最後の High 相を延ばして待つ。
--   アイドル時は SWCLK = 1、SWDIO = 1 を駆動する（enable = '0' なら両方 Hi-Z）。
--
-- 命令（cmd_valid / cmd_ready。完了で rsp_valid を 1 周期、rsp_* はそこから次の命令まで有効）:
--   OP_SEQ_OUT  cmd_count ビット（1〜64）を cmd_data の下位から出す
--   OP_SEQ_IN   cmd_count ビットを読み、rsp_data の下位から詰める（SWDIO は解放）
--   OP_TRANSFER cmd_request（CMSIS-DAP の要求: bit0 APnDP, bit1 RnW, bit2-3 A[3:2]）と cmd_data(31..0)（書き込み値）。
--               rsp_ack（001 OK / 010 WAIT / 100 FAULT / それ以外はプロトコル異常）、rsp_data(31..0)（読み値）、
--               rsp_parity_error（読みデータのパリティ異常）
--   OP_PINS     cmd_data(8) = 1 なら SWCLK を cmd_data(0) に、cmd_data(9) = 1 なら SWDIO を cmd_data(1) にする
--               （SWJ_Pins）。rsp_data(1..0) = 実行前の SWDIO 入力 / SWCLK 出力
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity swd_phy is
  port (
    clk              : in  std_logic;
    rst              : in  std_logic;
    -- 設定（命令の実行中は変えないこと）
    enable           : in  std_logic;                       -- '0' でピンを解放（DAP_Disconnect）
    half_period      : in  unsigned(15 downto 0);           -- SWCLK の半周期（clk 周期数, 2 以上）
    turnaround       : in  natural range 1 to 4;
    data_phase       : in  std_logic;                       -- WAIT / FAULT でもデータ相を出す
    idle_cycles      : in  natural range 0 to 255;          -- 転送後に SWDIO = 0 で回す周期数
    sample_delay     : in  natural range 0 to 3;            -- 入力ビットの取り込みを遅らせる周期数
    -- 命令
    cmd_valid        : in  std_logic;
    cmd_ready        : out std_logic;
    cmd_op           : in  std_logic_vector(1 downto 0);
    cmd_count        : in  natural range 1 to 64;
    cmd_request      : in  std_logic_vector(3 downto 0);
    cmd_data         : in  std_logic_vector(63 downto 0);
    rsp_valid        : out std_logic;
    rsp_ack          : out std_logic_vector(2 downto 0);
    rsp_data         : out std_logic_vector(63 downto 0);
    rsp_parity_error : out std_logic;
    -- ピン
    swclk            : out std_logic;
    swclk_oe         : out std_logic;
    swdio_o          : out std_logic;
    swdio_oe         : out std_logic;
    swdio_i          : in  std_logic
  );
end entity;

architecture rtl of swd_phy is
  constant OP_SEQ_OUT  : std_logic_vector(1 downto 0) := "00";
  constant OP_SEQ_IN   : std_logic_vector(1 downto 0) := "01";
  constant OP_TRANSFER : std_logic_vector(1 downto 0) := "10";
  constant OP_PINS     : std_logic_vector(1 downto 0) := "11";

  constant ACK_OK    : std_logic_vector(2 downto 0) := "001";
  constant ACK_WAIT  : std_logic_vector(2 downto 0) := "010";
  constant ACK_FAULT : std_logic_vector(2 downto 0) := "100";

  -- 相（同じ扱いのビットの並び）
  type phase_t is (P_NONE, P_SEQ_OUT, P_SEQ_IN, P_HEADER, P_TRN_ACK, P_ACK, P_RDATA, P_TRN_AFTER_READ,
                   P_TRN_BEFORE_WRITE, P_WDATA, P_DUMMY_READ, P_TRN_AFTER_FAIL, P_DUMMY_WRITE, P_BACKOFF,
                   P_IDLE);
  type half_t is (H_IDLE, H_LOW, H_HIGH);

  signal half      : half_t := H_IDLE;
  signal phase     : phase_t := P_NONE;
  signal timer     : unsigned(15 downto 0) := (others => '0');
  signal expired   : boolean := false;                      -- timer = 0（1 周期前に timer = 1 を見て決める。比較を
                                                            -- 多くのレジスタのクロックイネーブルの経路から外す。R24）
  signal bits_left : natural range 0 to 64 := 0;            -- 今のビットを含む残り
  signal tx        : std_logic_vector(63 downto 0) := (others => '1');   -- 出すビット（下位から）
  signal rx        : std_logic_vector(63 downto 0) := (others => '0');   -- 取り込んだビット（読みデータは上位から、
                                                                         -- シーケンスはビット番号の位置へ入る）
  signal ack       : std_logic_vector(2 downto 0) := (others => '0');
  signal parity    : std_logic := '0';                      -- 読みデータ + パリティの XOR（0 が正常）
  signal reading   : std_logic := '0';
  signal wdata     : std_logic_vector(31 downto 0) := (others => '0');
  signal count     : natural range 1 to 64 := 1;
  signal result    : std_logic_vector(63 downto 0) := (others => '0');

  signal sclk      : std_logic := '1';
  signal sdo       : std_logic := '1';
  signal sdo_en    : std_logic := '1';
  signal din       : std_logic := '1';
  -- 遅らせた取り込み（ビットの間隔 >= 4 周期 > sample_delay なので、保留は常に高々 1 件）
  signal pend_left : natural range 0 to 4 := 0;             -- 0 = 保留なし。1 になった周期に取り込む
  signal pend_kind : phase_t := P_NONE;
  signal pend_bit  : natural range 0 to 63 := 0;            -- P_SEQ_IN のビット番号
  signal done      : std_logic := '0';

  -- 送出順（下位から）: start, APnDP, RnW, A2, A3, parity, stop, park
  function header(request : std_logic_vector(3 downto 0)) return std_logic_vector is
    variable h : std_logic_vector(7 downto 0);
  begin
    h := "10" & (request(0) xor request(1) xor request(2) xor request(3)) & request(3) & request(2)
         & request(1) & request(0) & '1';
    return h;
  end function;

  function xor_all(v : std_logic_vector) return std_logic is
    variable r : std_logic := '0';
  begin
    for i in v'range loop
      r := r xor v(i);
    end loop;
    return r;
  end function;

  function drives(p : phase_t) return std_logic is
  begin
    case p is
      when P_SEQ_OUT | P_HEADER | P_WDATA | P_DUMMY_WRITE | P_IDLE => return '1';
      when others                                                  => return '0';
    end case;
  end function;
begin
  cmd_ready        <= '1' when half = H_IDLE and done = '0' and enable = '1' else '0';
  rsp_valid        <= done;
  rsp_ack          <= ack;
  rsp_data         <= result;
  rsp_parity_error <= parity;
  swclk            <= sclk;
  swclk_oe         <= enable;
  swdio_o          <= sdo;
  swdio_oe         <= sdo_en and enable;

  process (clk)
    -- 相の最後のビットの後に続く相（無ければ P_NONE）
    impure function following return phase_t is
    begin
      case phase is
        when P_HEADER     => return P_TRN_ACK;
        when P_TRN_ACK    => return P_ACK;
        when P_ACK =>
          if ack = ACK_OK then
            if reading = '1' then
              return P_RDATA;
            end if;
            return P_TRN_BEFORE_WRITE;
          elsif ack = ACK_WAIT or ack = ACK_FAULT then
            if data_phase = '1' and reading = '1' then
              return P_DUMMY_READ;
            end if;
            return P_TRN_AFTER_FAIL;
          end if;
          return P_BACKOFF;                                     -- プロトコル異常: データ相の分だけ待つ
        when P_RDATA            => return P_TRN_AFTER_READ;
        when P_TRN_BEFORE_WRITE => return P_WDATA;
        when P_TRN_AFTER_READ | P_WDATA =>
          if idle_cycles /= 0 then
            return P_IDLE;
          end if;
          return P_NONE;
        when P_DUMMY_READ       => return P_TRN_AFTER_FAIL;
        when P_TRN_AFTER_FAIL =>
          if data_phase = '1' and reading = '0' then
            return P_DUMMY_WRITE;
          end if;
          return P_NONE;
        when others             => return P_NONE;
      end case;
    end function;

    impure function length(p : phase_t) return natural is
    begin
      case p is
        when P_TRN_ACK | P_TRN_AFTER_READ | P_TRN_BEFORE_WRITE | P_TRN_AFTER_FAIL => return turnaround;
        when P_ACK                                                               => return 3;
        when P_RDATA | P_WDATA | P_DUMMY_READ | P_DUMMY_WRITE                    => return 33;
        when P_BACKOFF                                                           => return turnaround + 33;
        when P_IDLE                                                              => return idle_cycles;
        when others                                                              => return 8;
      end case;
    end function;

    -- 相の 1 ビット目を始める（出すビットは bits の下位から）
    variable index : natural range 0 to 63;                -- P_SEQ_IN のビット番号

    -- 入力ビット 1 つを din から取り込む
    procedure take(kind : phase_t; index : natural) is
    begin
      case kind is
        when P_ACK =>
          ack <= din & ack(2 downto 1);
        when P_SEQ_IN =>
          for k in 0 to 63 loop                               -- ビット番号の位置へ（後でずらさない）
            if k = index then
              rx(k) <= din;
            end if;
          end loop;
        when P_RDATA =>
          rx     <= din & rx(63 downto 1);
          parity <= parity xor din;
        when others =>
          null;
      end case;
    end procedure;

    procedure begin_phase(p : phase_t; n : natural; bits : std_logic_vector(63 downto 0)) is
    begin
      phase     <= p;
      bits_left <= n;
      tx        <= bits;
      sdo       <= bits(0) when drives(p) = '1' else '1';
      sdo_en    <= drives(p);
      sclk      <= '0';
      half      <= H_LOW;
      timer     <= half_period - 1;
      expired   <= false;                                   -- half_period は 2 以上（timer は 1 以上から始まる）
    end procedure;
  begin
    if rising_edge(clk) then
      din <= swdio_i;
      done   <= '0';

      -- 予約した取り込み（Low 相の終わりから sample_delay 周期後）
      if pend_left = 1 then
        take(pend_kind, pend_bit);
      end if;
      if pend_left /= 0 then
        pend_left <= pend_left - 1;
      end if;

      if rst = '1' or enable = '0' then
        pend_left <= 0;
        half   <= H_IDLE;
        phase  <= P_NONE;
        sclk   <= '1';
        sdo    <= '1';
        sdo_en <= '1';
      else
        case half is
          when H_IDLE =>
            if done = '0' and cmd_valid = '1' then
              rx     <= (others => '0');
              parity <= '0';
              case cmd_op is
                when OP_PINS =>
                  result <= (63 downto 2 => '0') & din & sclk;
                  if cmd_data(8) = '1' then
                    sclk <= cmd_data(0);
                  end if;
                  if cmd_data(9) = '1' then
                    sdo    <= cmd_data(1);
                    sdo_en <= '1';
                  end if;
                  done <= '1';
                when OP_TRANSFER =>
                  reading <= cmd_request(1);
                  wdata   <= cmd_data(31 downto 0);
                  begin_phase(P_HEADER, 8, (63 downto 8 => '0') & header(cmd_request));
                when OP_SEQ_IN =>
                  count <= cmd_count;
                  begin_phase(P_SEQ_IN, cmd_count, (others => '1'));
                when others =>
                  count <= cmd_count;
                  begin_phase(P_SEQ_OUT, cmd_count, cmd_data);
              end case;
            end if;

          when H_LOW =>
            if not expired then
              timer   <= timer - 1;
              expired <= timer = 1;
            else
              -- Low 相の終わり: 取り込みを予約して立ち上げる
              index := 0;
              if phase = P_SEQ_IN then
                index := count - bits_left;
              end if;
              if phase = P_ACK or phase = P_SEQ_IN or phase = P_RDATA then
                pend_kind <= phase;
                pend_bit  <= index;
                pend_left <= sample_delay + 1;
              end if;
              sclk  <= '1';
              half  <= H_HIGH;
              timer <= half_period - 1;
              expired <= false;
            end if;

          when H_HIGH =>
            if not expired then
              timer   <= timer - 1;
              expired <= timer = 1;
            elsif bits_left > 1 then
              bits_left <= bits_left - 1;
              tx        <= '1' & tx(63 downto 1);
              if drives(phase) = '1' then
                sdo <= tx(1);
              end if;
              sclk  <= '0';
              half  <= H_LOW;
              timer <= half_period - 1;
              expired <= false;
            elsif pend_left /= 0 then
              null;                                           -- 相の最後のビットの取り込みを待つ
            else
              case following is
                when P_NONE =>
                  -- 終わり: SWDIO = 1 を駆動して待つ。結果を確定する
                  half   <= H_IDLE;
                  phase  <= P_NONE;
                  sdo    <= '1';
                  sdo_en <= '1';
                  done   <= '1';
                  if phase = P_SEQ_IN then
                    result <= rx;
                  elsif phase = P_SEQ_OUT then
                    result <= (others => '0');
                  else
                    result <= x"00000000" & rx(62 downto 31);  -- 読みデータ（上位から入れた 33 ビットの下 32）
                  end if;
                when P_WDATA =>
                  begin_phase(P_WDATA, 33, (63 downto 33 => '1') & xor_all(wdata) & wdata);
                when P_DUMMY_WRITE | P_IDLE =>
                  begin_phase(following, length(following), (others => '0'));
                when others =>
                  begin_phase(following, length(following), (others => '1'));
              end case;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
