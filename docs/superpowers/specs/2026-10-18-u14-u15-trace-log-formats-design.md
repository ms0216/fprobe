# U14・U15 既存のトレース形式・ID ベースのログの取り込み（設計）

満たす競合の機能（`docs/reports/2026-10-18-gap-analysis.md`）:
- U14: SEG-64, KEIL-01, KEIL-03, TZ-10, ESP-02（取り込み）, PFT-04（取り込み）, TRX-01, TRX-02, ZEP-01, ZEP-02, ZEP-03, FRT-05
- U15: SEG-67（ホスト整形のログ）, TZ-11, MF-07, ESP-09, PRS-04, PRS-11, TRC-01, TRC-02, TRC-03

## 目的

ターゲットに既に組み込まれている記録の仕組み（SEGGER SystemView・Zephyr の CTF・Percepio Tracealyzer・Keil の
Event Recorder・ThreadX の TraceX、defmt・Trice・Compact logging）を、ファームを変えずに fprobe で読む。読んだものは
共通の時間軸の記録（F3）に入り、ロジアナの波形・DAP の転送・ほかの観測と同じ時刻で並ぶ。タスクの出入りは
U09 の時間軸の解析（負荷・ジョブ・逆転・割り込みとの合成）にそのまま渡す。各ベンダーのツールは自分の形式しか読めず、
波形とも同期しないので、ここが上回る点。

## 層と責務

| 層 | ファイル | 責務 |
|---|---|---|
| 書式 | `fprobe/logfmt/printf.py` | C の printf をホストで（型付きの値・32 ビットの語から） |
| 枠 | `fprobe/logfmt/framing.py` | 0x00 区切り、COBS・rzCOBS（defmt）・TCOBS v1（Trice） |
| ログの形式 | `fprobe/logfmt/defmt.py`・`trice.py`・`compact.py` | 表（ELF・til.json）と流れ → `LogRecord` |
| 番地の解読 | `fprobe/logfmt/addresses.py` | 文字の中のコードの番地 → 関数・ファイル:行（ESP-09） |
| ログの組み立て | `fprobe/logfmt/stream.py` | 形式の選び方、text の行、どの形式にも番地の解読 |
| トレースの形 | `fprobe/tracefmt/model.py` | `TraceEvent`（形式に依らない種類）・`Trace`・`TargetClock`・時間軸への変換・記録への書き方 |
| トレースの形式 | `fprobe/tracefmt/systemview.py`・`ctf.py`・`tracealyzer.py`・`tracex.py`・`eventrecorder.py` | 解読 |
| SCVD | `fprobe/tracefmt/scvd.py` | 型・式・書式、Component Viewer（メモリを読んで表示の木）、Event Recorder の書式 |
| 選び方・吸い出し | `fprobe/tracefmt/stream.py` | 流れ・一まとまり・RAM のスナップショット（RTT のリングを消費せずに等） |
| 機器 | `fprobe/probe_formats.py` | Probe の mixin `FormatTools`。受け口（RTT の listener・SWO / UART の窓・RAM の周期的な読み）、時刻、記録、時間軸の結果 |

解読の部品は機器に依らず、バイト列か「read(番地, 長さ)」だけを受け取る（単独で試験できる）。機器に触るのは
`FormatTools` だけで、必ず `_lock` の中。

## 受け口と時刻

| 受け口 | 方法 | 時刻 |
|---|---|---|
| rtt | ObserveTools の listener（取りこぼさない。rtt_read とは別） | ホストで受けた時刻を時計の対応付けで写す（記録中は記録の時計、そうでなければ始めたときにタイムベースを 1 回読む）。不確かさ = 対応付け + RTT の読みの周期 |
| swo | WindowTools の窓（stream swo）を `WindowCursor` で少しずつ読む → ITM のパケット（`TimedSwo`）→ port の Instrumentation のバイト | FPGA が SWO のバイトを受けた時刻（確か）。ITM の設定は testpoint と同じ（受信を始めてから TER を有効に、止めたら戻す） |
| uart | 窓（stream uart = イベント記録の uart_rx。今回 `_window_events` に足した） | FPGA が受けた時刻 |
| memory | Event Recorder の RAM を period_s ごとに読む（通し番号 since から続き） | 最初の読みの最後の出来事を読んだ時刻に合わせる（近似） |

ターゲットの時刻を持つ形式は `TargetClock`（最初に解読した出来事 ↔ 受けた時刻、以後はターゲットの時刻の速さ）で
タイムベースに写す。ずれ（ターゲットとプローブのクロックの差）は補正しない。速さが分からない形式（TraceX の
既定・CTF の id16）は記録に書かない（理由を notes に）。

## 記録（F3）

- stream "log": kind = 重要度（trace / debug / info / warn / error / println。重要度の無い形式は log）、解読できない
  かたまりは undecoded（body に生のバイトと理由）。a = 書式の ID。body = `LogRecord.to_dict()`（本文・書式・引数・
  ターゲットの時刻・場所・チャンネル・番地の解読）。
- stream "trace": kind = `KINDS`（task_in / task_out / task_ready / task_block / task_create / task_delete /
  task_info / isr_enter / isr_exit / idle / print / marker / object / overflow / info / other）、a = タスク、body = 形式・
  形式での名前・値・本文・ターゲットの時刻。
- stream "rtos": `trace_stop`・`trace_snapshot` の時間軸（run / switch / gap。U09 と同じ）。

## 形式ごとの要点と確かでない点

出典は各モジュールの docstring に。公開の仕様が無いもの・確かめられなかったものは次のとおり。

