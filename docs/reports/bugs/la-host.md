# ロジアナのホスト側の不具合台帳（検出専用。修正はしていない）

領域: host/fprobe/la.py（設定・LogicAnalyzer・DeepAnalyzer・LaStream）、la_wave.py（Capture・Transitions・Waveform・
decode_beats・decode_transitions・trigger_sample）、la_vcd.py、probe_la.py（la_* の道具）、collector.add_capture（時刻の置き方）、
Waveform.view（画面幅への縮め。timeline・recording が使う）。再現試験は host/tests/test_la_bugs.py（strict の xfail。
直ると XPASS で失敗して気づく）。分類: A = 静かに誤った結果、B = 失敗が見える、C = 端の条件・まれ、D = 品質。
件数: A 0 / B 1 / C 2 / D 1（確定 4 件）+ 疑い 5 件。

## 調べた手法と、不具合が見つからなかった範囲

試験の中に素朴な別実装（RLE の復号、変化点の表、画素ごとの縮め、VCD の読み戻し、深いバッファのリングの模型）を書き、
乱数の入力と突き合わせた（各 12 シード、numpy の有る経路と無い経路の両方）。すべて一致（不具合なし）:
- (a) RAW・RLE の復号（RLE の繰り返し長 1・2・3・255・256・65535・65536 と乱数、レコード 4〜240 個、空入力、拍の端数は ValueError）、
  decode_transitions（隣り合う同じ値のレコードの併合を含む）、Capture → Transitions → Waveform（level_at・edges・edges_between・
  count_between）の往復（チャンネル数 1〜16、全チャンネルの全サンプル）、VCD の読み戻し（Capture でも Transitions でも、
  分周 1〜7 = 10〜70 ns、全チャンネルの全サンプルの値、トリガのマーカーがトリガのサンプルでだけ 1）。
- (b) trigger_sample（RAW・RLE の全拍・全スロット）、LogicAnalyzer の深いバッファの窓（pretrigger = 0・1・7・8・9・100・
  4096・深さ − 1・乱数）で Capture.trigger = pretrigger かつ窓がトリガの前後の正しいサンプル、DeepAnalyzer.read / read_transitions
  の before_beats・after_beats（0・1・5・100・全部・None）での範囲とトリガ位置（RAW・RLE。リングの折り返しをまたぐ読みと、書いた拍数がリングを超える場合は試験していない = 疑い LA-S1）。
- (c) add_capture の置き方（トリガのサンプルがトリガ時刻に来る。分周 1〜7）。
- (d) LaStream の受け取り（任意のバイト位置での分割）→ StreamChunk の first・length・samples・transitions の連結が全体の
  復号と一致、VcdWriter のチャンクごとの書き出しが write_vcd と一致、見出し付きのストリームの任意位置の分割。
- (e) Waveform.view（画面幅 1〜10000、区間 1〜5000 サンプル）: 各画素の文字が「区間の先頭の値 / 区間内に変化があれば x」と一致し、
  どの変化点も x の画素に入るか画素の先頭に来る（消えない）。1 サンプルの脈は全幅で '0' だけにならない。

## 再現済み

### BUG-LA-01 (B) la_deep_arm が失敗するとイベント記録の読みが止まったまま
- 再現: test_failed_deep_arm_does_not_leave_event_recording_paused
- 原因: probe_la.py:197-199 で `pause_events(True)` してから `_deep.start()`（la.py:334 の「外部メモリの準備ができていない」等で
  RuntimeError）を呼ぶ。例外時に解除しない（la_deep_collect は finally で解除する）。self._deep も作りかけのまま残る。
- 直し方の案: start を try で包み、失敗したら pause_events(False) と self._deep = None。
- 影響: PYNQ で PS の中継が DDR の領域を書く前に la_deep_arm を呼ぶと、エラーは見えるが、その後イベント記録（recording）の
  取り込みが止まったままになる（次に la_deep_arm → la_deep_collect が成功するまで）。
- 確信度: 高。

