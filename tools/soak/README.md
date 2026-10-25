# 連続試験（ソーク試験）

実機の PYNQ-Z2 の fprobe（Ethernet）を無人で何時間も回し、不具合・劣化・取りこぼしを見つける道具。FPGA 解析（観測）を主に見る。

```
python tools/soak/soak.py --host <PYNQ の IP> --until 06:30 --log soak.jsonl
```

- `--until HH:MM`（次にその時刻になるとき）か `--hours N` で止まる。止めるファイル（既定 `soak.stop`）を作っても、Ctrl+C でも、実行中のシナリオが終わってから安全に止まり、最終の要約を出す。
- ログは `--log` の JSON Lines（1 行 = シナリオ 1 回: 時刻・合否・失敗の理由・数字・所要時間・この実行器自身の RSS/ハンドル/スレッド）。要約は `--summary-interval`（既定 600 秒）ごとに標準出力と `<log>.summary.txt` へ。
- 例外は記録して次へ。連続 3 回（`--max-consecutive-failures`）失敗したら、待って（`--backoff` 秒から倍々、最大 300 秒）再接続する。
- `--list` でシナリオ名。`--scenarios a,b` で絞る。`--weights obs_long=10,ping=1` で時間配分を変える（0 で外す）。
- 合否の基準（許容・長さ）はすべて引数。`python tools/soak/soak.py --help` に既定値。道具は判断せず、要約は事実（回数・失敗数・各数字の最初/最後/最小/最大/標準偏差・累計）だけ。
- 実機の準備は `gateware/tests/hil/test_fpga_obs_hw.py` と同じ（PYNQ に fprobe_pynq_deep、観測の 4 本と Primer の obs_target_primer、JB 1 番にカウンタの 12 ビット、JA に Nucleo）。他の道具で PYNQ を同時に使わない。
- `FPROBE_HOME` は使い捨ての一時フォルダに替える（利用者の保存を触らない）。
- 試験: `pytest tools/soak`（模擬の機器。実機は要らない）。

## 時間配分（既定の重み。実行時間の割合 %）

次に走らせるのは「使った時間 / 重み」が最小のシナリオ。短いシナリオは回数が多く、長いものは回数が少ないが、時間の割合は重みの比になる。

