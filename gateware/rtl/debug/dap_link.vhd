-- CMSIS-DAP のユニットから PHY（debug_port の swd_phy / jtag_phy）への命令。
-- 仕様: docs/superpowers/specs/2026-10-16-dap-processor-split-design.md §2、先出し（2 段）は
-- docs/superpowers/specs/2026-11-03-dap-line-rate-design.md §4
--
-- 転送（OP_TRANSFER）: WAIT は wait_retry 回まで再試行し、応答を CMSIS-DAP の転送応答に符号化して返す
-- （OK でも読みのパリティ異常は ERROR）。未接続（enable = '0'）なら PHY に出さず無応答（7）。
-- それ以外の命令（シーケンス・ピン）: 結果は val。未接続なら val = 0。
-- stamp: 応答の転送の線上の点の時刻（PHY の rsp_time の下位 32 ビット。DAP_Transfer のタイムスタンプ）。未接続の応答は 0。
--
-- 2 段の待ち行列: ユニットは応答を待たずに次の命令を出せる（受けて応答をまだ取っていない命令は 2 つまで。ユニットが数えて
-- 守る）。PHY には古い方から出す。前の転送の応答の前に出す次の転送は条件つき（phy_cond = 1: PHY は前の転送が OK のときだけ
-- 受ける。swd_phy / jtag_phy の先頭のコメント）なので、前が失敗した転送の後の転送は線に出ない（誤りの確認の原則）。
-- 応答（ack）は命令の順に、ユニットが取る（ack_take）まで保つ。応答が失敗（OK でない転送）なら、その後ろの命令を捨て、
-- その失敗をユニットが取るまで（取る周期を含む）に来た命令も捨てる（ユニットは失敗を取ったら、出して応答の無い命令は
-- 捨てられたと数える）。resp / ok / rdata / val は ack の間はその応答、取った後は次の応答が来るまで取った応答のまま
-- （シーケンスのユニットは応答の値を後の状態で使う）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.dap_pkg.all;

entity dap_link is
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    enable       : in  std_logic;
    wait_retry   : in  unsigned(15 downto 0);
    -- ユニット側
    req          : in  std_logic;                         -- 1 周期: 命令を入れる
    op           : in  std_logic_vector(1 downto 0);
    count        : in  natural range 1 to 64;
    request      : in  std_logic_vector(3 downto 0);
    data         : in  std_logic_vector(63 downto 0);
    ack          : out std_logic;                         -- いちばん古い命令の応答がある（ack_take の周期まで保つ）
    ack_take     : in  std_logic;                         -- ack を取った（その辺で次の応答へ）
    resp         : out std_logic_vector(7 downto 0);
    ok           : out boolean;                           -- resp = RESP_OK
    rdata        : out std_logic_vector(31 downto 0);
    val          : out std_logic_vector(63 downto 0);
    stamp        : out std_logic_vector(31 downto 0);
    -- PHY 側
    phy_valid    : out std_logic;
    phy_ready    : in  std_logic;
    phy_cond     : out std_logic;                         -- 前の転送が OK のときだけ受けること
    phy_op       : out std_logic_vector(1 downto 0);
    phy_count    : out natural range 1 to 64;
    phy_request  : out std_logic_vector(3 downto 0);
    phy_data     : out std_logic_vector(63 downto 0);
    phy_rsp_valid : in std_logic;
    phy_rsp_ack  : in  std_logic_vector(2 downto 0);
    phy_rsp_data : in  std_logic_vector(63 downto 0);
    phy_rsp_parity : in std_logic;
    phy_rsp_time : in  unsigned(31 downto 0)
  );
end entity;

architecture rtl of dap_link is
  type entry_t is record
    op       : std_logic_vector(1 downto 0);
    count    : natural range 1 to 64;
    request  : std_logic_vector(3 downto 0);
    data     : std_logic_vector(63 downto 0);
    retry    : unsigned(15 downto 0);
    issued   : boolean;                                   -- PHY が受けた（応答待ちか応答済み）
    answered : boolean;                                   -- 応答がある
    resp     : std_logic_vector(7 downto 0);
    val      : std_logic_vector(63 downto 0);
    stamp    : std_logic_vector(31 downto 0);
  end record;
  constant ENTRY_NONE : entry_t := (OP_SEQ_OUT, 1, (others => '0'), (others => '0'), (others => '0'), false, false,
                                    (others => '0'), (others => '0'), (others => '0'));
  type entries_t is array (0 to 1) of entry_t;
  signal e        : entries_t := (others => ENTRY_NONE);
  signal n        : natural range 0 to 2 := 0;            -- 入っている命令の数（e(0) が古い）
  signal failing  : boolean := false;
  signal taken    : entry_t := ENTRY_NONE;                -- 最後に取った応答
  signal shown    : entry_t;                              -- 出している応答                     -- 失敗の応答をユニットがまだ取っていない（来た命令は捨てる）
  signal pick     : natural range 0 to 1;                 -- PHY に出している命令
  signal present  : boolean;
