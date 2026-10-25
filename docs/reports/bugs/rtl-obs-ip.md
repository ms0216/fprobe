# 対象側 IP（target_ip/fprobe_obs）の不具合台帳（検出専用。修正はしていない）

領域: obs_soft_tap / obs_snap_core / obs_tck_sampler / obs_soft_core、examples/（primer20k_counter・xilinx_bscan・shrike_lite）、
README とホストの復号器 host/fprobe/obs_decoder.py の一致。obs_core / obs_async_fifo は別の担当が済ませた。
再現試験は gateware/tests/test_obs_ip_bugs.py（strict の xfail。直ると XPASS で失敗して気づく）。
分類: A = 静かに誤った結果、B = 失敗が見える、C = 端の条件・まれ、D = 品質。

件数: A 0 / B 0 / C 1 / D 2 / 疑い 5。**A・B は見つからなかった**（下の「確かめて問題が無かったこと」を参照）。

## 再現済み

### BUG-RTL-OBSIP-01 (D) obs_soft_tap は IDCODE の bit0 = 1 を検査しない
- 再現: test_obs_ip_bugs.py::test_idcode_bit0_zero_is_rejected
- 原因: target_ip/fprobe_obs/rtl/obs_soft_tap.vhd generic IDCODE（コメントに「bit0 は 1」とあるが assert が無い）。
  obs_snap_core・obs_soft_core も IDCODE をそのまま渡す。
- 直し方の案: アーキテクチャ冒頭に `assert IDCODE(0) = '1' report ... severity failure;`（obs_core の FIFO_DEPTH と同じ流儀）。
- 影響: bit0 = 0 にすると IDCODE が BYPASS（1 ビット）と見分けられず、鎖の走査（IDCODE の読み出し・装置の数え）が静かにずれる。
  利用者が自分の IDCODE を入れるときの落とし穴。
- 確信度: 再現済み
- **修正済み: obs_soft_tap の本体先頭に `assert IDCODE(0) = '1' ... severity failure`。試験の xfail は外した**

### BUG-RTL-OBSIP-02 (C) obs_soft_core は ONE_CLOCK = false でも obs_tck_sampler.vhd が無いと解析できない
- 再現: test_obs_ip_bugs.py::test_soft_core_two_clock_needs_no_sampler_file（GHDL: `unit "obs_tck_sampler" not found`）
- 原因: obs_soft_core.vhd:38 の `entity work.obs_tck_sampler` は generate の中だが、直接のエンティティ実体化は解析時に
  ユニットが要る。README（入れ方 2）は「`obs_tck_sampler.vhd` も足す」を ONE_CLOCK = true の説明に置くので、
  2 クロック版の利用者は足さない。さらに README は `obs_soft_tap.vhd` を足すとは明記していない（obs_soft_core の説明に括弧で
  出るだけ）。Vivado・Gowin でも同様に失敗するかは未確認（合成は走らせていない）。
- 直し方の案: README に「obs_soft_core は ONE_CLOCK に関わらず obs_async_fifo・obs_core・obs_tck_sampler・obs_soft_tap を
  必要とする」と書く。または sampler を component 宣言＋空の既定にする等で依存を外す。
- 影響: 明日の組み込みで 2 クロック版を選ぶと、最初のビルドで「見つからない」と止まる（失敗は見える）。
- 確信度: 再現済み（GHDL）
- **文書で対応（README に依存ファイルを明記済み）。回路は変えない。試験は xfail のまま**

### BUG-RTL-OBSIP-03 (D) shrike_demo_core は OBS が 0・1・2 以外だと tdo・tdo_oe を駆動しない
- 再現: test_obs_ip_bugs.py::test_shrike_demo_core_unknown_obs_drives_tdo（tdo = 'U'）
- 原因: examples/shrike_lite/rtl/shrike_demo_core.vhd の 3 つの generate（OBS = 1 / 2 / 0）。それ以外の値の else が無い。
  CLAUDE.md の「すべての信号を明示的に駆動する」（合成は EX1998 で失敗）に反するが、既定 OBS = 1 では出ない。
