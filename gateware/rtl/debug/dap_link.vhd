-- CMSIS-DAP のユニットから PHY（debug_port の swd_phy / jtag_phy）への命令。
-- 仕様: docs/superpowers/specs/2026-10-16-dap-processor-split-design.md §2
--
-- 転送（OP_TRANSFER）: WAIT は wait_retry 回まで再試行し、応答を CMSIS-DAP の転送応答に符号化して返す
-- （OK でも読みのパリティ異常は ERROR）。未接続（enable = '0'）なら PHY に出さず無応答（7）。
-- それ以外の命令（シーケンス・ピン）: 結果は val。未接続なら val = 0。
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
    req          : in  std_logic;                         -- 1 周期
    op           : in  std_logic_vector(1 downto 0);
    count        : in  natural range 1 to 64;
    request      : in  std_logic_vector(3 downto 0);
    data         : in  std_logic_vector(63 downto 0);
    ack          : out std_logic;                         -- 1 周期。以下は次の req まで有効
    resp         : out std_logic_vector(7 downto 0);
    ok           : out boolean;                           -- resp = RESP_OK
    rdata        : out std_logic_vector(31 downto 0);
    val          : out std_logic_vector(63 downto 0);
    -- PHY 側
    phy_valid    : out std_logic;
    phy_ready    : in  std_logic;
    phy_op       : out std_logic_vector(1 downto 0);
    phy_count    : out natural range 1 to 64;
    phy_request  : out std_logic_vector(3 downto 0);
    phy_data     : out std_logic_vector(63 downto 0);
    phy_rsp_valid : in std_logic;
    phy_rsp_ack  : in  std_logic_vector(2 downto 0);
    phy_rsp_data : in  std_logic_vector(63 downto 0);
    phy_rsp_parity : in std_logic
  );
end entity;

architecture rtl of dap_link is
  type state_t is (S_IDLE, S_ISSUE, S_WAIT);
  signal state   : state_t := S_IDLE;
  signal valid_r : std_logic := '0';
  signal op_r    : std_logic_vector(1 downto 0) := OP_SEQ_OUT;
  signal count_r : natural range 1 to 64 := 1;
  signal req_r   : std_logic_vector(3 downto 0) := (others => '0');
  signal data_r  : std_logic_vector(63 downto 0) := (others => '0');
  signal retry   : unsigned(15 downto 0) := (others => '0');
  signal ack_r   : std_logic := '0';
  signal resp_r  : std_logic_vector(7 downto 0) := (others => '0');
  signal ok_r    : boolean := false;
  signal rdata_r : std_logic_vector(31 downto 0) := (others => '0');
  signal val_r   : std_logic_vector(63 downto 0) := (others => '0');
begin
  phy_valid   <= valid_r;
  phy_op      <= op_r;
  phy_count   <= count_r;
  phy_request <= req_r;
  phy_data    <= data_r;
  ack         <= ack_r;
  resp        <= resp_r;
  ok          <= ok_r;
  rdata       <= rdata_r;
  val         <= val_r;

  process (clk)
    procedure answer(r : std_logic_vector(7 downto 0)) is
    begin
      resp_r <= r;
      ok_r   <= r = RESP_OK;
      ack_r  <= '1';
      state  <= S_IDLE;
    end procedure;
  begin
    if rising_edge(clk) then
      ack_r <= '0';
      if phy_ready = '1' then
        valid_r <= '0';
      end if;

      if rst = '1' then
        state   <= S_IDLE;
        valid_r <= '0';
      else
        case state is
          when S_IDLE =>
            if req = '1' then
              op_r    <= op;
              count_r <= count;
              req_r   <= request;
              data_r  <= data;
              retry   <= wait_retry;
              state   <= S_ISSUE;
            end if;

          when S_ISSUE =>
            if enable = '0' then
              val_r <= (others => '0');
              answer(RESP_NO_ACK);
            elsif valid_r = '0' then
              valid_r <= '1';
              state   <= S_WAIT;
            end if;

          when S_WAIT =>
            if phy_rsp_valid = '1' then
              rdata_r <= phy_rsp_data(31 downto 0);
              val_r   <= phy_rsp_data;
              if op_r /= OP_TRANSFER then
                answer(RESP_OK);
              elsif phy_rsp_ack = "010" and retry /= 0 then
                retry <= retry - 1;
                state <= S_ISSUE;                                   -- WAIT: 再試行
              else
                answer(transfer_response(phy_rsp_ack, phy_rsp_parity, req_r(1)));
              end if;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