| 重み | シナリオ | 内容と合否の基準 |
|---|---|---|
| 30 | obs_long | 観測を 60 秒。gap 0・lost 0・overflow_seen/stuck なし。値が 1 ずつ増える（組 0 は 16 ビット）。変化の間隔が全部 4096 サイクル。件数が 秒×27 MHz/4096 の ±2%。値が変わる。測る: 時刻の傾き（fprobe の刻み/対象のサイクル）と申告との差 ppm（ドリフト。判定はしない） |
| 20 | obs_reopen | open/close を 20 回。毎回 ID が 0x464F4253。失敗は種類（id_timeout / 答えない）つき |
| 5 | obs_quiet | 変化が遅い組 1 を 60 秒。値の変化が無い間も時刻が進み続ける: fprobe の時刻が単調。変化の間隔が 2^28 サイクル。件数が期待 ±2 個。fprobe の時刻の進みと対象のサイクルの比が申告と ±0.1%（刻みの合い方） |
| 2 | obs_count | `fpga_obs_count` の回数（`mark rose` / `mark fell` / `counter changed`）が、同じ記録の値をその場で数えた回数と一致 |
| 2 | obs_groups | select で組 0・1・0・1 を切り替える。両方の組が読め、組の切り替えの印がある。同じ組の続きは 1 ずつ増える。gap 0 |
| 2 | obs_halt | 条件 `mark rose` → MCU 停止（trigger_set の halt）を 1 回。2 秒以内に停止、トリガの count ≥ 1。測る: 停止までの遅延（trigger_set が返ってから HALTED を 1 ms ごとの問い合わせで見つけるまで。問い合わせの周期を含む上限）。終わりに resume して RUNNING に戻る |
| 10 | chaos_obs | 観測の状態遷移のランダムな操作列（下記）。不変条件が 1 つでも破れたら失敗。操作列と種はログの `details` に残る |
| 3 | swd_mem | Nucleo の DBGMCU_IDCODE を 200 回: 全部が最初の読みと同じ（`--idcode-expect 0x...` で期待値も）。レイテンシ p50/p99/最大。SRAM 0x20004000 に 2 つのパターンを書いて読み戻し、元の値に戻す |
| 2 | sampler | 10 kHz × 5 秒（IDCODE の番地）。errors 0。書き込まれた数が期待の ±10%。値が 1 通り |
| 4 | la | 深いバッファで ch10 を取り込み、立ち上がりの周期の平均が 2^13/27 MHz の ±2%（立ち上がり 5 個以上） |
| 8 | la_deep | 深いバッファを rate（1・10・100 MS/s）× RAW/RLE の 6 通りを実行のたびに巡回し、pretrigger 0・1・16・4096 の 4 通りで取り込む。辺の周期が 2^13/27 MHz の ±2%（`--la-tol`）、トリガの位置が pretrigger と一致（`--la-trigger-tol` サンプル、既定 0。R4 が直した 0 番・1 番サンプルの境界）。立ち上がり 5 個以上。RAW はトリガ後に周期 7 個ぶんの拍、RLE は 200 拍 |
| 8 | la_obs_align | カウンタの同じビット（ch10 = 観測の組 0 の bit0）の辺を、深いバッファと観測で同時に取る（手本 test_o2）。ロジアナの辺ごとに最も近い観測の辺との差（観測 - ロジアナ、ns）を測る。辺が両方 5 個以上、差の最大絶対値が `--align-max-ns`（既定 200）以内。平均・最大・最小・標準偏差・最大絶対値を数字に出し、要約で夜のあいだの分布（ドリフト）を見る |
| 2 | eventlog | MCU を 100 回読む間の EventLog: written > 0・lost 0・読めた数 = min(written, 容量)・stop の後に written が増えない |
| 2 | ping | UDP の往復（CSR 読み）200 回。失敗 0。p50/p99/最大 |

obs_long + obs_reopen = 50%、観測全体で 78%（ロジアナ系は la 4 + la_deep 8 + la_obs_align 8 = 20%）。

## chaos_obs（ランダムな操作列）

開き直し・停止中の操作・途中の失敗の後の持ち越し・止まったまま戻らない、といった状態遷移の不具合を、決まった手順では出にくいので、有効な操作をランダムに長く流して探す。状態（closed / opened / running / stopped）は自分で追い、今の状態で許される操作だけを選ぶ。

- 操作: open / reopen（開いたまま open）/ close / start / stop / select（組 0・1）/ reg_read（0〜9・14・15）/ reg_write（14 = CONTROL、15 = SELECT。組 2 以上は無視される）/ status / wait / trigger_set / trigger_clear（条件 `mark rose`、halt なし・la のみ。MCU に触れない）/ bad_open・bad_register（範囲外の引数。文書化された ValueError が出るのが正しい）。
- 不変条件（各操作の後。最初の違反でそのラウンドを止める）: (a) `status()` が追っている状態と矛盾しない（開いていれば running・id_ok・ID が FOBS・stuck/id_timeout なし、記録していなければ target_running なし。閉じていれば running・driving なし）(b) open の ID が 0x464F4253 (c) 記録していない間、stop の後（飛んでいる記録が尽きるのを `--chaos-settle-s` 待った後）に値の記録が増えない (d) open の直後に前のセッションの記録が見えず、CONTROL・SELECT のレジスタが 0、ID・申告のレジスタ（0〜9）が open の戻りと一致 (e) 有効な操作は例外を出さず、出る例外は ValueError / RuntimeError / TimeoutError だけ（素の KeyError 等は「文書化されていない例外」の失敗）。範囲外の引数は ValueError (f) 走っていた区間の記録（切れ目・組の切り替えの印の無い区間）で、各組の値が 1 ずつ増え、範囲内（読む前にリングが一周した区間は lost に数えて判定しない）(g) RSS・ハンドルはラウンドの前後の差を `rss_mb_delta` / `handles_delta` に出す（判定はしない。長期の推移は各行の `resources`）。
- 引数: `--chaos-ops N`（1 ラウンドの操作数。既定 60）、`--seed S`（既定 -1 = ラウンドごとに新しい種。S 以上を指定すると n 回目のラウンドは S + n）、`--chaos-wait-max-s`、`--chaos-settle-s`。
- 再現: ログの `details` に `seed` と `ops`（実行した操作の並び。失敗したらそこまで）が入る。`--chaos-replay FILE`（`ops` の JSON 配列か、その `details` を持つログ。失敗した記録を優先）で同じ列をそのまま流せる。例 `python tools/soak/soak.py --host H --scenarios chaos_obs --chaos-replay soak.jsonl --hours 0.01`。
- 数字: ops・opens・events・chaos_gaps・chaos_event_lost・rss_mb_delta・handles_delta。

