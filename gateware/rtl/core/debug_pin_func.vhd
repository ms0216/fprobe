-- デバッグコネクタの端子の「機能」の出力（ボード非依存、組み合わせ回路）: fprobe_core のデバッグの線・UART の TX・RTS を、
-- 端子の番号（ボードのパッケージの DBG_*）に並べる。駆動の最終の選択（gpio・クロストリガの出力との優先）は pin_select か、
-- それを使わないボードではトップ（gpio > 機能 > 高インピーダンス）。
--   SWCLK・SWDIO・TDI・UART TX は値と oe をそのまま。nRESET はオープンドレイン（Low だけ駆動）。それ以外の端子は入力のまま。
--   UART の RTS/CTS（U52）: cts_claim・rts_claim の端子は候補 FLOW_PIN_CHOICES(cts_pin・rts_pin) の端子。CTS を取った端子は
--   機能の出力を止め（入力にする）、その端子の値を uart_cts_n に返す（取っていなければ '0' = 出ている）。RTS を取った端子は
--   uart_rts_n を駆動する。候補に無い番号は何もしない。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity debug_pin_func is
  generic (
    PINS        : positive := 8;
    DBG_SWCLK   : natural;
    DBG_TDI     : natural;
    DBG_NRESET  : natural;
    DBG_SWDIO   : natural;
    DBG_UART_TX : natural;
    -- UART の RTS/CTS に使える端子の候補（cts_pin・rts_pin の 2 ビットで選ぶ）。既定は claim しない構成（UART_FLOW なし）用
    FLOW_PIN_CHOICES : integer_vector := (0 => 0)
  );
  port (
    swclk_o    : in  std_logic;
    swclk_oe   : in  std_logic;
    swdio_o    : in  std_logic;
    swdio_oe   : in  std_logic;
    nreset_low : in  std_logic;                      -- '1' で nRESET を Low に駆動
    tdi_o      : in  std_logic;
    tdi_oe     : in  std_logic;
    uart_tx    : in  std_logic;
    uart_tx_oe : in  std_logic;
    rts_claim  : in  std_logic := '0';
    rts_pin    : in  std_logic_vector(1 downto 0) := "00";
    uart_rts_n : in  std_logic := '1';
    cts_claim  : in  std_logic := '0';
    cts_pin    : in  std_logic_vector(1 downto 0) := "00";
    pins_in    : in  std_logic_vector(PINS - 1 downto 0) := (others => '1');   -- 端子の値（CTS を読む）
    func_o     : out std_logic_vector(PINS - 1 downto 0);
    func_oe    : out std_logic_vector(PINS - 1 downto 0);
    uart_cts_n : out std_logic
  );
end entity;

architecture rtl of debug_pin_func is
  constant CHOICES : integer_vector(0 to FLOW_PIN_CHOICES'length - 1) := FLOW_PIN_CHOICES;
begin
  process (all)
  begin
    func_o  <= (others => '0');
    func_oe <= (others => '0');
    func_o(DBG_SWCLK)    <= swclk_o;
    func_oe(DBG_SWCLK)   <= swclk_oe;
    func_o(DBG_SWDIO)    <= swdio_o;
    func_oe(DBG_SWDIO)   <= swdio_oe;
    func_o(DBG_NRESET)   <= '0';
    func_oe(DBG_NRESET)  <= nreset_low;
    func_o(DBG_TDI)      <= tdi_o;
    func_oe(DBG_TDI)     <= tdi_oe;
    func_o(DBG_UART_TX)  <= uart_tx;
    func_oe(DBG_UART_TX) <= uart_tx_oe;    -- ホストが UART に最初の 1 バイトを送るまで駆動しない（B29）
    for j in CHOICES'range loop
      if cts_claim = '1' and to_integer(unsigned(cts_pin)) = j then
        func_oe(CHOICES(j)) <= '0';
      end if;
      if rts_claim = '1' and to_integer(unsigned(rts_pin)) = j then
        func_o(CHOICES(j))  <= uart_rts_n;
        func_oe(CHOICES(j)) <= '1';
      end if;
    end loop;
  end process;

  -- CTS の線（アクティブ Low）。取っていなければ出ているとみなす（'0'）
  process (all)
  begin
    uart_cts_n <= '0';
    for j in CHOICES'range loop
      if cts_claim = '1' and to_integer(unsigned(cts_pin)) = j then
        uart_cts_n <= pins_in(CHOICES(j));
      end if;
    end loop;
  end process;
end architecture;
