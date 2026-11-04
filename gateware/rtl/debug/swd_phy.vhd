-- SWD の物理層: SWCLK / SWDIO を駆動し、ビット列と 1 トランザクションを実行する。
-- 仕様: docs/superpowers/specs/2026-10-15-m4-swd-cmsis-dap-design.md §3、速さ（sys/2・転送の間の空き 0）は
-- docs/superpowers/specs/2026-11-03-dap-line-rate-design.md。振る舞いは ARM の CMSIS-DAP 参照実装（SW_DP.c）に合わせる。
--
-- ビットのタイミング: 1 ビット = Low 相（SWCLK = 0）+ High 相（SWCLK = 1）、各 half_period 周期（1 以上。1 で SWCLK = sys / 2）。
--   出力ビットは Low 相の頭で SWDIO に出す（ターゲットは立ち上がりで取り込む）。
--   入力ビットは Low 相の終わりで取り込む（ターゲットは立ち上がりの後に変える）。SWDIO はこちらの SWCLK に
--   同期して変わる信号なので、同期化は 1 段（I/O の FF）だけにする。サンプル点は Low 相の終わり（立ち上げる周期）
--   から sample_delay 周期後（往復遅延の補償。ピンの値は立ち上がりの後もターゲットの出力遅延の間は前のビットのまま）。
--   取り込みは Low 相の終わりで予約し、遅延線（段 k = k 周期前に予約したビット）の段 sample_delay から din（1 周期前の
--   ピン）を取る。ビットの間隔（2 × half_period）より sample_delay が長ければ、複数のビットの取り込みが遅延線に並ぶ。
--   相の最後のビットの値で次を決める（ACK・読みのパリティ）ときは、その取り込みが済むまで最後の High 相を延ばして待つ。
--   アイドル時は SWCLK = 1、SWDIO = 1 を駆動する（enable = '0' なら両方 Hi-Z）。
--
-- 命令（cmd_valid / cmd_ready）:
--   OP_SEQ_OUT  cmd_count ビット（1〜64）を cmd_data の下位から出す
--   OP_SEQ_IN   cmd_count ビットを読み、rsp_data の下位から詰める（SWDIO は解放）
--   OP_TRANSFER cmd_request（CMSIS-DAP の要求: bit0 APnDP, bit1 RnW, bit2-3 A[3:2]）と cmd_data(31..0)（書き込み値）。
--               rsp_ack（001 OK / 010 WAIT / 100 FAULT / それ以外はプロトコル異常）、rsp_data(31..0)（読み値）、
--               rsp_parity_error（読みデータのパリティ異常）。cmd_count は見ない（jtag_phy のポストの DP の読みは JTAG だけ）
--   OP_PINS     cmd_data(8) = 1 なら SWCLK を cmd_data(0) に、cmd_data(9) = 1 なら SWDIO を cmd_data(1) にする
--               （SWJ_Pins）。rsp_data(1..0) = 実行前の SWDIO 入力 / SWCLK 出力
--
-- 続けて受ける（転送の間の空き 0）: 命令の最後のビットの High 相の終わりの周期に cmd_valid なら、その周期に次の命令を
--   受け（cmd_ready = 1）、同じ辺で次の命令の最初のビットの Low 相を始める。完了の rsp_valid（1 周期）はその辺の後の周期に
--   出るので、次の命令の受理（cmd_valid and cmd_ready）は前の命令の rsp_valid より 1 周期前になりうる（観測する側は
--   受理と応答の対を高々 2 つ重ねて数える）。rsp_ack / rsp_data / rsp_parity_error / rsp_time は rsp_valid の周期から
--   次の rsp_valid まで保つ。OP_PINS は前の命令が終わってから（High 相の終わりでは受けない）。
-- cmd_cond（条件つきの命令）: '1' の命令は、直前に終わった命令が OK の転送（ACK OK でパリティ異常なし）か転送でない命令の
--   ときだけ受ける。そうでなければ受けない（cmd_ready = 0 のまま。出した側は応答を見て取り下げる）。dap_link が次の転送を
--   前の転送の応答の前に出しておき、前が失敗したら線に出さないために使う（誤りの後の転送を線に出さない原則）。
--
-- 線上の点の時刻（rsp_time。docs/superpowers/specs/2026-11-01-time-accuracy-design.md R5）: OP_TRANSFER は ACK の 3 ビット目の
--   Low 相が終わって SWCLK が 1 になった最初の周期の now（ACK の値に依らない。WAIT・FAULT も同じ点）。ほかの命令は命令を
--   受理した周期の now。ヘッダの最初のビットの Low 相の始め（SWCLK が 0 になった最初の周期）から、この点までは
--   (10 + turnaround) × 2 × half_period + half_period 周期（ホストの fprobe.timing と cocotb が確かめる）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.dap_pkg.all;