## 要約に必ず出るもの

観測の欠け gap の累計、イベント lost の累計、stuck の回数、id_timeout の回数、時刻の傾きの最小・最大・標準偏差、シナリオ別の回数・失敗・例外・使った時間、各数字の最初/最後/最小/最大/標準偏差。リソース（rss_mb・handles・threads）は `resources` の欄（最初と最後の差がリーク）。

## 実機で確かめてほしい点（手本の試験と違うところ）

- `RealRig` は `Probe(eth=HOST)`（API の実装）を使う。条件の数え・条件からの停止は手本と同じ Probe の呼び出し。それ以外は手本どおり Session/FpgaObs/EventLog/Sampler/DeepAnalyzer。
- obs_long は 60 秒ぶんのイベント（約 40 万件）を一度に読む。リングの容量とイベント読みの時間（長いと 1 回が 60 秒よりかなり長い）。件数が合わなければ lost に出る。
- obs_quiet が組 1 を `obs.select(1)` で見る。組 1 の値が 2^28 サイクルごとに 1 増えるという前提（Primer のカウンタの 43〜28 ビット）。違えば `--quiet-interval`。
- obs_halt は Nucleo に attach して走っている前提（走っていなければ resume する）。Nucleo のファームがその間止まっても構わないこと。
- swd_mem の SRAM 書き込みは既存の試験と同じ 0x20004000。Nucleo のファームが使うなら `--ram-address`。サンプラ・イベントログは IDCODE の番地を読む（値が変わらないので全部同じ値のはず）。
- 立ち上がり周期の理論値 2^13/27 MHz はカウンタのビット 12 の周期。ch10 の取り込みの長さ（post_beats 200000）に立ち上がりが 5 個以上入ること（`--la-min-edges`）。
- chaos_obs（実機は未確認）: 観測の回路が TLR で CONTROL・SELECT を 0 に戻す前提（obs_core.vhd）。opened/stopped で `target_running` が 0 になる前提（stop の後に待つ）。組の切り替えの印は記録していないときに出ても許す（値の記録だけを数える）。trigger_set は `Probe.fpga_obs_trigger_set(halt=False, la=True)` で、ケイパビリティ obs_match が要る。記録は `EventCursor`（open のたびに作り直し）で読み、ラウンドの間 EventLog を走らせっぱなしにする（他のシナリオは都度 start/stop）。
- obs_reopen の失敗の種類は FpgaObs の例外の文言（id_scans を含むか）で分けている。
- la_deep（実機は未確認）: rate 100 MS/s・RAW は周期 7 個ぶん約 26,000 拍をストリームで読む。`DeepAnalyzer.start(pretrigger=…)` はマスク無しで始め、開始から pretrigger サンプル後に成立する前提でトリガの位置 = pretrigger を求める。RLE は ch10 以外が変化するとトリガ後 200 拍に ch10 の辺が 5 個入らないことがある（`--la-deep-rle-post-beats`）。
- la_obs_align（実機は未確認）: ロジアナの辺の時刻は `trigger_time + (i + 1 - trigger) × 周期 × timebase_hz`（手本と同じ）。観測の辺は time_fprobe のある値の bit0 の変化。