- defmt（ワイヤ形式 4）: 表・枠・引数・表示は公式の解読器の試験の値で確かめた。浮動小数の表記（ryu）がすべての値で
  一致するかは未確認。`:cbor` の表示は持たない。
- Trice: バイナリ形式・TCOBS・til.json は v1.x と v2 で同じと判断（v1.3.0 の現物では未照合）。typeX0・`_F`・`_C` は
  解読しない（生のバイト）。v2 の `{name}` の構造化テンプレートは扱わない。
- Compact logging: Memfault は公式のホストの解読器と同じ規則（log_fmt の位置 = ID、CBOR）。番地のログは
  Percepio の compact print・SystemView の PRINT_ELF と同じ考えの fprobe の約束（文字列の番地 + 引数の語）。
- SystemView: OVERFLOW の DropCount は累積と読んだ。ID 32 以上は記述ファイル（SYSVIEW_<OS>.txt）があれば名前と
  引数、無ければ "API<ID>"。ESP のコア番号の拡張は扱わない。.SVDat は先頭の ";" の行を読み飛ばすだけ（SEGGER の
  保存形式の中身は非公開）。ポストモーテムは RTT の上りの RdOff〜WrOff を消費せずに読み、最初の sync から。
- Zephyr CTF: 見出しは版で 4 通り（ctf_header）。metadata を渡せば版に合わせて読む。長さの欄が無いので表に無い
  ID の後は読めない。RAM の backend は線形で 0 埋めで終わる。
- Tracealyzer: PSF の流れ（4.6 以降）の見出し・時刻・エントリの表・出来事の枠は TraceRecorder v4.11 のソースで確認。
  出来事のコードの名前は FreeRTOS のポートのもの。RAM のスナップショット（RingBuffer・4.5 までの RecorderDataType）は
  形が流れと違い細部を確かめていないので読まない。減るタイマ・OS のタイマの時刻の形は生の値。
- TraceX: スレッドの切り替えは出来事として無いので、出来事の文脈（thread_pointer）が変わった所を切り替えとする
  （時刻は近似）。時刻の速さは ts_hz で渡す。Cortex-M のポートが ISR_ENTER / EXIT を自動で入れるかは未確認。
- Event Recorder: RAM の形はプロトコル 1.1 のソースから。uVision の読み方（巻き戻りの扱い）は非公開なので、通し番号・
  周回の番号・トグルビットで書きかけと上書きを見分ける。レベルは記録に残らない（SCVD から）。
- SCVD: `%F` は文書に無い（ELF の文字列とした）。readlist・list の limit は「その値に達したら終わり」。

## API（`host/api/fprobe_api.json`）

| メソッド | kind | 内容 |
|---|---|---|
| log.start | control | ログの流れの解読を始める（形式・受け口・ELF / til.json） |
| log.stop | control | 止める |
| log.status | read | 様子 |
| log.read | read | 行（since から。重要度・文字で絞る） |
| log.decode | read | バイト・ファイルを解読（機器に触らない） |
| log.annotate | read | 文字の中の番地を関数・ファイル:行に |
| trace.start | control | トレースの流れの取り込み（systemview・ctf・tracealyzer）、Event Recorder の RAM の周期的な読み |
| trace.stop | control | 止めて要約、時間軸（rtos_timeline_* の解析） |
| trace.status | read | 様子 |
| trace.events | read | 出来事（種類・タスクで絞る） |
| trace.snapshot | control（observes） | RAM から吸い出して解読（ポストモーテム） |
| trace.import | control | ファイルを解読（時間軸の結果を置き換えるので control） |
| scvd.load | control | SCVD を読む |
| scvd.view | control（observes） | Component Viewer の表示の木 |

## 試験

- 部品: 公式の試験の値（defmt の解読器・rzcobs・tcobs・trice の TestTREX・Memfault の README）、実物の資料
  （Zephyr v4.4 の TSDL、RTX5.scvd・EventRecorder.scvd）、仕様のとおりの書き手（SystemView の _SendPacket、
  EventRecorder.c、tx_trace、PSF）で作ったデータ。
- 試験用 ELF（zig cc）: defmt_demo（defmt の表と DWARF の場所）、compact_demo（.rodata の書式・Memfault の log_fmt）、
  formats_demo（実機試験用。下記）。
- Probe の道具: 偽の CPU（rtt_demo の RTT）・偽のイベント記録のリング（SWO・UART の窓）・偽のメモリ。

## 6. 実機試験（formats_demo）

`host/firmware/formats_demo`: RTT の上り "Terminal"（tick の行・pc=0x… の番地）・"defmt"（rzcobs）・"SysView"（下り
"SysView" の START か sysview_autostart で始める。worker / IDLE を 10 ms ごとに切り替え、SysTick を割り込みとして）・
"Compact"、ITM ポート 2 の Trice（枠なし）、RAM の Event Recorder（ThreadSwitched・stdout）・CTF の RAM の backend・
TraceX のバッファ。手順は `gateware/tests/hil/test_formats_hw.py`。

## 未対応（後で）

- 書き出し（SystemView・CTF・Perfetto の形式で出す）は U13。
- ESP-IDF の RISC-V のパニックのレジスタの吐き出しを GDB のスタブとして読む形（Backtrace の行の番地の解読はある）。
- Tracealyzer の RAM のスナップショット、4.5 までの形式、FreeRTOS 以外のポートのコード表。
- Trice の typeX0・`_F`・暗号化（XTEA）、defmt の `:cbor`。