begin
  -- PHY に出す: 古い方がまだ出ていなければそれ（条件なし）。古い方が出て応答待ちなら次（条件つき）。古い方が応答済み（OK。
  -- 失敗なら次は捨ててある）なら次を条件なしで
  process (all)
  begin
    pick    <= 0;
    present <= false;
    if n >= 1 and not e(0).issued then
      present <= true;
    elsif n = 2 and not e(1).issued then
      pick    <= 1;
      present <= true;
    end if;
  end process;
  phy_valid   <= '1' when present else '0';
  phy_cond    <= '1' when pick = 1 and not e(0).answered else '0';
  phy_op      <= e(pick).op;
  phy_count   <= e(pick).count;
  phy_request <= e(pick).request;
  phy_data    <= e(pick).data;

  ack   <= '1' when n >= 1 and e(0).answered else '0';
  shown <= e(0) when n >= 1 and e(0).answered else taken;
  resp  <= shown.resp;
  ok    <= shown.resp = RESP_OK;
  rdata <= shown.val(31 downto 0);
  val   <= shown.val;
  stamp <= shown.stamp;

  process (clk)
    variable v        : entries_t;
    variable m        : natural range 0 to 2;
    variable fail_new : boolean;                          -- この辺で失敗の応答ができた
    variable took_fail : boolean;                         -- この辺でユニットが失敗の応答を取った
    variable a        : natural range 0 to 1;
    variable r        : std_logic_vector(7 downto 0);

    procedure answer(k : natural; code : std_logic_vector(7 downto 0); value : std_logic_vector(63 downto 0);
                     at : std_logic_vector(31 downto 0)) is
    begin
      v(k).answered := true;
      v(k).resp     := code;
      v(k).val      := value;
      v(k).stamp    := at;
      if v(k).op = OP_TRANSFER and code /= RESP_OK then
        fail_new := true;                                 -- 後ろの命令を捨てる
        m        := k + 1;
      end if;
    end procedure;
  begin
    if rising_edge(clk) then
      v         := e;
      m         := n;
      fail_new  := false;
      took_fail := false;
      if rst = '1' then
        e       <= (others => ENTRY_NONE);
        n       <= 0;
        failing <= false;
      else
        -- PHY が受けた
        if present and phy_ready = '1' then
          v(pick).issued := true;
        end if;
        -- PHY の応答（いちばん古い応答待ちの命令のもの）
        if phy_rsp_valid = '1' then
          a := 0;
          if v(0).answered then
            a := 1;
          end if;
          if v(a).op /= OP_TRANSFER then
            answer(a, RESP_OK, phy_rsp_data, std_logic_vector(phy_rsp_time));
          elsif phy_rsp_ack = ACK_WAIT and v(a).retry /= 0 then
            v(a).retry  := v(a).retry - 1;
            v(a).issued := false;                         -- WAIT: 再試行（次の命令は条件つきなので受けられていない）
          else
            r := transfer_response(phy_rsp_ack, phy_rsp_parity, v(a).request(1));
            answer(a, r, phy_rsp_data, std_logic_vector(phy_rsp_time));
          end if;
        end if;
        -- 未接続: 出す前の命令は PHY に出さず無応答
        if enable = '0' then
          for k in 0 to 1 loop
            if k < m and not v(k).issued and not v(k).answered then
              answer(k, RESP_NO_ACK, (others => '0'), (others => '0'));
            end if;
          end loop;
        end if;
        -- ユニットが応答を取った（取ったのは前の周期に ack に出ていた応答）
        if ack_take = '1' and n >= 1 and e(0).answered then
          took_fail := e(0).op = OP_TRANSFER and e(0).resp /= RESP_OK;
          taken     <= e(0);
          v(0) := v(1);
          v(1) := ENTRY_NONE;
          m    := m - 1;
        end if;
        -- 新しい命令（失敗の応答をユニットが取るまで、取る周期を含めて捨てる）
        if req = '1' and not failing and not fail_new then
          assert m < 2 report "dap_link: queue overflow" severity failure;
          if m < 2 then
            v(m) := (op => op, count => count, request => request, data => data, retry => wait_retry,
                     issued => false, answered => false, resp => (others => '0'), val => (others => '0'),
                     stamp => (others => '0'));
            m := m + 1;
          end if;
        end if;
        for k in 0 to 1 loop
          if k >= m then
            v(k) := ENTRY_NONE;
          end if;
        end loop;
        e       <= v;
        n       <= m;
        failing <= (failing and not took_fail) or fail_new;
      end if;
    end if;
  end process;
end architecture;
