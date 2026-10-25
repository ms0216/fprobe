# RTL 不具合台帳: 「条件で MCU を止める」経路（swd_injector・dap_monitor の控え・dap_sampler との競合）

検出時は検出専用（RTL は変更していなかった）。修正は下の「修正状況」。再現試験は `gateware/tests/tb/rtl_bugs_xtrig_tb.py`（あるべき動作を書いた cocotb）と
`gateware/tests/test_rtl_bugs_xtrig.py`（`xfail(strict=True)` の包み。直って通ると XPASS で失敗 = xfail を外す合図）。
重さ: A = 静かに誤った結果 / B = 失敗が見える / C = 端の条件・まれ / D = 品質。

## 修正状況

- 修正済み: BUG-RTL-INJ-1 / INJ-2 / INJ-3 / SMP-1 / MON-1（再現試験の xfail は外し、普通の試験にした。以下の各項は検出時の記述）。
- 修正内容:
  - INJ-3 / SMP-1: dap_monitor が控えの AP（`shadow_ap` = CSW・TAR を書いた APSEL）を出し、swd_injector・dap_sampler は
    `shadow_valid` かつ `shadow_ap = ap`（注入先／サンプラの AP）のときだけ控えを使う。違えば CSW・TAR を読んで保存する（控えが無いときと同じ手順）。
  - INJ-1: 注入の失敗の後始末を、FAULT なら ABORT（STKERRCLR）→ CSW → TAR → SELECT の順にした（戻す値が無い＝SELECT 書き込み前の失敗は SELECT のみ）。
    WAIT の使い切りは CSW → TAR → SELECT、プロトコル異常・パリティ異常は SELECT のみ。戻す値は控え、または読んで保存した値。
  - INJ-2: 後始末の転送も通常の応答の待ち（S_RESPONSE）で扱い、WAIT は wait_retry 回まで再試行する。後始末の中の失敗はそれ以上戻しを重ねず、
    SELECT の転送へ進み、SELECT でも失敗なら終える（無限待ちにしない）。result は失敗（1）のまま。S_RESTORE_SELECT / S_RESTORE_RESPONSE は廃止。
  - MON-1: dap_monitor に `enable`（swd_enable）を足し、立ち下がり（切断）で SELECT の控えを 0、CSW・TAR の控えを無効にする。
    線のリセット（SWJ_Sequence）・nRESET では捨てない（台帳の案の一部。切断だけ）。
- 挙動の差: 注入が FAULT で失敗したとき STICKYERR を消す（以前はホストが次の転送で見ていた。dap_sampler と同じ）。ほかの外から見える形式は変えていない。
- 試験の修正: INJ-3 / MON-1 の試験は RDBUFF（DP の読み）を AP の読みに数えていたため `>= 3` が成り立たなかった。AP の CSW・TAR の読み（番地 0・4）がちょうど出ることを確かめる形にした。

## 確認した不具合（再現あり）

### BUG-RTL-INJ-1（重さ A）注入の DRW が FAULT だと CSW・TAR を戻さずに終わる
- 再現: `test_bug_inj_1`（`bug_inj_1_fault_leaves_csw_and_tar_modified`）。ホストが CSW = 自動増加・TAR = 0x20000100 のとき
  注入先が FAULT（STICKYERR）→ 結果 1 で終わり、ターゲットの CSW は増加なし（0x23000002）のまま、TAR は DHCSR のまま。
- 原因の推定: `swd_injector.vhd:218-224`（FAULT・WAIT 使い切り・パリティ異常は `S_RESTORE_SELECT` へ行き SELECT だけ戻す。
  既知の `ponytail:` コメント 12 行目）。STICKYERR が立つと以後の AP アクセスは FAULT になるため、戻すには ABORT（STKERRCLR）が要る
  （dap_sampler の `P_CLEAR` と同じ手順）。
- 直し方の案（適用しない）: FAULT のとき ABORT ← STKERRCLR → CSW・TAR（控え、または読んで保存した値）→ SELECT の順に戻す。
  WAIT の使い切りは STICKYERR が立たないので、CSW・TAR を（再試行つきで）戻してから SELECT。