entity swd_phy is
  port (
    clk              : in  std_logic;
    rst              : in  std_logic;
    -- 設定（命令の実行中は変えないこと）
    enable           : in  std_logic;                       -- '0' でピンを解放（DAP_Disconnect）
    half_period      : in  unsigned(15 downto 0);           -- SWCLK の半周期（clk 周期数, 1 以上）
    turnaround       : in  natural range 1 to 4;
    data_phase       : in  std_logic;                       -- WAIT / FAULT でもデータ相を出す
    idle_cycles      : in  natural range 0 to 255;          -- 転送後に SWDIO = 0 で回す周期数
    sample_delay     : in  natural range 0 to 7;            -- 入力ビットの取り込みを遅らせる周期数
    now              : in  unsigned(63 downto 0);           -- タイムベース（rsp_time に取る）
    -- 命令
    cmd_valid        : in  std_logic;
    cmd_ready        : out std_logic;
    cmd_cond         : in  std_logic := '0';                -- 直前の命令が OK のときだけ受ける（先頭のコメント）
    cmd_op           : in  std_logic_vector(1 downto 0);
    cmd_count        : in  natural range 1 to 64;
    cmd_request      : in  std_logic_vector(3 downto 0);
    cmd_data         : in  std_logic_vector(63 downto 0);
    rsp_valid        : out std_logic;
    rsp_ack          : out std_logic_vector(2 downto 0);
    rsp_data         : out std_logic_vector(63 downto 0);
    rsp_parity_error : out std_logic;
    rsp_time         : out unsigned(63 downto 0);           -- 線上の点の時刻（先頭のコメント）
    -- ピン
    swclk            : out std_logic;
    swclk_oe         : out std_logic;
    swdio_o          : out std_logic;
    swdio_oe         : out std_logic;
    swdio_i          : in  std_logic
  );
end entity;

