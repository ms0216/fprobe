-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- 生成元: gateware/fprobe_gw/csr/fprobe_map.py の LA_TOK_CONSTANTS（ロジアナの RLE のトークン形式の符号の定数）
library ieee;
use ieee.std_logic_1164.all;

package la_tok_pkg is
  constant LA_TOK_FORMAT_VERSION      : natural := 1;   -- 形式の版（TLV LA_RLE_TOKEN_INFO）
  constant LA_TOK_BEAT_BYTES          : natural := 16;   -- 1 拍のバイト数（MEM_BEAT_BYTES）
  constant LA_TOK_NOP                 : natural := 7;   -- NOP（頭 0 0000 111 で尾が無い）
  constant LA_TOK_TAIL_FLAG           : natural := 128;   -- 尾の最上位ビット
  constant LA_TOK_SPECIAL_FLAG        : natural := 64;   -- 尾 T1 のビット 6: 1 = 特殊（WAIT・VALUE・REPEAT）、0 = TOGGLE_LONG
  constant LA_TOK_DELTA_EXT           : natural := 7;   -- 間隔の欄 7 = 拡張
  constant LA_TOK_DELTA_DIRECT_MAX    : natural := 6;   -- 頭だけで書ける間隔の最大
  constant LA_TOK_KIND_WAIT           : natural := 0;   -- 特殊トークンの種類 WAIT（頭の cccc）
  constant LA_TOK_KIND_VALUE          : natural := 1;   -- 同 VALUE
  constant LA_TOK_KIND_REPEAT         : natural := 2;   -- 同 REPEAT
  constant LA_TOK_T1_BITS             : natural := 6;   -- 尾 T1 の P の幅
  constant LA_TOK_TAIL_BITS           : natural := 7;   -- 2 個目以降の尾の P の幅
  constant LA_TOK_MAX_TAILS           : natural := 6;   -- 尾の最大数
  constant LA_TOK_PAYLOAD_BITS        : natural := 41;   -- P の最大幅（6 + 7 × 5）
  constant LA_TOK_VALUE_MIN_CHANGES   : natural := 4;   -- 1 サンプルで変わる ch がこの数以上なら VALUE（TOGGLE は 3 個まで）
  constant LA_TOK_VALUE_MIN_TAILS     : natural := 3;   -- VALUE の尾の最小数
  constant LA_TOK_WAIT_BITS           : natural := 40;   -- 1 つの WAIT の時間の幅（最大 2^40 − 1）
  constant LA_TOK_CONTROL_RLE_FMT_BIT : natural := 17;   -- CSR la_deep.control.rle_fmt のビット
end package;