- 直し方の案: OBS の範囲を `natural range 0 to 2` にするか、`OBS > 2` で assert。
- 影響: 例の設定の打ち間違いが、合成の警告か実機の TDO 不定になる。
- 確信度: 再現済み
- **修正済み: shrike_demo_core の no_obs_g の条件を `OBS /= 1 and OBS /= 2` にして、範囲外の値でも tdo・tdo_oe を 0 に駆動する。試験の xfail は外した**

## 疑い（再現できない・実機や合成が要る）

### SUSPECT-RTL-OBSIP-04 (C) obs_soft_tap の tdo_q・tdo_oe_q はリセットを持たず、宣言の初期値 '0' に頼る
- 場所: obs_soft_tap.vhd tdo_q・tdo_oe_q（同 state・ir・id_shift・bypass・ir_shift）。
- 疑い: CLAUDE.md の方針どおり宣言の初期値を合成が無視する場合、電源投入後の最初の TCK の立ち下がり（1 クロック版は
  fall の印）まで tdo_oe_q が不定で、TDO を駆動して鎖の他の装置の TDO と衝突しうる。state・ir は TMS = 1 の 5 回で自己回復する
  （コメントのとおり。16 状態の符号がすべて有効）が、tdo_oe_q は回復の前に出る。id_shift・bypass・ir_shift は使う前に
  Capture で読み込まれるので問題なし。
- 直し方の案: tdo_oe_q を state = TLR のとき 0 に落とす（ただし TCK が来てからなので電源投入直後は防げない）。
  根本は電源投入の短時間なので、使う FPGA が初期値を守るか（Xilinx・Gowin は守る）を README に書くだけでも足りる。
- 確信度: 低（FPGA ごとに初期値の扱いが違う。シミュレーションでは出ない）

### SUSPECT-RTL-OBSIP-05 (C) obs_tck_sampler の初期値（TCK = 0・TMS = 1）への依存
- 場所: obs_tck_sampler.vhd s_tck・s_tms・s_tdi の初期値。コメントが自分で認めている（初期値が効かないと偽の辺が 1 回出る）。
- 疑い: TCK が High のまま電源が入ると偽の立ち上がり 1 回（TMS が 0 の値だと Run-Test/Idle へ進む）。TAP は 5 回の TMS = 1
  で戻るので実害は小さい。ただし obs_snap_core の changes・prev は rst まで、obs_core の CONTROL・SELECT は TLR までの間に
  偽の辺が来ると 1 回だけ余分な状態遷移を見る。
- 確信度: 低

### SUSPECT-RTL-OBSIP-06 (D) 1149.1 との差: IR の更新が Update-IR を出る立ち上がり、TLR での IR リセットが TLR を出る立ち上がり
- 場所: obs_soft_tap.vhd の命令レジスタの case（`when S_UP_IR => ir <= ir_shift`、`when S_TLR => ir <= IR_IDCODE` は
  その状態のときの立ち上がりで効く）。標準は Update-IR の立ち下がり・TLR に入ったとき。
- 確かめたこと: 次の DR の Capture までに少なくとも 2 回の立ち上がりがあるので、JTAG のピンからは差が見えない
  （ランダム TMS 20000 回の基準モデルとの突き合わせで状態・TDO・tdo_oe が一致）。user_sel が TLR の間だけ古いまま残る
  が、obs_core は tlr を優先して見る。
- 確信度: 高い（差はあるが実害なし）

