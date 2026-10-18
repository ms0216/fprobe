-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen / fprobe_gw build が書く）。
-- 生成元: gateware/fprobe_gw/build_info.py
-- 版なし（リポジトリの既定。ビルドでは GowinBuilder が差し替える）
library ieee;
use ieee.std_logic_1164.all;
use work.csr_bus_pkg.all;

package build_info_pkg is
  constant BUILD_INFO : csr_word_array_t(0 to 4) := (
    x"00000000",
    x"00000000",
    x"00000000",
    x"00000000",
    x"00000000"
  );
end package;
