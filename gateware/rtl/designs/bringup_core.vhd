-- ボード立ち上げ確認用デザインの本体（ボード非依存）。
--
-- * UART 受信 → FIFO → 送信のエコー。
--   受信は公称ボーレートに最も近い分周比、送信は公称より TX_MARGIN_PPM 以上速い分周比で動く。
--   したがってホストの送信速度が「公称 + TX_MARGIN_PPM」以下なら、隙間なく送り続けても欠けない。
--   それより速いホストが送り続けると FIFO が溢れうるが、その場合は rx_error をラッチして知らせる
--   （オーバーラン・フレーミングエラーを黙って捨てない）。rx_error はリセットで解除する。
-- * '?' を受けると "fprobe-bringup sync_hz=<SYS_CLK_HZ> rev=<REVISION>\r\n" を返す。
--   ホストはこの値でクロック周波数とボーレート精度を検証する。
-- * heartbeat は約 1 Hz（0.5 秒ごとに反転）、activity は受信ごとに反転する。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity bringup_core is
  generic (
    SYS_CLK_HZ : positive;
    BAUD            : positive := 115_200;
    REVISION        : natural  := 0;
    ECHO_FIFO_DEPTH : positive := 64;      -- 2 の冪
    TX_MARGIN_PPM   : natural  := 5_000    -- 送信を公称より速くする割合（ppm）
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    rx        : in  std_logic;
    tx        : out std_logic;
    heartbeat : out std_logic;
    activity  : out std_logic;
    rx_error  : out std_logic    -- 受信の取りこぼし（ラッチ）
  );
end entity;

architecture rtl of bringup_core is
  constant RX_DIVISOR : natural := (SYS_CLK_HZ + BAUD / 2) / BAUD;
  -- 送信を速める量（Hz）。整数演算で 0 に丸まらないよう最低 1 Hz とする
  constant TX_MARGIN_HZ : positive := maximum(1, BAUD * (TX_MARGIN_PPM / 100) / 10_000);
  constant TX_DIVISOR   : natural  := SYS_CLK_HZ / (BAUD + TX_MARGIN_HZ);

  constant BANNER : string :=
    "fprobe-bringup sync_hz=" & integer'image(SYS_CLK_HZ) &
    " rev=" & integer'image(REVISION) & CR & LF;

  type rom_t is array (0 to BANNER'length - 1) of std_logic_vector(7 downto 0);

  function to_rom(s : string) return rom_t is
    variable rom : rom_t;
  begin
    for i in rom'range loop
      rom(i) := std_logic_vector(to_unsigned(character'pos(s(s'low + i)), 8));
    end loop;
    return rom;
  end function;

  constant BANNER_ROM : rom_t := to_rom(BANNER);
  constant QUESTION   : std_logic_vector(7 downto 0) :=
    std_logic_vector(to_unsigned(character'pos('?'), 8));

  type state_t is (S_ECHO, S_BANNER);
  signal state        : state_t := S_ECHO;
  signal banner_index : natural range 0 to BANNER'length - 1 := 0;

  signal rx_valid, rx_ready     : std_logic;
  signal rx_data                : std_logic_vector(7 downto 0);
  signal fifo_valid, fifo_ready : std_logic;
  signal fifo_data              : std_logic_vector(7 downto 0);
  signal tx_valid, tx_ready     : std_logic;
  signal tx_data                : std_logic_vector(7 downto 0);

  signal frame_error    : std_logic;
  signal overrun        : std_logic;
  signal rx_error_reg   : std_logic := '0';

  signal half_second    : natural range 0 to SYS_CLK_HZ / 2 - 1 := 0;
  signal heartbeat_reg  : std_logic := '0';
  signal activity_reg   : std_logic := '0';
begin
  -- 送信を公称より速くできるだけの分周分解能があること（例: 100 MHz / 115200 なら 871 と 866）
  assert TX_DIVISOR < RX_DIVISOR and TX_DIVISOR * 100 >= RX_DIVISOR * 98
    report "bringup_core: BAUD too high for SYS_CLK_HZ (TX cannot run slightly faster than RX)"
    severity failure;

  rx_i : entity work.uart_rx
    port map (clk => clk, rst => rst, divisor => to_unsigned(RX_DIVISOR, 16), rx => rx,
              out_valid => rx_valid, out_ready => rx_ready, out_data => rx_data,
              frame_error => frame_error, overrun => overrun);

  fifo_i : entity work.sync_fifo
    generic map (WIDTH => 8, DEPTH => ECHO_FIFO_DEPTH)
    port map (clk => clk, rst => rst,
              in_valid => rx_valid, in_ready => rx_ready, in_data => rx_data,
              out_valid => fifo_valid, out_ready => fifo_ready, out_data => fifo_data,
              level => open);

  tx_i : entity work.uart_tx
    port map (clk => clk, rst => rst, divisor => to_unsigned(TX_DIVISOR, 16),
              in_valid => tx_valid, in_ready => tx_ready, in_data => tx_data,
              tx => tx, busy => open);

  -- 送信元の選択: 通常は FIFO の先頭をそのまま送る。'?' は送らずに消費してバナーへ
  process (all)
  begin
    tx_valid   <= '0';
    tx_data    <= fifo_data;
    fifo_ready <= '0';
    case state is
      when S_ECHO =>
        if fifo_data = QUESTION then
          fifo_ready <= '1';
        else
          tx_valid   <= fifo_valid;
          fifo_ready <= tx_ready;
        end if;
      when S_BANNER =>
        tx_valid <= '1';
        tx_data  <= BANNER_ROM(banner_index);
    end case;
  end process;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        state        <= S_ECHO;
        banner_index <= 0;
      else
        case state is
          when S_ECHO =>
            if fifo_valid = '1' and fifo_data = QUESTION then
              banner_index <= 0;
              state        <= S_BANNER;
            end if;
          when S_BANNER =>
            if tx_ready = '1' then
              if banner_index = BANNER'length - 1 then
                state <= S_ECHO;
              else
                banner_index <= banner_index + 1;
              end if;
            end if;
        end case;
      end if;
    end if;
  end process;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        half_second   <= 0;
        heartbeat_reg <= '0';
        activity_reg  <= '0';
        rx_error_reg  <= '0';
      else
        if frame_error = '1' or overrun = '1' then
          rx_error_reg <= '1';
        end if;
        if half_second = SYS_CLK_HZ / 2 - 1 then
          half_second   <= 0;
          heartbeat_reg <= not heartbeat_reg;
        else
          half_second <= half_second + 1;
        end if;
        if rx_valid = '1' and rx_ready = '1' then
          activity_reg <= not activity_reg;
        end if;
      end if;
    end if;
  end process;

  heartbeat <= heartbeat_reg;
  activity  <= activity_reg;
  rx_error  <= rx_error_reg;
end architecture;
