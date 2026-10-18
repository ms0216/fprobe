-- JTAG の物理層: TCK / TMS / TDI を駆動し TDO を取る。命令 I/F は swd_phy と同じで、JTAG-DP の違いをここで吸収する
-- （dap_processor の転送処理とクロストリガの割り込みを SWD と共通にするため）。
-- 仕様: docs/superpowers/specs/2026-10-16-m5-jtag-design.md §2
--
-- 前提: TAP は Run-Test/Idle にいる（JTAG_Sequence でホストが入れる）。チェーン上の位置は dr_before / dr_after
-- （選んだ装置より TDO 側 / TDI 側の装置数）と ir_before / ir_after（その IR の合計ビット数）、ir_len で与える。
--
-- ビットのタイミングは swd_phy と同じ（Low 相の頭で TMS / TDI を変え、Low 相の終わりに TDO を取り込んで TCK を上げる）。
-- TAP は立ち上がりで TMS / TDI を取り込み、立ち下がりで TDO を変えるので、Low 相の終わりは TDO が落ち着いた時点。
--
-- 命令:
--   OP_SEQ_OUT  SWJ_Sequence: cmd_count ビットを cmd_data の下位から TMS に出す（TDI = 1）
--   OP_SEQ_IN   JTAG_Sequence: cmd_count ビット、TMS = cmd_request(0)、TDI = cmd_data、TDO を rsp_data の下位から
--   OP_TRANSFER SWD と同じ意味の転送（cmd_request: bit0 APnDP, bit1 RnW, bit2-3 A[3:2]）を JTAG-DP で行う:
--     AP → APACC。DP の A = 0: 読みは IDCODE 命令（32 ビット）、書きは ABORT。その他の DP → DPACC。
--     JTAG-DP は読み値が次のスキャンで返る。AP の読みは SWD と同じくポストのまま返し（dap_processor が RDBUFF で
--     回収する）、DP の読み（RDBUFF 以外）は続けて RDBUFF をスキャンして値を返す（SWD-DP と同じ見え方）。
--     ACK: JTAG の OK/FAULT（010）→ "001"、WAIT（001）→ "010"、それ以外 → "111"（プロトコル異常）。
--     IDCODE・ABORT のスキャンに ACK は無いので "001"。
--     IR は前回と同じなら省く。
--   OP_PINS     cmd_data(8/9/10) = 1 なら TCK / TMS / TDI を cmd_data(0/1/2) に。rsp_data(3..0) = TDO, TDI, TMS, TCK
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity jtag_phy is
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    -- 設定（命令の実行中は変えないこと）
    enable      : in  std_logic;
    half_period : in  unsigned(15 downto 0);
    idle_cycles : in  natural range 0 to 255;
    dr_before   : in  natural range 0 to 7;
    dr_after    : in  natural range 0 to 7;
    ir_before   : in  natural range 0 to 63;
    ir_after    : in  natural range 0 to 63;
    ir_len      : in  natural range 1 to 32;
    -- 命令（swd_phy と同じ）
    cmd_valid   : in  std_logic;
    cmd_ready   : out std_logic;
    cmd_op      : in  std_logic_vector(1 downto 0);
    cmd_count   : in  natural range 1 to 64;
    cmd_request : in  std_logic_vector(3 downto 0);
    cmd_data    : in  std_logic_vector(63 downto 0);
    rsp_valid   : out std_logic;
    rsp_ack     : out std_logic_vector(2 downto 0);
    rsp_data    : out std_logic_vector(63 downto 0);
    -- ピン
    tck         : out std_logic;
    tms         : out std_logic;
    tdi         : out std_logic;
    tdo         : in  std_logic
  );
end entity;

