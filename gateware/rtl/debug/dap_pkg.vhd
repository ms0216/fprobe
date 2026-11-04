-- CMSIS-DAP のコマンド処理（dap_processor とその部品）で共有する定数・型・関数。
-- 仕様: docs/superpowers/specs/2026-10-16-dap-processor-split-design.md
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package dap_pkg is

  -- コマンド ID
  constant ID_INFO            : natural := 16#00#;
  constant ID_HOST_STATUS     : natural := 16#01#;
  constant ID_CONNECT         : natural := 16#02#;
  constant ID_DISCONNECT      : natural := 16#03#;
  constant ID_TRANSFER_CONFIG : natural := 16#04#;
  constant ID_TRANSFER        : natural := 16#05#;
  constant ID_TRANSFER_BLOCK  : natural := 16#06#;
  constant ID_TRANSFER_ABORT  : natural := 16#07#;
  constant ID_WRITE_ABORT     : natural := 16#08#;
  constant ID_DELAY           : natural := 16#09#;
  constant ID_RESET_TARGET    : natural := 16#0A#;
  constant ID_SWJ_PINS        : natural := 16#10#;
  constant ID_SWJ_CLOCK       : natural := 16#11#;
  constant ID_SWJ_SEQUENCE    : natural := 16#12#;
  constant ID_SWD_CONFIGURE   : natural := 16#13#;
  constant ID_JTAG_SEQUENCE   : natural := 16#14#;
  constant ID_JTAG_CONFIGURE  : natural := 16#15#;
  constant ID_JTAG_IDCODE     : natural := 16#16#;
  constant ID_SWO_TRANSPORT   : natural := 16#17#;
  constant ID_SWO_MODE        : natural := 16#18#;
  constant ID_SWO_BAUDRATE    : natural := 16#19#;
  constant ID_SWO_CONTROL     : natural := 16#1A#;
  constant ID_SWO_STATUS      : natural := 16#1B#;
  constant ID_SWO_DATA        : natural := 16#1C#;
  constant ID_SWD_SEQUENCE    : natural := 16#1D#;

  -- コマンドの状態
  constant DAP_OK    : std_logic_vector(7 downto 0) := x"00";
  constant DAP_ERROR : std_logic_vector(7 downto 0) := x"FF";

  -- 転送要求のビット（bit0 APnDP, bit1 RnW, bit2-3 A[3:2], bit4 値一致, bit5 一致マスク, bit7 タイムスタンプ）
  constant REQ_APNDP       : natural := 0;
  constant REQ_RNW         : natural := 1;
  constant REQ_MATCH_VALUE : natural := 4;
  constant REQ_MATCH_MASK  : natural := 5;
  constant REQ_TIMESTAMP   : natural := 7;
  -- 転送要求の 4 ビット（bit3-2 A[3:2], bit1 RnW, bit0 APnDP）
  constant DP_W_ABORT      : std_logic_vector(3 downto 0) := "0000";   -- A = 0x0
  constant DP_R_IDCODE     : std_logic_vector(3 downto 0) := "0010";   -- A = 0x0, RnW（DPIDR / JTAG の IDCODE）
  constant DP_R_CTRL       : std_logic_vector(3 downto 0) := "0110";   -- A = 0x4, RnW（CTRL/STAT）
  constant DP_W_SELECT     : std_logic_vector(3 downto 0) := "1000";   -- A = 0x8
  constant DP_R_RDBUFF     : std_logic_vector(3 downto 0) := "1110";   -- A = 0xC, RnW
  constant AP_W_CSW        : std_logic_vector(3 downto 0) := "0001";
  constant AP_R_CSW        : std_logic_vector(3 downto 0) := "0011";
  constant AP_W_TAR        : std_logic_vector(3 downto 0) := "0101";
  constant AP_R_TAR        : std_logic_vector(3 downto 0) := "0111";
  constant AP_W_DRW        : std_logic_vector(3 downto 0) := "1101";
  constant AP_R_DRW        : std_logic_vector(3 downto 0) := "1111";
  -- DP のレジスタのビット
  constant STICKYERR       : natural := 5;                                 -- CTRL/STAT.STICKYERR
  constant STKERRCLR       : std_logic_vector(31 downto 0) := x"00000004"; -- ABORT.STKERRCLR

  -- 転送応答
  constant RESP_OK       : std_logic_vector(7 downto 0) := x"01";
  constant RESP_NO_ACK   : std_logic_vector(7 downto 0) := x"07";      -- 未接続も無応答と同じ
  constant RESP_ERROR    : std_logic_vector(7 downto 0) := x"08";      -- パリティ異常
  constant RESP_MISMATCH : std_logic_vector(7 downto 0) := x"10";

  -- PHY（swd_phy / jtag_phy）の命令
  constant OP_SEQ_OUT  : std_logic_vector(1 downto 0) := "00";
  constant OP_SEQ_IN   : std_logic_vector(1 downto 0) := "01";
  constant OP_TRANSFER : std_logic_vector(1 downto 0) := "10";
  constant OP_PINS     : std_logic_vector(1 downto 0) := "11";
  -- 転送の cmd_count: ポストの DP の読み（jtag_phy は続く RDBUFF をスキャンしない）
  constant COUNT_POSTED : natural := 2;

  -- PHY の転送の応答（ACK の 3 ビット）
  constant ACK_OK    : std_logic_vector(2 downto 0) := "001";
  constant ACK_WAIT  : std_logic_vector(2 downto 0) := "010";
  constant ACK_FAULT : std_logic_vector(2 downto 0) := "100";

  -- コマンドを受け持つユニット
  type dap_unit_t is (U_NONE, U_CONFIG, U_SWO, U_SEQUENCE, U_TRANSFER);
  function unit_of(id : natural) return dap_unit_t;

  -- JTAG のチェーン（DAP_JTAG_Configure で最大 8 台）
  constant JTAG_MAX_DEVICES : positive := 8;

  -- v の i 番目のバイト（下位から）。範囲が定数のスライスだけで書く（合成ツールの制約）
  function byte_of(v : std_logic_vector(63 downto 0); i : natural) return std_logic_vector;
  function bytes_for(bits : natural) return natural;
  -- 応答の 1 バイト b を、put の値（64 ビット、下位から送る）にする
  function byte_word(b : std_logic_vector(7 downto 0)) return std_logic_vector;
  -- 1 µs の周期数（四捨五入。DAP_Delay・SWJ_Pins の待ち）
  function us_cycles(clk_hz : positive) return positive;
  -- 転送応答: OK / WAIT / FAULT はそのまま、それ以外（無応答 111 等）は 3 ビットの値。OK でもパリティ異常は ERROR
  function transfer_response(ack : std_logic_vector(2 downto 0); parity_error, reading : std_logic)
    return std_logic_vector;