### BUG-LA-02 (C) la_arm の既定の pretrigger = 1024 が、深さ 1024 以下のバッファで ValueError
- 再現: test_la_arm_default_pretrigger_works_on_a_small_buffer
- 原因: probe_la.py:58 の既定値と la.py:183 の `0 <= pretrigger < depth`。
- 直し方の案: 既定を None にして min(1024, depth // 2) 等（使う側が見える形で返す）にするか、小さいバッファでは既定を深さに合わせる。
- 影響: 内蔵バッファが 1024 以下の構成（例: 小さい FPGA の簡易版）で引数なしの la_arm / la_capture が失敗する。実機の構成の深さは未確認。
- 確信度: 高（試験で再現。実機で起きるかは構成による）。

### BUG-LA-03 (C) rate_hz の 0・負の扱い
- 再現: test_rate_hz_zero_or_negative_is_rejected_with_a_message
- 原因: la.py:194 と la.py:339 の `round(self.clock_hz / rate_hz)`。0 は ZeroDivisionError（日本語の説明が無い）、負は
  max(1, ...) で黙って最高速（分周 1）になる。
- 直し方の案: rate_hz <= 0 は ValueError。
- 影響: 誤入力が失敗として見えない（負）か、分かりにくい例外（0）。返るサンプル周期は実際の値なので、周期を見れば気づける。
- 確信度: 高。

### BUG-LA-04 (D) la_deep_collect の要約が切り捨て・省いたチャンネルを言わない
- 再現: test_deep_summary_reports_what_it_omits_like_the_internal_one
- 原因: probe_la.py:16-26 `_transition_summary`。la_collect の `_channel_summary` は first_edges_truncated・first_edges_limit・
  constant_low を返すが、深い方は先頭 16 個で切ること、常に 0 の ch を省いたことを返さない（edges の数と比べれば切りは分かる）。
- 直し方の案: `_channel_summary` と同じ形（first_edges_truncated・constant_low）にそろえる。
- 影響: AI が「変化が無い ch」と「省かれた ch」を区別できない。設計原則（欠けを明示する）に反する。
- 確信度: 高。

## 疑い（再現できていない。実機または RTL の確認が要る）

- LA-S1（C）深いバッファのリングが折り返した後の read の範囲。la.py:428 `available = written - post - 1` は、`written`（CSR の
  書いた拍数）がリングの拍数を超えて増えるなら、before_beats=None（既定の「残っている全部」）で count がリングより大きくなり、
  同じ拍を 2 度読む。written が止まる（リングの拍数で飽和する）なら問題ない。RTL（la_deep_buffer.vhd の w_written）の確認が要る。
  la_deep_collect の既定は before_beats=4096 の指定なので通常は踏まない。
- LA-S2（C）trigger_time の補正（la.py:290 pin_time）は、ケイパビリティの段数・位相の幅を「タイムベースの刻み」として引く。
  サンプル周波数とタイムベースが違う構成（Primer は la_sample_hz 100.285714 MHz）で、段数の単位が一致しているかは未確認
  （試験は同じ周波数の模型だけ）。
- LA-S3（D）VCD の時刻は `$timescale 1ns` に丸める（la_vcd.py:89）。サンプル周期が整数 ns でない構成（Primer の 9.971 ns 等）では、
  隣のサンプルの間隔が 9 か 10 ns になる（値の順序は保たれる。時間の精度は最大 0.5 ns）。
- LA-S4（D）Waveform.view は画素の範囲を「先頭は切り下げ、終わりは切り上げ」で取るので、幅が区間を割り切らないとき隣の画素と
  1 サンプル重なる。重なりの変化で隣の画素が x になりうる（消えるのではなく余分に x。欠けは出ない）。
- LA-S5（D）VCD の既定の名前（la_vcd.py:13 DEFAULT_NAMES = ch8・ch9 が内部信号）は、names を渡さないと必ず混ざる。今のケイパビリティの並び
  （ピン la_pins = 8 → 内部 2 → 専用の入力）では ch8・ch9 は実際に内部信号なので正しい。la_pins が 8 でない構成ができたら、
  names を渡さない書き出し（write_vcd 直接・sigrok.py）で誤る。今は顕在化しない。