architecture rtl of swd_phy is
  -- 相（同じ扱いのビットの並び）
  type phase_t is (P_NONE, P_SEQ_OUT, P_SEQ_IN, P_HEADER, P_TRN_ACK, P_ACK, P_RDATA, P_TRN_AFTER_READ,
                   P_TRN_BEFORE_WRITE, P_WDATA, P_DUMMY_READ, P_TRN_AFTER_FAIL, P_DUMMY_WRITE, P_BACKOFF,
                   P_IDLE);
  type half_t is (H_IDLE, H_LOW, H_HIGH);

  signal half      : half_t := H_IDLE;
  signal phase     : phase_t := P_NONE;
  signal timer     : unsigned(15 downto 0) := (others => '0');
  signal expired   : boolean := false;                      -- 相の最後の周期（timer = 0。1 周期前に timer = 1 を見て決める。
                                                            -- 比較を多くのレジスタのクロックイネーブルの経路から外す。R24）
  signal hp_one    : boolean;                               -- half_period = 1（相が 1 周期）
  signal bits_left : natural range 0 to 255 := 0;           -- 今のビットを含む残り（アイドル相は idle_cycles で最大 255）
  signal tx        : std_logic_vector(63 downto 0) := (others => '1');   -- 出すビット（下位から）
  signal rx        : std_logic_vector(63 downto 0) := (others => '0');   -- 取り込んだビット（読みデータは上位から、
                                                                         -- シーケンスはビット番号の位置へ入る）
  signal ack       : std_logic_vector(2 downto 0) := (others => '0');
  signal parity    : std_logic := '0';                      -- 読みデータ + パリティの XOR（0 が正常）
  signal reading   : std_logic := '0';
  signal xfer      : boolean := false;                      -- 実行中（か直前に終えた）命令は転送
  signal last_ok   : boolean := true;                       -- 直前に終えた命令が OK の転送か転送でない命令（cmd_cond）
  signal wdata     : std_logic_vector(31 downto 0) := (others => '0');
  signal count     : natural range 1 to 64 := 1;

  signal sclk      : std_logic := '1';
  signal sdo       : std_logic := '1';
  signal sdo_en    : std_logic := '1';
  signal din       : std_logic := '1';
  -- 遅らせた取り込みの遅延線: 段 k は k 周期前の辺（Low 相の終わり）で予約したビット。段 sample_delay を取り込む
  type take_t is record
    valid : std_logic;
    kind  : phase_t;                                        -- P_ACK / P_SEQ_IN / P_RDATA
    bit_n : natural range 0 to 63;                          -- P_SEQ_IN のビット番号
  end record;
  constant TAKE_NONE : take_t := ('0', P_NONE, 0);
  type take_line_t is array (0 to 7) of take_t;
  signal take_line : take_line_t := (others => TAKE_NONE);
  signal done      : std_logic := '0';
  signal point     : unsigned(63 downto 0) := (others => '0');   -- この命令の rsp_time（作業中）
  signal mark      : std_logic := '0';                      -- 前の周期に ACK の 3 ビット目の後で SWCLK を上げた
  -- 応答（rsp_valid の周期から次の rsp_valid まで保つ。次の命令を同じ辺で始めても変わらない）
  signal r_ack     : std_logic_vector(2 downto 0) := (others => '0');
  signal r_data    : std_logic_vector(63 downto 0) := (others => '0');
  signal r_parity  : std_logic := '0';
  signal r_time    : unsigned(63 downto 0) := (others => '0');

  -- 送出順（下位から）: start, APnDP, RnW, A2, A3, parity, stop, park
  function header(request : std_logic_vector(3 downto 0)) return std_logic_vector is
    variable h : std_logic_vector(7 downto 0);
  begin
    h := "10" & (request(0) xor request(1) xor request(2) xor request(3)) & request(3) & request(2)
         & request(1) & request(0) & '1';
    return h;
  end function;

  function drives(p : phase_t) return std_logic is
  begin
    case p is
      when P_SEQ_OUT | P_HEADER | P_WDATA | P_DUMMY_WRITE | P_IDLE => return '1';
      when others                                                  => return '0';
    end case;
  end function;

  -- 相の最後のビットの後に続く相（無ければ P_NONE）
  function following_of(p : phase_t; a : std_logic_vector(2 downto 0); rd, dphase : std_logic;
                        idle : natural) return phase_t is
  begin
    case p is
      when P_HEADER     => return P_TRN_ACK;
      when P_TRN_ACK    => return P_ACK;
      when P_ACK =>
        if a = ACK_OK then
          if rd = '1' then
            return P_RDATA;
          end if;
          return P_TRN_BEFORE_WRITE;
        elsif a = ACK_WAIT or a = ACK_FAULT then
          if dphase = '1' and rd = '1' then
            return P_DUMMY_READ;
          end if;
          return P_TRN_AFTER_FAIL;
        end if;
        return P_BACKOFF;                                       -- プロトコル異常: データ相の分だけ待つ
      when P_RDATA            => return P_TRN_AFTER_READ;
      when P_TRN_BEFORE_WRITE => return P_WDATA;
      when P_TRN_AFTER_READ | P_WDATA =>
        if idle /= 0 then
          return P_IDLE;
        end if;
        return P_NONE;
      when P_DUMMY_READ       => return P_TRN_AFTER_FAIL;
      when P_TRN_AFTER_FAIL =>
        if dphase = '1' and rd = '0' then
          return P_DUMMY_WRITE;
        end if;
        return P_NONE;
      when others             => return P_NONE;
    end case;
  end function;

  -- この周期の辺で取り込む値を入れた後の値（取り込みと同じ辺で、その値で次を決める。half_period = 1・sample_delay = 0 で
  -- ACK・命令の終わりを延ばさない）
  signal ack_eff    : std_logic_vector(2 downto 0);
  signal parity_eff : std_logic;
  signal rx_eff     : std_logic_vector(63 downto 0);
  signal following : phase_t;
  signal drained   : boolean;                                -- この辺の後に取り込みの残りが無い（段 0〜sample_delay − 1 が空）
  signal ending    : boolean;                                -- この周期の辺で命令の最後のビットの High 相が終わる
  signal ok_now    : boolean;                                -- 終わる命令が OK（cmd_cond の判定）
  signal accept    : boolean;                                -- この周期の辺で次の命令を受ける