end package;

package body dap_pkg is

  function unit_of(id : natural) return dap_unit_t is
  begin
    case id is
      when ID_INFO | ID_HOST_STATUS | ID_CONNECT | ID_DISCONNECT | ID_TRANSFER_CONFIG | ID_DELAY |
           ID_RESET_TARGET | ID_SWJ_CLOCK | ID_SWD_CONFIGURE | ID_JTAG_CONFIGURE =>
        return U_CONFIG;
      when ID_SWO_TRANSPORT | ID_SWO_MODE | ID_SWO_BAUDRATE | ID_SWO_CONTROL | ID_SWO_STATUS | ID_SWO_DATA =>
        return U_SWO;
      when ID_SWJ_PINS | ID_SWJ_SEQUENCE | ID_SWD_SEQUENCE | ID_JTAG_SEQUENCE =>
        return U_SEQUENCE;
      when ID_TRANSFER | ID_TRANSFER_BLOCK | ID_WRITE_ABORT | ID_JTAG_IDCODE =>
        return U_TRANSFER;
      when others =>
        return U_NONE;
    end case;
  end function;

  function byte_of(v : std_logic_vector(63 downto 0); i : natural) return std_logic_vector is
    variable b : std_logic_vector(7 downto 0) := (others => '0');
  begin
    for k in 0 to 7 loop
      if k = i then
        b := v(8 * k + 7 downto 8 * k);
      end if;
    end loop;
    return b;
  end function;

  function bytes_for(bits : natural) return natural is
  begin
    return (bits + 7) / 8;
  end function;

  function byte_word(b : std_logic_vector(7 downto 0)) return std_logic_vector is
  begin
    return x"00000000000000" & b;
  end function;

  function us_cycles(clk_hz : positive) return positive is
  begin
    return (clk_hz + 500_000) / 1_000_000;
  end function;

  function transfer_response(ack : std_logic_vector(2 downto 0); parity_error, reading : std_logic)
    return std_logic_vector is
  begin
    if ack = "001" and reading = '1' and parity_error = '1' then
      return RESP_ERROR;
    end if;
    return "00000" & ack;
  end function;

end package body;