architecture rtl of jtag_phy is
  constant OP_SEQ_OUT  : std_logic_vector(1 downto 0) := "00";
  constant OP_SEQ_IN   : std_logic_vector(1 downto 0) := "01";
  constant OP_TRANSFER : std_logic_vector(1 downto 0) := "10";
  constant OP_PINS     : std_logic_vector(1 downto 0) := "11";
  constant IR_ABORT    : std_logic_vector(3 downto 0) := x"8";
  constant IR_DPACC    : std_logic_vector(3 downto 0) := x"A";
  constant IR_APACC    : std_logic_vector(3 downto 0) := x"B";
  constant IR_IDCODE   : std_logic_vector(3 downto 0) := x"E";
  constant RDBUFF_READ : std_logic_vector(34 downto 0) := x"00000000" & "111";   -- DPACC、A = 0xC、読み

  -- ビット列は区間の並びとして 1 ビットずつ作る（ビット列全体を組み立てない）:
  --   IR スキャン: G_IR_HEAD（TMS 1,1,0,0）→ G_IR_BEFORE（1）→ G_IR_VALUE → G_IR_AFTER（1）→ G_IR_TAIL（TMS 1,0）
  --   DR スキャン: G_DR_HEAD（TMS 1,0,0）→ G_DR_BEFORE（0）→ G_DR_BODY（本体、TDO を取る）→ G_DR_AFTER（0）→ G_DR_TAIL
  --   シフトする区間（BEFORE / VALUE / BODY / AFTER）の最後のビットだけ TMS = 1（Exit1 へ）。G_IDLE は TMS = 0。
  --   G_SEQ はシーケンス命令（cmd_count ビット）。長さ 0 の区間は飛ばす。
  type seg_t is (G_NONE, G_START, G_SEQ, G_IR_HEAD, G_IR_BEFORE, G_IR_VALUE, G_IR_AFTER, G_IR_TAIL,
                 G_DR_HEAD, G_DR_BEFORE, G_DR_BODY, G_DR_AFTER, G_DR_TAIL, G_IDLE);
  type half_t is (H_IDLE, H_LOW, H_HIGH);

  signal half      : half_t := H_IDLE;
  signal seg       : seg_t := G_NONE;
  signal timer     : unsigned(16 downto 0) := (others => '0');   -- half_period - 2 から数え、負になったら相の終わり
  signal index     : natural range 0 to 255 := 0;              -- 区間の中のビット番号
  signal length    : natural range 0 to 255 := 0;              -- シーケンスのビット数
  signal seg_len   : natural range 0 to 255 := 0;              -- 今の区間の長さ（区間に入るときに決める）
  signal shifting  : natural range 0 to 127 := 0;              -- シフトの残りビット数（1 なら最後 = TMS 1）
  signal din       : std_logic := '1';
  signal tck_r, tms_r, tdi_r : std_logic := '1';
  signal done      : std_logic := '0';
  signal ack       : std_logic_vector(2 downto 0) := (others => '0');
  signal result    : std_logic_vector(63 downto 0) := (others => '0');
  -- シーケンス
  signal seq_op    : std_logic_vector(1 downto 0) := OP_SEQ_OUT;
  signal seq_tms   : std_logic := '0';
  signal seq_out   : std_logic_vector(63 downto 0) := (others => '1');   -- 出すビット（下位から、毎ビットずらす）
  signal seq_in    : std_logic_vector(63 downto 0) := (others => '0');   -- 取り込んだ TDO（ビット番号の位置へ）
  -- 転送
  signal ir        : std_logic_vector(3 downto 0) := (others => '0');
  signal ir_valid  : boolean := false;                                   -- TAP の IR が ir_cur になっている
  signal ir_cur    : std_logic_vector(3 downto 0) := (others => '0');
  signal payload   : std_logic_vector(34 downto 0) := (others => '0');   -- RnW, A[3:2], データ
  signal dr_shift  : std_logic_vector(34 downto 0) := (others => '0');   -- 本体のシフトレジスタ（出す / 取る）
  signal short_dr  : boolean := false;                                   -- IDCODE（32 ビット、ヘッダなし）
  signal dp_read   : boolean := false;                                   -- RDBUFF をもう 1 回スキャンする

  function map_ack(a : std_logic_vector(2 downto 0)) return std_logic_vector is
  begin
    case a is
      when "010"  => return "001";                                       -- OK / FAULT
      when "001"  => return "010";                                       -- WAIT
      when others => return "111";
    end case;
  end function;
