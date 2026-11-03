# ロジアナの取り込みと VCD の書き出しを分ける（生の保存ファイル `.fplaraw`）

2026-11-03。指示書 `2026-11-03-cloud-sessions-plan.md` §C。目標は性能の表（`docs/reports/2026-11-02-performance-abcd.md`）の D（理論値）。

## 1. 現状と問題

`DeepAnalyzer.stream_to_vcd`（`host/fprobe/la.py`）は、1 本のループの中で次を続けて行う:

```
LaStream.chunks（受け取り: リンク → 見出し分け → 欠けの置き）
  → StreamChunk.changes()      拍・RLE → 変化点（numpy。la_wave.stream_transitions）
  → VcdWriter.write()          変化点 → VCD の行のバイト列（la_vcd。描くスレッドと書くスレッドは別）
```

受け取り（機器のリングを空ける側）が、変化点の取り出しと VCD の行の組み立て（CPU）に律速される。VCD は 1 変化点あたり
数十バイトで、入力の拍（RLE なら 4 バイト/変化点）より桁違いに大きい。書き出しが遅いとホストが遅れ、機器のリングが溢れる
（`overrun`）。取り込みの速さは「線の速さ」だけで決まるべきで、VCD への変換の速さに依存してはならない。

記録（`recording_stream.py` の `LaRecordingStream`）は別の道: 受け取りのスレッドが変化点を取り出し、波形のかたまりとして記録へ
入れる（VCD は後から `Recording.export_vcd`、`recording_vcd.py`）。こちらは書き出しが既に後だが、受け取りのスレッドでの変化点の
取り出しは同じ負担になる（§7 に残す）。

## 2. 設計

取り込みと書き出しを別の層にし、間に**生の保存ファイル**（`.fplaraw`）を置く。

```
取り込み:  LaStream.chunks → RawCaptureWriter.write(chunk)         受け取ったバイト列をそのまま書く（復号しない）
書き出し:  RawCapture（読む）→ StreamChunk → changes() → VcdWriter    後から、取り込みと無関係の速さで
```

- 取り込みは**何も解釈しない**: `StreamChunk.data`（拍の列。RAW・RLE・詰めた語のまま）を、chunk の位置情報（`first`・
  `previous`・`count`・`gap`）と一緒にそのまま追記する。変化点の取り出し・VCD の整形をしない。`memoryview` はコピーせず書く。
- 書き出しは `RawCapture` から `StreamChunk` を復元して、今と同じ `chunk.changes()` → `VcdWriter` に通す。**VCD の部品は
  変えない**ので、出力は今とバイト単位で同じ（`test_la_vcd_golden.py`・`test_recording_vcd_golden.py` と、新しい等価の試験）。
- 取り込み側が持つ情報（流れの性質: rle・pack・ch 数・サンプル間隔・first_channel・ch 名・ch ごとの時刻のずれ）は、ファイルの
  先頭の**ヘッダ**に書く。書き出しはファイルだけで完結する（機器・Capabilities に触れない）。
- 取り込み側は、書き出しの速さに触れない。`stream_to_raw` は受け取りのループだけを持つ。

## 3. ファイル形式（版 1）

すべて little endian。バイト列のそのままの保存なので、サンプルの意味の解釈（RLE の復号等）はファイルの外（`la_wave`）にある。

```
file   := header chunk* [index]
header := MAGIC(8) = b"FPLARAW\x01"   META_LEN(u32)   META(JSON, utf-8, META_LEN バイト)
chunk  := CHUNK_HEAD(struct "<4sB3xIQqI4xQQQ" = 60 バイト)   DATA(nbytes バイト)
index  := OFFSETS(u64 × n)   N(u64)   INDEX_OFFSET(u64)   TAIL_MAGIC(8) = b"FPLAREND"
```

`CHUNK_HEAD` の項目: `b"CHNK"`・flags（bit0 = previous あり、bit1 = 直前に欠けあり）・nbytes・first（u64、最初のサンプルの通し番号）・
count（i64、サンプル数。-1 = 数えていない）・previous（u32、flags bit0 のとき有効）・gap_start / gap_samples / gap_beat（u64、
bit1 のとき有効）。`StreamChunk` の `data / first / previous / count / gap` の写しで、`events` は保存しない（イベントは別の道
＝ イベントのリング。生のファイルはロジアナの拍の保存）。

`META`（JSON）: `{"version": 1, "rle": bool, "pack": int, "channels": int, "first_channel": int, "sample_period_s": float,
"names": {"<ch>": "<名前>"}, "skew": [float|null …], "timebase_hz": float|null}`。`skew` は ch ごとの時刻のずれ（分からない ch は
null。`fprobe.timing`）。`timebase_hz` が null のときは skew を空にする（今の `stream_to_vcd` と同じ規則）。

