-- ULPI リンク層（USB3317 との物理プロトコル）。
--
-- 根拠: docs/reference/usb2-hs-device-on-ulpi.md §1。clk は ULPI CLKOUT そのもの（反転しない）。
--
-- I/O タイミング（ULPI 標準の「立ち上がりで出力・取り込み」に合わせる）:
--   * PHY の出力（DIR, NXT, DATA）は PHY が立ち上がり後 1.5〜3.5 ns で変える。これを立ち下がりで取り込み
--     （前後に約 8 ns の余裕）、内部の立ち上がりで使う。内部の立ち上がり k で見る値 = PHY のエッジ k での値。
--   * link の出力（STP, DATA）は立ち上がりでレジスタから出し、PHY は次の立ち上がりで取り込む
--     （クロック分配と出力バッファの遅延が 1 周期 − セットアップ 5 ns に収まる）。
--   * DATA の出力イネーブル = not dir（ピンから組み合わせ）。DIR が立った周期のうちにバスを離すため。
--   エッジ k で NXT = 1 なら、PHY はエッジ k のバス上のバイトを受理した。link は同じエッジで次のバイトを
--   出力レジスタに載せる（エッジ k + 1 で PHY に見える）。
--
-- 役割:
--   * バス所有中に用事が無ければ 0x00（Idle）を駆動する。
--   * 起動: リセット後 STP を STARTUP_STP_CYCLES 保持して解放し、DIR が 4 周期連続 low で phy_ready。
--   * 受信: RX CMD（DIR 2 周期以上 high かつ NXT=0）から line_state / vbus_valid / rx_active / rx_error、
--     DIR と NXT の同時立ち上がりで rx_active 開始、DIR=1 かつ NXT=1 の周期に rx_valid / rx_data。
--     レジスタ読み出し中は RX CMD を解読しない。
--   * 送信: tx_* ストリーム。先頭バイトは PID（下位 4 ビットを TX CMD に埋める）、tx_last で終端し
--     最後のバイトが受理された次の周期に STP。バイトは 2 段の送信キューで受け取る。tx_ready はキューの
--     空きだけで決まり（NXT に依存しない）、上流の論理は 1 周期をまるごと使える。1 パケットずつ扱い、
--     最後のバイトを受け取ったらそのパケットが終わるまで次は受け取らない。
--     TX CMD の受理前に DIR が立てば受理されていないので、DIR が下がってから同じ PID で再試行する。
--     途中で DIR が立つか、次のバイトが要る時にキューが空なら tx_aborted を 1 周期出す
--     （後者は 0xFF + STP で PHY 側も中断させる）。中断したパケットの残りは捨てる。
--   * チャープ: chirp_k = '1' の間 NOPID（0x40）+ 0x00 を送り続け、'0' になったら 0xFF + STP で終える。
--   * レジスタ: reg_req を上げ続けると 1 回実行し reg_done を 1 周期出す（読み出し値は reg_rdata）。
--     reg_req が一度 '0' に戻るまで次の要求は受け付けない（4 フェーズハンドシェイク）。
--     DIR に割り込まれた操作は自動で再試行する。
--   優先度: 送信の再試行 > チャープ > 送信 > レジスタ。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity ulpi_link is
  generic (
    STARTUP_STP_CYCLES : positive := 64
  );
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    -- PHY
    dir        : in  std_logic;
    nxt        : in  std_logic;
    data_i     : in  std_logic_vector(7 downto 0);
    data_o     : out std_logic_vector(7 downto 0);
    data_oe    : out std_logic;
    stp        : out std_logic;
    -- 状態
    phy_ready  : out std_logic;
    line_state : out std_logic_vector(1 downto 0);
    vbus_valid : out std_logic;
    rx_active  : out std_logic;
    rx_error   : out std_logic;
    -- 受信バイト
    rx_valid   : out std_logic;
    rx_data    : out std_logic_vector(7 downto 0);
    -- 送信パケット
    tx_valid   : in  std_logic;
    tx_ready   : out std_logic;
    tx_data    : in  std_logic_vector(7 downto 0);
    tx_last    : in  std_logic;
    tx_aborted : out std_logic;
    -- チャープ（OpMode = 10 で使う）
    chirp_k    : in  std_logic;
    -- レジスタアクセス
    reg_req    : in  std_logic;
    reg_we     : in  std_logic;
    reg_addr   : in  std_logic_vector(5 downto 0);
    reg_wdata  : in  std_logic_vector(7 downto 0);
    reg_done   : out std_logic;
    reg_rdata  : out std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of ulpi_link is
  type state_t is (
    S_STARTUP, S_WAIT_PHY, S_IDLE,
    S_TX_CMD, S_TX_RETRY, S_TX_DATA, S_STOP,
    S_CHIRP_CMD, S_CHIRP_DATA,
    S_REG_CMD, S_REG_WDATA, S_REG_STOP, S_REG_TURN, S_REG_RDATA);

  constant CMD_TRANSMIT : std_logic_vector(3 downto 0) := "0100";
  constant CMD_REGWRITE : std_logic_vector(1 downto 0) := "10";
  constant CMD_REGREAD  : std_logic_vector(1 downto 0) := "11";
  constant ABORT_DATA   : std_logic_vector(7 downto 0) := x"FF";

  -- PHY 出力の取り込み（立ち下がり）
  signal dir_s         : std_logic := '1';
  signal nxt_s         : std_logic := '0';
  signal data_s        : std_logic_vector(7 downto 0) := (others => '0');

  -- link 出力のレジスタ（立ち上がり）
  signal data_q        : std_logic_vector(7 downto 0) := (others => '0');
  signal stp_q         : std_logic := '1';

  signal state         : state_t := S_STARTUP;
  signal startup_count : natural range 0 to STARTUP_STP_CYCLES := 0;
  signal dir_low_count : natural range 0 to 3 := 0;
  signal ready         : std_logic := '0';
  signal reg_released  : std_logic := '1';    -- 前回完了後に reg_req が下がった
  signal pid_q         : std_logic_vector(3 downto 0) := (others => '0');
  signal last_q        : std_logic := '0';    -- 出力レジスタのバイトが送信の最後

  -- 送信キュー（先頭が txq(0)）。要素は last & data
  type txq_t is array (0 to 1) of std_logic_vector(8 downto 0);
  signal txq           : txq_t := (others => (others => '0'));
  signal txq_count     : natural range 0 to 2 := 0;
  signal tx_closed     : std_logic := '0';    -- パケットの最後を受け取った（または中断の通知中）
  signal push          : boolean;

  signal dir_d         : std_logic := '1';
  signal rx_active_r   : std_logic := '0';
  signal rx_error_r    : std_logic := '0';
  signal line_state_r  : std_logic_vector(1 downto 0) := "00";
  signal vbus_r        : std_logic := '0';
  signal rx_valid_r    : std_logic := '0';
  signal rx_data_r     : std_logic_vector(7 downto 0) := (others => '0');
  signal reg_done_r    : std_logic := '0';
  signal reg_rdata_r   : std_logic_vector(7 downto 0) := (others => '0');
  signal tx_aborted_r  : std_logic := '0';

  signal reg_window    : boolean;   -- レジスタ読み出しのデータがバス上に来うる区間