begin
  cmd_ready <= '1' when half = H_IDLE and seg = G_NONE and done = '0' and enable = '1' else '0';
  rsp_valid <= done;
  rsp_ack   <= ack;
  rsp_data  <= result;
  tck       <= tck_r;
  tms       <= tms_r;
  tdi       <= tdi_r;

  process (clk)
    -- 区間 s の長さ
    impure function length_of(s : seg_t) return natural is
    begin
      case s is
        when G_SEQ       => return length;                          -- 開始時に設定済み
        when G_IR_HEAD   => return 4;
        when G_IR_BEFORE => return ir_before;
        when G_IR_VALUE  => return ir_len;
        when G_IR_AFTER  => return ir_after;
        when G_IR_TAIL | G_DR_TAIL => return 2;
        when G_DR_HEAD   => return 3;
        when G_DR_BEFORE => return dr_before;
        when G_DR_BODY   =>
          if short_dr then
            return 32;
          end if;
          return 35;
        when G_DR_AFTER  => return dr_after;
        when G_IDLE      => return idle_cycles;
        when G_NONE | G_START => return 0;
      end case;
    end function;

    function successor(s : seg_t) return seg_t is
    begin
      case s is
        when G_IR_HEAD   => return G_IR_BEFORE;
        when G_IR_BEFORE => return G_IR_VALUE;
        when G_IR_VALUE  => return G_IR_AFTER;
        when G_IR_AFTER  => return G_IR_TAIL;
        when G_IR_TAIL   => return G_DR_HEAD;
        when G_DR_HEAD   => return G_DR_BEFORE;
        when G_DR_BEFORE => return G_DR_BODY;
        when G_DR_BODY   => return G_DR_AFTER;
        when G_DR_AFTER  => return G_DR_TAIL;
        when others      => return G_NONE;                          -- G_DR_TAIL / G_IDLE / G_SEQ は手順で決める
      end case;
    end function;

    -- 区間 s の i ビット目の TMS / TDI を出し、TCK を下げて Low 相を始める
    procedure emit(s : seg_t; i : natural) is
    begin
      case s is
        when G_SEQ =>
          if seq_op = OP_SEQ_OUT then
            tms_r <= seq_out(0);
            tdi_r <= '1';
          else
            tms_r <= seq_tms;
            tdi_r <= seq_out(0);
          end if;
        when G_IR_HEAD =>
          tms_r <= '1' when i < 2 else '0';
          tdi_r <= '1';
        when G_IR_BEFORE | G_IR_AFTER =>
          tms_r <= '1' when shifting = 1 else '0';
          tdi_r <= '1';                                                -- 他の装置は BYPASS
        when G_IR_VALUE =>
          tms_r <= '1' when shifting = 1 else '0';
          tdi_r <= '1';                                                -- 4 ビットを超える IR の上位は 1
          for k in 0 to 3 loop
            if k = i then
              tdi_r <= ir(k);
            end if;
          end loop;
        when G_DR_HEAD =>
          tms_r <= '1' when i = 0 else '0';
          tdi_r <= '0';
        when G_DR_BEFORE | G_DR_AFTER =>
          tms_r <= '1' when shifting = 1 else '0';
          tdi_r <= '0';
        when G_DR_BODY =>
          tms_r <= '1' when shifting = 1 else '0';
          tdi_r <= dr_shift(0);
        when G_IR_TAIL | G_DR_TAIL =>
          tms_r <= '1' when i = 0 else '0';                           -- → Update、→ Idle
          tdi_r <= '1';
        when others =>                                                -- G_IDLE
          tms_r <= '0';
          tdi_r <= '1';
      end case;
      tck_r <= '0';
      half  <= H_LOW;
      timer <= resize(half_period, 17) - 2;
    end procedure;

    -- 区間 s を始める（長さ 0 なら次の周期に飛ばす）
    procedure enter(s : seg_t) is
    begin
      seg     <= s;
      index   <= 0;
      seg_len <= length_of(s);
      if s = G_IR_BEFORE then
        shifting <= ir_before + ir_len + ir_after;
      elsif s = G_DR_BEFORE then
        if short_dr then
          shifting <= dr_before + 32 + dr_after;
        else
          shifting <= dr_before + 35 + dr_after;
        end if;
      end if;
    end procedure;

    procedure finish(a : std_logic_vector(2 downto 0); data : std_logic_vector(63 downto 0)) is
    begin
      ack    <= a;
      result <= data;
      enter(G_IDLE);
    end procedure;
  begin
    if rising_edge(clk) then
      din  <= tdo;
      done <= '0';

      if rst = '1' or enable = '0' then
        half     <= H_IDLE;
        seg      <= G_NONE;
        tck_r    <= '1';
        tms_r    <= '1';
        tdi_r    <= '1';
        ir_valid <= false;
      else
        case half is
          when H_IDLE =>
            if seg = G_NONE then
              if done = '0' and cmd_valid = '1' then
                case cmd_op is
                  when OP_PINS =>
                    result <= (63 downto 4 => '0') & din & tdi_r & tms_r & tck_r;
                    if cmd_data(8) = '1' then
                      tck_r <= cmd_data(0);
                    end if;
                    if cmd_data(9) = '1' then
                      tms_r <= cmd_data(1);
                    end if;
                    if cmd_data(10) = '1' then
                      tdi_r <= cmd_data(2);
                    end if;
                    ir_valid <= false;
                    done     <= '1';
                  when OP_SEQ_OUT | OP_SEQ_IN =>
                    seq_op   <= cmd_op;
                    seq_tms  <= cmd_request(0);
                    seq_out  <= cmd_data;
                    seq_in   <= (others => '0');
                    length   <= cmd_count;
                    seg_len  <= cmd_count;
                    ir_valid <= false;
                    seg      <= G_SEQ;
                    index    <= 0;
                  when others =>                                      -- OP_TRANSFER
                    payload  <= cmd_data(31 downto 0) & cmd_request(3 downto 1);
                    short_dr <= false;
                    dp_read  <= false;
                    if cmd_request(0) = '1' then
                      ir <= IR_APACC;
                    elsif cmd_request(3 downto 2) = "00" and cmd_request(1) = '1' then
                      ir       <= IR_IDCODE;
                      short_dr <= true;
                    elsif cmd_request(3 downto 2) = "00" then
                      ir <= IR_ABORT;
                    else
                      ir      <= IR_DPACC;
                      dp_read <= cmd_request(1) = '1' and cmd_request(3 downto 2) /= "11";
                    end if;
                    seg <= G_START;                                   -- 次の周期に IR の要否を決める
                end case;
              end if;
            elsif seg = G_START then
              -- 転送の開始: IR が違えば IR スキャンから
              dr_shift <= payload;
              if ir_valid and ir_cur = ir then
                enter(G_DR_HEAD);
              else
                ir_cur   <= ir;
                ir_valid <= true;
                enter(G_IR_HEAD);
              end if;
            elsif index = seg_len then
              -- 区間の終わり（長さ 0 の区間もここを通る）
              case seg is
                when G_SEQ =>
                  seg    <= G_NONE;
                  result <= seq_in;
                  done   <= '1';
                when G_DR_TAIL =>
                  if short_dr then
                    finish("001", x"00000000" & dr_shift(34 downto 3));      -- IDCODE: ACK は無い
                  elsif ir = IR_ABORT then
                    finish("001", (others => '0'));                       -- ABORT の取り込み値は不定（見ない）
                  elsif map_ack(dr_shift(2 downto 0)) /= "001" or not dp_read then
                    finish(map_ack(dr_shift(2 downto 0)), x"00000000" & dr_shift(34 downto 3));
                  else
                    dp_read <= false;                                     -- DPACC RDBUFF の読みで値を受け取る
                    dr_shift    <= RDBUFF_READ;
                    enter(G_DR_HEAD);
                  end if;
                when G_IDLE =>
                  seg  <= G_NONE;
                  done <= '1';
                when others =>
                  enter(successor(seg));
              end case;
            else
              emit(seg, index);
            end if;

          when H_LOW =>
            if timer(16) = '0' then
              timer <= timer - 1;
            else
              -- Low 相の終わり: TDO を取り込んで TCK を上げる
              if seg = G_SEQ then
                for k in 0 to 63 loop
                  if k = index then
                    seq_in(k) <= din;
                  end if;
                end loop;
                seq_out <= '1' & seq_out(63 downto 1);
              elsif seg = G_DR_BODY then
                dr_shift <= din & dr_shift(34 downto 1);
              end if;
              if seg = G_IR_BEFORE or seg = G_IR_VALUE or seg = G_IR_AFTER or
                 seg = G_DR_BEFORE or seg = G_DR_BODY or seg = G_DR_AFTER then
                shifting <= shifting - 1;
              end if;
              tck_r <= '1';
              half  <= H_HIGH;
              timer <= resize(half_period, 17) - 2;
            end if;

          when H_HIGH =>
            if timer(16) = '0' then
              timer <= timer - 1;
            else
              index <= index + 1;
              half  <= H_IDLE;                                        -- 次のビット（または区間の終わり）
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
