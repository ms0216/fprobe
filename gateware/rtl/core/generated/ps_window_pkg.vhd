-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- 生成元: gateware/fprobe_gw/ps_window.py（PYNQ の PS と PL の窓のレジスタ表）
-- 仕様: docs/superpowers/specs/2026-11-03-ps-stream-position-design.md
library ieee;
use ieee.std_logic_1164.all;

package ps_window_pkg is
  constant WIN_LINK_ID      : std_logic_vector(31 downto 0) := x"46504C4B";
  constant WIN_LINK_VERSION : natural := 4;
  constant WIN_EPOCH_BITS   : natural := 16;
  constant WIN_STATE_ACTIVE   : natural := 16;
  constant WIN_STATE_WRITING  : natural := 17;
  constant WIN_STATE_OVERRUN  : natural := 18;
  constant WIN_STATE_CURRENT  : natural := 19;
  constant WIN_PORT_FIRST   : natural := 10;   -- la_ps_port が受け持つ語の範囲
  constant WIN_PORT_LAST    : natural := 63;
  -- レジスタ（語の番号）
  constant WIN_ID           : natural := 0;
  constant WIN_VERSION      : natural := 1;
  constant WIN_RX_STATUS    : natural := 2;
  constant WIN_RX_COMMIT    : natural := 3;
  constant WIN_TX_HEAD      : natural := 4;
  constant WIN_TX_DONE      : natural := 5;
  constant WIN_DROPS        : natural := 6;
  constant WIN_MEM_BASE     : natural := 7;
  constant WIN_MEM_BYTES    : natural := 8;
  constant WIN_MEM_ERRORS   : natural := 9;
  constant WIN_LA_WRITTEN   : natural := 11;
  constant WIN_EV_WPOS      : natural := 12;
  constant WIN_EV_START     : natural := 13;
  constant WIN_STREAM_EPOCH : natural := 15;
  constant WIN_LA_CONSUMED  : natural := 16;
  constant WIN_EV_CONSUMED  : natural := 17;
  constant WIN_STREAM_BEATS : natural := 18;
  constant WIN_RD_EPOCH     : natural := 19;
  constant WIN_RD_REGION    : natural := 20;
  constant WIN_RD_FIRST     : natural := 21;
  constant WIN_RD_COUNT     : natural := 22;
  constant WIN_RD_ACK       : natural := 23;
  constant WIN_LA_WPTR      : natural := 24;
  constant WIN_POS_TAG      : natural := 25;
  constant WIN_STALE_WRITES : natural := 26;
  constant WIN_RX_BUF       : natural := 256;
  constant WIN_TX_BUF       : natural := 512;
end package;