**索引**（`index`）は閉じるときに書く。末尾の `TAIL_MAGIC` があれば索引から chunk の位置を引ける（`len`・範囲の読み・並列の書き出しの
分割に使う）。無ければ（途中で落ちた・書いている最中）先頭から走査して読む。最後の chunk が途中で切れていれば、それは捨てて
`complete = False` とする（切れた所の前までは正しい）。

## 4. API

`host/fprobe/la_raw.py`（新規。`la.py` を import しない: 依存は `la_wave`・`la_vcd` のみで、`StreamChunk` は `la.py` が持つので
`la_raw` は「生のかたまり」の型 `RawChunk`（data / rle / first / previous / pack / count / gap）を自分で持ち、書き出しで `la_wave.stream_transitions`
を直接呼ぶ）。

| 名前 | 役割 |
|---|---|
| `RawMeta` | ヘッダの内容（dataclass） |
| `RawCaptureWriter(path, meta)` | 追記。`write(chunk)`・`close()`（索引を書く）。with 可 |
| `RawCapture(path)` | 読み。`meta`・`complete`・`chunks()`（`RawChunk` を順に）・`len`・`samples` |
| `raw_to_vcd(raw, vcd_path, names=None, workers=None)` | 書き出し。`RawCapture` → `VcdWriter` |

`DeepAnalyzer`（`la.py`）:

- `stream_to_raw(raw_path, duration_s, *, <stream と同じ引数>, usage_interval_s=None) -> StreamStats`: 取り込みだけ。
- `stream_to_vcd(path, duration_s, *, …, raw_path=None)`: 今の署名の末尾に `raw_path` を足す。**中身は `stream_to_raw` + `raw_to_vcd`**。
  `raw_path=None` は VCD の隣の一時ファイル（`<path>.fplaraw.tmp`）に取り込み、書き出しの後に消す。指定すればそこに残す。
  途中で例外（`StreamLinkLoss`・`StreamHoldExceeded` 等）が出ても、それまでの分を書き出してから例外を再送出する
  （今は途中までの VCD が残るのと同じ。**黙って捨てない・隠さない**）。
- 返す `StreamStats`・VCD の内容・例外の型は今と同じ。

`Probe.la_stream`（API）は引数 `raw_path`（既定 None）を足す。新しい API `la_raw_to_vcd(raw_path, vcd_path, names)` で、取り込んだ
ファイルを後から VCD にできる。応答に `raw` を足す（`raw_path` を指定したときのパス、無ければ null）。画面は変えない
（新しい項目は画面に出さなくてよい。CLAUDE.md の「人と AI の見えるものは一対一」のため、画面の API 一覧が自動生成なら自動で並ぶ）。

## 5. 互換と不変条件

- 出力の VCD はバイト単位で今と同じ。golden（`test_la_vcd_golden.py`・`test_recording_vcd_golden.py`）と、
  `stream_to_vcd` の新旧の等価（`test_la_stream.py` の既存の試験）で固定する。
- 欠けの区間（`gaps`）・pack・RLE・first_channel・ch ごとのずれ・`StreamStats` は今と同じ。
- ヒューリスティックなし: 書き出しの分割・並列の度合いは固定の規則（ファイルの chunk 単位）で、中身の性質で変えない。
- 後始末: 取り込み・書き出しとも with で閉じる。一時ファイルは `finally` で消す（取り込みが失敗しても残さない）。

## 6. 試験

- `host/tests/test_la_raw.py`: 書いて読むと同じ chunk（data・first・previous・count・gap）／索引あり・索引なし（切り詰め）・
  最後の chunk が途中で切れたファイル／`complete` の値／空のファイル／版と magic の誤り／memoryview を書ける／
  `raw_to_vcd` の出力が `write_vcd`（全体の変化点）と一致（RAW・RLE・pack・gap・ずれあり）。
- `test_la_stream.py` に: `stream_to_vcd(raw_path=…)` が raw を残す・None なら一時ファイルが残らない・途中の例外でも途中までの VCD が残り例外が出る。
- 取り込みの道が書き出しの速さに律速されない: 書き出しを遅くした（`VcdWriter.write` を遅くした）状態でも、`stream_to_raw` の時間は変わらない
  （時間の試験は `host/tests/perf_budget.py` の `within`）。

## 7. 速さの測り方と残したこと

- 道具: `tools/bench/la_vcd_bench.py` に段 `capture`（`RawCaptureWriter.write` のみ）と `export`（`RawCapture` → VCD）を足す。
  前の段 `total`（変化点の取り出し + VCD。今の `stream_to_vcd` の受け取りループの中身）と同じ場面・同じ量で比べる。
  結果は `docs/reports/2026-11-03-cloud-C.md`。
- 残す: 記録（`LaRecordingStream`）の受け取りのスレッドの変化点の取り出し。波形のかたまりが記録の形式（変化点）なので、生の保存に
  置き換えるには記録の形式の版を上げる設計が要る（別の仕様）。今回は VCD の垂れ流しの道だけ分ける。
- 実機: PYNQ の実測（`la_stream` の持続）は手元で確かめる（この環境に機器が無い）。