begin
  reg_window <= state = S_REG_TURN or state = S_REG_RDATA;

  process (clk)
  begin
    if falling_edge(clk) then
      dir_s  <= dir;
      nxt_s  <= nxt;
      data_s <= data_i;
    end if;
  end process;

  data_o     <= data_q;
  stp        <= stp_q;
  data_oe    <= not dir;
  phy_ready  <= ready;
  tx_aborted <= tx_aborted_r;
  reg_done   <= reg_done_r;
  reg_rdata  <= reg_rdata_r;

  tx_ready <= '1' when txq_count < 2 and tx_closed = '0' else '0';
  push     <= tx_valid = '1' and txq_count < 2 and tx_closed = '0';

  ------------------------------------------------------------------------------
  -- 制御状態機械（送信・チャープ・レジスタ・起動）。出力レジスタを状態と一緒に決める
  ------------------------------------------------------------------------------
  process (clk)
    variable pop   : boolean;            -- この周期に送信キューの先頭を出力レジスタへ移す
    variable abort : boolean;            -- 送信を中断する（キューの残りを捨てる）

    procedure finish(stop_data : std_logic_vector(7 downto 0)) is
    begin
      data_q <= stop_data;
      stp_q  <= '1';
      state  <= S_STOP;
    end procedure;

    procedure back_to_idle is
    begin
      data_q <= x"00";
      state  <= S_IDLE;
    end procedure;
  begin
    if rising_edge(clk) then
      pop          := false;
      abort        := false;
      reg_done_r   <= '0';
      tx_aborted_r <= '0';
      if reg_req = '0' then
        reg_released <= '1';
      end if;

      if rst = '1' then
        state         <= S_STARTUP;
        data_q        <= x"00";
        stp_q         <= '1';
        startup_count <= 0;
        dir_low_count <= 0;
        ready         <= '0';
        reg_released  <= '1';
        txq_count     <= 0;
        tx_closed     <= '0';
      else
        case state is
          when S_STARTUP =>
            if startup_count = STARTUP_STP_CYCLES then
              stp_q <= '0';
              state <= S_WAIT_PHY;
            else
              startup_count <= startup_count + 1;
            end if;

          when S_WAIT_PHY =>
            -- PHY は PLL ロックまで DIR を上げたまま。下がって落ち着いたら使える
            if dir_s = '1' then
              dir_low_count <= 0;
            elsif dir_low_count = 3 then
              ready <= '1';
              state <= S_IDLE;
            else
              dir_low_count <= dir_low_count + 1;
            end if;

          when S_IDLE =>
            if dir_s = '0' then
              if chirp_k = '1' then
                data_q <= CMD_TRANSMIT & "0000";
                state  <= S_CHIRP_CMD;
              elsif txq_count /= 0 then
                pop    := true;
                pid_q  <= txq(0)(3 downto 0);
                last_q <= txq(0)(8);
                data_q <= CMD_TRANSMIT & txq(0)(3 downto 0);
                state  <= S_TX_CMD;
              elsif reg_req = '1' and reg_released = '1' then
                if reg_we = '1' then
                  data_q <= CMD_REGWRITE & reg_addr;
                else
                  data_q <= CMD_REGREAD & reg_addr;
                end if;
                state <= S_REG_CMD;
              end if;
            end if;

          when S_TX_CMD | S_TX_DATA =>
            if dir_s = '1' then
              data_q <= x"00";
              if state = S_TX_CMD then
                state <= S_TX_RETRY;           -- TX CMD は受理されていない
              else
                tx_aborted_r <= '1';           -- PHY が送信を打ち切った
                abort        := true;
                state        <= S_IDLE;
              end if;
            elsif nxt_s = '1' then
              if last_q = '1' then
                finish(x"00");
              elsif txq_count /= 0 then
                pop    := true;
                data_q <= txq(0)(7 downto 0);
                last_q <= txq(0)(8);
                state  <= S_TX_DATA;
              else
                tx_aborted_r <= '1';           -- 送信データの途切れ。PHY 側も中断させる
                abort        := true;
                finish(ABORT_DATA);
              end if;
            end if;

          when S_TX_RETRY =>
            if dir_s = '0' then
              data_q <= CMD_TRANSMIT & pid_q;
              state  <= S_TX_CMD;
            end if;

          when S_STOP =>
            stp_q <= '0';
            back_to_idle;

          when S_CHIRP_CMD =>
            if dir_s = '1' then
              back_to_idle;
            elsif nxt_s = '1' then
              data_q <= x"00";
              state  <= S_CHIRP_DATA;
            end if;

          when S_CHIRP_DATA =>
            if dir_s = '1' then
              back_to_idle;
            elsif chirp_k = '0' then
              finish(ABORT_DATA);
            end if;

          when S_REG_CMD =>
            if dir_s = '1' then
              back_to_idle;                    -- 割り込まれた。後で再試行
            elsif nxt_s = '1' then
              if reg_we = '1' then
                data_q <= reg_wdata;
                state  <= S_REG_WDATA;
              else
                data_q <= x"00";
                state  <= S_REG_TURN;
              end if;
            end if;

          when S_REG_WDATA =>
            if dir_s = '1' then
              back_to_idle;
            elsif nxt_s = '1' then
              data_q <= x"00";
              stp_q  <= '1';
              state  <= S_REG_STOP;
            end if;

          when S_REG_STOP =>
            stp_q        <= '0';
            reg_done_r   <= '1';
            reg_released <= '0';
            state        <= S_IDLE;

          when S_REG_TURN =>
            -- PHY が DIR を上げる周期がターンアラウンド。NXT も立っていれば受信に割り込まれた
            if dir_s = '1' then
              state <= S_IDLE when nxt_s = '1' else S_REG_RDATA;
            end if;

          when S_REG_RDATA =>
            if dir_s = '1' and nxt_s = '0' then
              reg_rdata_r  <= data_s;
              reg_done_r   <= '1';
              reg_released <= '0';
            end if;
            state <= S_IDLE;                   -- 取れなかった場合は再試行される
        end case;

        -- 送信キュー（push は空きがある時だけなので、push と pop が重なるのは 1 段の時）
        if push and pop then
          txq(0) <= tx_last & tx_data;
        elsif push then
          txq(txq_count) <= tx_last & tx_data;
          txq_count      <= txq_count + 1;
        elsif pop then
          txq(0)    <= txq(1);
          txq_count <= txq_count - 1;
        end if;
        if push and tx_last = '1' then
          tx_closed <= '1';
        end if;
        if state = S_STOP or tx_aborted_r = '1' then
          tx_closed <= '0';                    -- パケットが終わった / 中断を上流が受け取った
        end if;
        if abort then
          txq_count <= 0;                      -- 残りを捨て、上流が中断を受け取るまで次を受け取らない
          tx_closed <= '1';
        end if;
      end if;
    end if;
  end process;

  ------------------------------------------------------------------------------
  -- 受信側の解読（制御状態機械とは独立に常時動く）
  ------------------------------------------------------------------------------
  process (clk)
  begin
    if rising_edge(clk) then
      dir_d      <= dir_s;
      rx_valid_r <= '0';
      if rst = '1' then
        rx_active_r  <= '0';
        rx_error_r   <= '0';
        line_state_r <= "00";
        vbus_r       <= '0';
      elsif dir_s = '0' then
        rx_active_r <= '0';
        rx_error_r  <= '0';
      elsif dir_d = '0' then
        -- ターンアラウンド。NXT も立っていれば受信開始（RX CMD は来ない）
        if nxt_s = '1' then
          rx_active_r <= '1';
        end if;
      elsif nxt_s = '1' then
        if rx_active_r = '1' then
          rx_valid_r <= '1';
          rx_data_r  <= data_s;
        end if;
      elsif not reg_window then
        -- RX CMD
        line_state_r <= data_s(1 downto 0);
        vbus_r       <= data_s(3);              -- SessVld 以上
        rx_active_r  <= data_s(4);
        rx_error_r   <= data_s(5) and data_s(4);
      end if;
    end if;
  end process;

  line_state <= line_state_r;
  vbus_valid <= vbus_r;
  rx_active  <= rx_active_r;
  rx_error   <= rx_error_r;
  rx_valid   <= rx_valid_r;
  rx_data    <= rx_data_r;
end architecture;