- 影響: 注入が失敗した後、ホスト（pyOCD 等）がキャッシュした CSW・TAR と実際が食い違い、次のブロック転送が同じ番地に書き続ける・
  別の番地を読む等の静かな誤り。FAULT は出る（result = 1）が、ホストにはその注入が見えないので原因が分からない。
- 確信度: 高（再現済み）。

### BUG-RTL-INJ-2（重さ B〜C）失敗の後始末の SELECT の復帰が WAIT でも再試行しない
- 再現: `test_bug_inj_2`。WAIT の再試行 2 回のとき、注入の DRW が 3 回 WAIT → 失敗 → 後始末の SELECT 書き込みも WAIT。
  SELECT がホストの値（0xF0）に戻らず 0 のまま残る。
- 原因の推定: `swd_injector.vhd:227-235`。`S_RESTORE_RESPONSE` は応答の ack を見ずに `mine <= false; state <= S_IDLE`。
  通常経路（step 9）は `S_RESPONSE` で WAIT を再試行するが、後始末にはその仕組みが無い。ACK が OK かの確認もしない。
- 直し方の案: 後始末の応答も ack を見て、WAIT なら `wait_retry` まで再発行する（`tries` を使う）。それでも失敗なら result に別の
  印（SELECT を戻せなかった）を残す。
- 影響: 直後のホストの DP/AP アクセスが別の bank・AP に行く（静かな誤り）。DP が忙しい（WAIT を返す）状況でしか起きない。
- 確信度: 高（モデルに DP の WAIT を入れて再現。実機の DP が SELECT 書き込みに WAIT を返すかは対象による）。

### BUG-RTL-INJ-3（重さ A）ホストの控えの AP と注入先の AP が違っても控えを使う
- 再現: `test_bug_inj_3`。ホストが SELECT = APSEL 1 で CSW・TAR を書いた後、`ap = 0` へ注入すると、CSW・TAR の読みが 0 回
  （高速経路）で、AP1 の控えの値を AP0 の CSW・TAR に書いて「戻す」。
- 原因の推定: `dap_monitor.vhd:174` の `shadow_valid` は `csw_ap = tar_ap` だけで、**注入先（CSR の ap）と同じか**を見ない。
  `swd_injector.vhd:180` は `shadow_valid` だけで `fast` にする。
- 直し方の案: monitor が `csw_ap`（控えの AP）を出し、注入器は `csw_ap = ap` のときだけ高速経路にする（違えば読んで保存）。
  さらに高速経路で SELECT を戻す前に、書いた AP が ap と控えの AP のどちらか明確にする。
- 影響: マルチ AP のターゲット（STM32H7 の AP0/AP1、Zynq の複数 AP 等）で、AP0 に注入するとホストが AP1 で使っていた CSW・TAR で
  AP0 の CSW・TAR を上書きする。pyOCD は AP ごとにキャッシュを持つので、AP0 のキャッシュと実際が静かに食い違う。
- 確信度: 高（読みと再現。モデルは AP を 1 つしか持たないので「書き込みの行き先」ではなく「読んで保存しなかった」ことで判定）。

### BUG-RTL-SMP-1（重さ A）サンプラにも同じ（控えの AP とサンプラの AP が違う）
- 再現: `test_bug_smp_1`。ホストが AP1 の CSW・TAR を書いた後、サンプラ（`ap = 0`）が借りると CSW・TAR を読まずに（高速経路）
  AP0 の CSW・TAR を上書きし、戻しでも AP1 の控えの値を AP0 に書く。
- 原因の推定: `dap_sampler.vhd:359-360`（`saved`/`fast <= shadow_valid`）。BUG-RTL-INJ-3 と同根。
- 直し方の案: INJ-3 と同じ（控えの AP を受けて比べる）。
- 影響: INJ-3 と同じ。サンプラは常時動くので、マルチ AP でサンプラを使うと AP0 の CSW・TAR が常に壊れうる。
- 確信度: 高。