begin
  process (all)
    variable t : take_t;
    variable d : boolean;
  begin
    t := take_line(sample_delay);
    ack_eff    <= ack;
    parity_eff <= parity;
    rx_eff     <= rx;
    if t.valid = '1' then
      case t.kind is
        when P_ACK =>
          ack_eff <= din & ack(2 downto 1);
        when P_SEQ_IN =>
          for k in 0 to 63 loop                                 -- ビット番号の位置へ（後でずらさない）
            if k = t.bit_n then
              rx_eff(k) <= din;
            end if;
          end loop;
        when P_RDATA =>
          rx_eff     <= din & rx(63 downto 1);
          parity_eff <= parity xor din;
        when others =>
          null;
      end case;
    end if;
    d := true;
    for k in 0 to 6 loop
      if k < sample_delay and take_line(k).valid = '1' then
        d := false;
      end if;
    end loop;
    drained <= d;
  end process;

  hp_one    <= half_period = 1;
  following <= following_of(phase, ack_eff, reading, data_phase, idle_cycles);
  ending <= half = H_HIGH and expired and bits_left <= 1 and drained and following = P_NONE;
  ok_now <= not xfer or (ack_eff = ACK_OK and (reading = '0' or parity_eff = '0'));
  accept <= enable = '1' and rst = '0' and cmd_valid = '1' and
            ((half = H_IDLE and (cmd_cond = '0' or last_ok)) or
             (ending and cmd_op /= OP_PINS and (cmd_cond = '0' or ok_now)));

  cmd_ready        <= '1' when accept else '0';
  rsp_valid        <= done;
  rsp_ack          <= r_ack;
  rsp_data         <= r_data;
  rsp_parity_error <= r_parity;
  rsp_time         <= r_time;
  swclk            <= sclk;
  swclk_oe         <= enable;
  swdio_o          <= sdo;
  swdio_oe         <= sdo_en and enable;

  process (clk)
    -- 半周期の時計: 積み直す（half_period 周期後に expired）・1 つ進める
    procedure half_restart is
    begin
      timer   <= half_period - 1;
      expired <= hp_one;
    end procedure;
    procedure half_tick is
    begin
      timer   <= timer - 1;
      expired <= timer = 1;
    end procedure;

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

    variable index : natural range 0 to 63;                -- P_SEQ_IN のビット番号

    -- 相の 1 ビット目を始める（出すビットは bits の下位から）
    procedure begin_phase(p : phase_t; n : natural; bits : std_logic_vector(63 downto 0)) is
    begin
      phase     <= p;
      bits_left <= n;
      tx        <= bits;
      sdo       <= bits(0) when drives(p) = '1' else '1';
      sdo_en    <= drives(p);
      sclk      <= '0';
      half      <= H_LOW;
      half_restart;
    end procedure;

    -- 命令を受けて始める（OP_PINS はこの辺で終える）
    procedure start_command is
    begin
      rx     <= (others => '0');
      parity <= '0';
      point  <= now;                                        -- 転送でなければこの値（転送は ACK の後の点で書き直す）
      xfer   <= cmd_op = OP_TRANSFER;
      case cmd_op is
        when OP_PINS =>
          r_data <= (63 downto 2 => '0') & din & sclk;
          r_ack  <= (others => '0');
          r_parity <= '0';
          r_time <= now;
          if cmd_data(8) = '1' then
            sclk <= cmd_data(0);
          end if;
          if cmd_data(9) = '1' then
            sdo    <= cmd_data(1);
            sdo_en <= '1';
          end if;
          last_ok <= true;
          done    <= '1';
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
    end procedure;
  begin
    if rising_edge(clk) then
      din    <= swdio_i;
      done   <= '0';
      mark   <= '0';
      if mark = '1' then
        point <= now;                                         -- SWCLK が 1 になった最初の周期
      end if;

      -- 予約した取り込み（Low 相の終わりから sample_delay 周期後）。遅延線を進める（段 sample_delay は取り込んで空ける）
      ack    <= ack_eff;
      parity <= parity_eff;
      rx     <= rx_eff;
      take_line(0) <= TAKE_NONE;
      for k in 1 to 7 loop
        take_line(k) <= take_line(k - 1);
        if k - 1 = sample_delay then
          take_line(k) <= TAKE_NONE;
        end if;
      end loop;

      if rst = '1' or enable = '0' then
        take_line <= (others => TAKE_NONE);
        half    <= H_IDLE;
        phase   <= P_NONE;
        sclk    <= '1';
        sdo     <= '1';
        sdo_en  <= '1';
        last_ok <= true;
      else
        case half is
          when H_IDLE =>
            if accept then
              start_command;
            end if;

          when H_LOW =>
            if not expired then
              half_tick;
            else
              -- Low 相の終わり: 取り込みを予約して立ち上げる
              index := 0;
              if phase = P_SEQ_IN then
                index := count - bits_left;
              end if;
              if phase = P_ACK or phase = P_SEQ_IN or phase = P_RDATA then
                take_line(0) <= ('1', phase, index);
              end if;
              if phase = P_ACK and bits_left = 1 then
                mark <= '1';                                  -- 線上の点（次の周期の now を取る）
              end if;
              sclk    <= '1';
              half    <= H_HIGH;
              half_restart;
            end if;

          when H_HIGH =>
            if not expired then
              half_tick;
            elsif bits_left > 1 then
              bits_left <= bits_left - 1;
              tx        <= '1' & tx(63 downto 1);
              if drives(phase) = '1' and phase /= P_IDLE then       -- アイドル相は 0 のまま（64 ビットを超えても tx から 1 を出さない）
                sdo <= tx(1);
              end if;
              sclk    <= '0';
              half    <= H_LOW;
              half_restart;
            elsif not drained and (phase = P_ACK or following = P_NONE or drives(following) = '1') then
              -- 次を決める値（ACK・命令の結果）の取り込みを待つ。自分が線を駆動する相（読みの後の idle 等）の前も待つ: 遅れた
              -- 取り込みが自分の駆動とぶつかった線を読まないように（前は idle_cycles > 0 で読みのパリティを駆動の後に取った。
              -- レビュー 2026-11-03）
              null;
            else
              case following is
                when P_NONE =>
                  -- 終わり: 結果を確定する。次の命令を受けるならこの辺で始め、無ければ SWDIO = 1 を駆動して待つ
                  done    <= '1';
                  r_ack   <= ack_eff;
                  r_parity <= parity_eff;
                  r_time  <= point;
                  last_ok <= ok_now;
                  if phase = P_SEQ_IN then
                    r_data <= rx_eff;
                  elsif phase = P_SEQ_OUT then
                    r_data <= (others => '0');
                  else
                    r_data <= x"00000000" & rx_eff(62 downto 31);  -- 読みデータ（上位から入れた 33 ビットの下 32）
                  end if;
                  if not xfer then
                    r_ack    <= (others => '0');
                    r_parity <= '0';
                  end if;
                  if accept then
                    start_command;
                  else
                    half   <= H_IDLE;
                    phase  <= P_NONE;
                    sdo    <= '1';
                    sdo_en <= '1';
                  end if;
                when P_WDATA =>
                  begin_phase(P_WDATA, 33, (63 downto 33 => '1') & (xor wdata) & wdata);
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