### SUSPECT-RTL-OBSIP-07 (D) 既定の IDCODE 0x0F0B5001 の製造者 ID（bits 11:1）が 0
- 場所: obs_soft_tap.vhd generic IDCODE の既定、obs_snap_core.vhd、obs_soft_core.vhd。
- 疑い: 1149.1 は bit0 = 1 のほかに製造者 ID（JEDEC）を持つ。0 は OpenOCD 等が「不明な装置」として警告する可能性。
  動作には影響しない（fprobe は IDCODE をそのまま使う）。
- 確信度: 低

### SUSPECT-RTL-OBSIP-08 (D) examples/primer20k_counter の電源投入リセット（por）が宣言の初期値に頼る
- 場所: primer20k_obs_counter_top.vhd `signal por ... := (others => '0')`。
- 疑い: 初期値が無視される FPGA では por が起動時に 15 だと rst が一度も立たず、obs_core の clk 側が reset の無いまま動く
  （obs_core は rst の明けに CONTROL を写すので、TLR 前の CONTROL = 0 の前提が崩れる）。例のコメントが認めている。
  Gowin は初期値を守るのでこのボードでは出ない。
- 確信度: 低

## 確かめて問題が無かったこと

- **TAP の状態機械**: obs_soft_tap を 1149.1 の 16 状態の基準モデルとランダムな TMS・TDI 列（4000 回 × 1 クロック版・2 クロック版。
  探索では 20000 回、TCK の半周期 61〜100 ns・clk 20 ns の位相ずらし）で突き合わせ。状態の符号・TDO（IR / IDCODE / BYPASS）・
  tdo_oe（Shift-IR・Shift-DR の間だけ）が一致。Capture-IR は 0001、TLR で IDCODE。再発防止に
  test_obs_tap_model.py として残した（緑）。
- **1 クロック版の TCK 上限**: README の「clk の 1/8 以下」より速い clk/6 まで、TDO の読みが一致（余裕あり）。
- **一時停止・中断**: Shift-DR に Pause-DR をはさんでも（obs_snap_core・obs_soft_core）データがずれない。Capture-DR 直後に
  抜けた走査で記録が失われない。走査の途中の TLR で IDCODE に戻る。
- **obs_snap_core**: 乱数の probes（走査中も変化）と Capture-DR の周期を観測する基準モデルで、値・変化の回数（255 の飽和を含む）が
  一致（test_obs_snap_core_matches_model）。
- **generic の組み合わせ**: GROUPS = 1・3・256、FIFO_DEPTH = 4・8、LOST_BITS = 1・8、CLOCK_HZ = 2^31 - 1、ONE_CLOCK の
  true / false で、シミュレーションが通る。FIFO_DEPTH = 1・2・6 は obs_core の assert で組み立てが止まる（意図どおり）。
  GHDL の `--synth` で obs_soft_core（4 通り）・obs_snap_core に、駆動されない信号・ラッチの警告は出ない。
- **ホストの復号器との一致**: obs_decoder.py の記録の種類（値 0・刻み/印 1・溢れ 2・制御 3）、印の番号 14:7・late 6:0、
  組の番号 7:0、標本の 0xA7・回数 23:16・値 15:0 は README と RTL に一致。0xA7 は記録の語（31:30 の種類）と衝突しない。
- **examples のポート・generic**: primer20k_counter（obs_soft_tap + obs_core の 2 クロック版）・xilinx_bscan（BSCANE2 → obs_core、
  ONE_CLOCK => false）・shrike_lite（obs_snap_core / obs_soft_core）のポート名・generic は RTL と README に一致。

## 調べていない範囲

- 合成・実機・HIL（指示により走らせていない）。Vivado / Gowin / ForgeFPGA での初期値・EX1998 の挙動。
- obs_core / obs_async_fifo の内部（別の担当）。obs_soft_core 経由の記録の中身の乱数試験。
- shrike_lite の demo_accel / demo_spi_slave、shrike_lite_check、ホストの fpga_obs.py のエンジン側（obs_jtag_engine）。
- 実際のタイミング制約（非同期クロックの制約）が README のとおりで足りるか。