### BUG-RTL-MON-1（重さ C）接続し直した後も dap_monitor の控えが残る
- 再現: `test_bug_mon_1`。CSW・TAR を書く → DAP_Disconnect → DAP_Connect → 注入すると、CSW・TAR を読まず古い控えを書き戻す。
- 原因の推定: `dap_monitor.vhd` は `rst` でしか `csw_known`/`tar_known`/`sel` を消さない。接続・切断・SWD の線のリセット
  （DAP_SWJ_Sequence の線リセット）・nRESET を見ない。
- 直し方の案: 切断（swd_enable の立ち下がり）・線のリセット・nRESET の Low で控えを無効にする入力を足す。
- 影響: 接続し直した後、ホストが CSW・TAR を書き直す前に注入・サンプラが走ると、ターゲットのリセット値を古い値に書き換える。
  pyOCD などは接続時にキャッシュを捨てるので実害は小さい（C）。
- 確信度: 中〜高（再現済み。実害が出るホストは限られる）。

## 疑い（再現なし・または仕様判断が要る）

- SUS-1（D）注入の要求は実行中なら黙って捨てる（`swd_injector.vhd:163`、仕様）。捨てた回数を数えない。
  別の源が時間をおいて 2 回起きたとき 2 回目が注入されたかが外から分からない（count は成功だけ）。数える CSR があるとよい。
- SUS-2（D）注入は DAP コマンドの境界まで待つ（`dap_busy = 0`）。DAP_Transfer_Block（数千語）の間は、その全部が終わるまで
  停止しない。条件から停止までの遅れに上限が無い（測った時刻は `inject_time` で出るので事実は分かる。仕様の記述として明記を）。
- SUS-3（C）注入器が線を借りている間に、PHY を使わない DAP コマンド（DAP_SWJ_Clock・DAP_SWD_Configure 等）が `swd_half_period`
  などを変えると、注入の転送の途中で PHY の設定が変わりうる。`dap_cmd_ready = 0` で止まるのは PHY を使うコマンドだけ。
  dap_sampler のコメント（PHY を使わない DAP の命令が設定を変える間に戻しを重ねない）と同種の問題が注入器にもある可能性。未再現。
- SUS-4（D）`obs_match`: `restart` と `obs_valid` が同じ周期のとき、その値は旧セッションの `m_seen`/`m_last` と比べて発火しうる
  （`obs_match.vhd:55-60`。`restart` の `m_seen <= 0` は後から書くので効くのは次の値から）。同周期をどちらの値とみなすかは仕様の判断。
- SUS-5（D）`sampler_match`: enable の立ち上がりの周期の標本は（リセット側の枝に入るので）数えず捨てる（`seen` に入らない）。
  「enable の後の最初の標本では出さない」の規則とは矛盾しないが、その標本の値は次の比べの前の値にならない。

## 調べた範囲 / 調べていない範囲

- 読んだ: `swd_injector.vhd` 全体、`dap_monitor.vhd` の控え、`dap_sampler.vhd` の借り・戻し、`sampler_match.vhd`、`obs_match.vhd`、
  `xtrig_out.vhd`、`fprobe_core.vhd` の注入・サンプラ・源の配線（`sources` の束ね・`inj_request`）。
- シミュレーション: 注入 × サンプラ（常時保持）× ホストの転送 × WAIT をランダムな時刻に重ねる試験を 2 本（seed 25・27）。
  どちらも通った（競合による SELECT・CSW・TAR の破損は見つからず）。
- 構成の組み合わせ: `fprobe_core` の generic を、xtrig なし・trig_seq なし・両方なし・サンプラあり注入なし・サンプラなし注入あり・
  JTAG なしサンプラなし・サンプラ一致のみ（サンプラなし / xtrig なし）の 9 通りで GHDL の解析・初期化・基本の 3 試験まで通した。
  コンパイル不能・信号の駆動漏れ（初期化で X）は見つからなかった（EX1998 は合成でしか出ないので未確認）。
- 調べていない: 合成（禁止）、実機、`trig_seq`（試験済みと指示）、`xtrig_out` の乱数試験以外の端の条件、`csr_xtrig` の CSR 層、
  JTAG 経路の注入（`swd_enable` = 0 は結果 2 を返すだけ）、`OBS_PORT = true` の構成での全経路の結合。
