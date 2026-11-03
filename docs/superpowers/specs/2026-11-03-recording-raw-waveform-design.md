# 記録の波形を生の拍で保存する（記録の形式の版 3）

2026-11-03。`2026-11-03-la-raw-capture-design.md`（VCD の垂れ流しの取り込みと書き出しの分離）の残し事項: 記録
（`recording_stream.py` の `LaRecordingStream`）の受け取りのスレッドが、受け取りの中で `chunk.changes()`（変化点の取り出し）を
行っている。これを生の保存に置き換え、変化点への復号は読む側（記録の読み手）に移す。

## 1. 現状

`LaRecordingStream._run` は、チャンクを受け取るたびに `chunk.changes()`（numpy で拍 → 変化点）を行い、`chunk_s` 秒ぶんの変化点を
1 つの `Transitions`（値 uint16・開始 uint64）にして `Recorder.add_waveform` に渡す。ファイル `waveform/NNNNNN.bin` は
「JSON の見出し 1 行 + 値の列 + 開始の列」で、`chunks` 表に (t0, t1, path, ticks_per_sample, channels, length) の 1 行。
読み手（`Recording._transitions` / `_transitions_np` / `chunk_skew`）はこの形を読む。受け取りのスレッドが変化点の取り出しに
律速される点は、VCD の垂れ流しと同じ（毎サンプル変化する信号で、線の速さの 3〜24 %）。

## 2. 設計

波形のファイルを 2 つの形式にする。**ファイルの見出しの `format` が形式を自己申告する**（読みはこれで分ける）:

| format | 中身 | 書く道 |
|---|---|---|
| 無し（形式 1。今まで） | 変化点（値 uint16・開始 uint64） | `Recorder.add_waveform(Transitions, start_ticks)` |
| 2（新） | 生の拍（RAW・RLE・詰めた語のまま） | `Recorder.begin_raw_waveform(...)` → `RawWaveformWriter` |

形式 2 のファイル（拡張子 `.fpraw`。形式 1 は `.bin`）:

```
FPRAW2 {"format": 2, "rle": bool, "pack": int, "channels": int, "period_s": float, "previous": int|null,
        "channel_skew": [...]（ずれがあるときだけ）}\n
<拍の列そのまま>
```

見出しの行の頭の印 `FPRAW2 `（と拡張子）は、**形式 1 しか知らない古い読み手を必ず「読めない」誤りで止める**ためのもの。古い読み手は見出しの行を
JSON として読むので、印があると `json.JSONDecodeError` で止まる（波形を読む口も、見出しだけを読む口 `chunk_skew` も）。見出しの欄を足しただけの形では、
欄を見ない古い読み手が通る口が残る（最初の実装では波形の読みは `KeyError: 'count'` で止まったが、`chunk_skew` は成功を返した）。
新しい読み手は印で形式を分ける（印の無い行は形式 1）。

- `previous` = かたまりの最初のサンプルの直前のサンプルの値（`StreamChunk.previous`。流れの先頭・欠けの後は null）。
- サンプルの数 `length` は `chunks` 表の列にある（今までと同じ。ファイルには持たない）。拍の列は届いたまま追記する（復号しない・
  かたまりを閉じるまでメモリに持たない: 受け取りのバッファは写さずに指しているだけなので、届いたらすぐファイルへ書く）。
- 読み（`Recording._transitions`）は `la_wave.stream_transitions(data, rle, pack, 0, previous)` で変化点にする。
  今までの変化点のファイルと**同じ変化点になる**ように、かたまりの頭が続いている値と同じ（変化点が頭に無い）ときは、頭に
  (previous, 0) を足す（今の `LaRecordingStream` の「頭は続いている値から始める」と同じ。`previous` が null のときは足さない:
  先頭のサンプルは必ず変化点になる）。`trigger` は 0。
- 長期の記録（`SegmentedRecorder`）の索引 `wave` 表の first / last / toggles（縮図の事実）は、変化点から取らずに、拍の列から
  `la_wave.raw_facts` で直接求める（変化点を作らない。first = 最初のサンプルの値、last = 最後のサンプルの値、toggles = 最初の値と
  違う値が出たビット）。変化点から求めた値と同じになることを試験で固定する。`_wave_bytes`（間引きの容量の勘定）はファイルの
  実際の大きさ（形式 1 は `10 × 変化点 + 64` の見積もり、形式 2 は見出し + 拍の列の長さ）。

## 3. 記録の形式の版（PRAGMA user_version）

出来事の表の版（`recording_table`）と同じく、`recording.sqlite` の `PRAGMA user_version` で見分ける:

| user_version | 意味 |
|---|---|
| 0 | 表 events（版 1）。波形は形式 1 のみ |
| 2 | 表 event + names（版 2）。波形は形式 1 のみ |
| **3（新）** | 表は版 2 と同じ。波形のファイルに形式 2 が現れうる（形式 1 も混ざってよい） |
| 4 以上 | 未来の版。この版は開かずに断る（`RecordingVersionError`。黙って読み違えない） |

- **新しく作る記録はいつも 3**（出来事の表の版 2 → 3 の変更は無い。`version_of` は表の形で判定するまま）。
- **古い記録（0・2）は書き換えない・移さない**: 開いてそのまま読める。古い記録に書き足すとき（`Recorder` で開き直す）も、
  user_version を変えず、波形は形式 1 で書く（`Recorder.waveform_format` = 1。`LaRecordingStream` はこれを見て、変化点の道に戻る）。
  形式 2 のファイルを user_version < 3 の記録に置かない（書き手が作らない）。読みは見出しの `format` で分けるので、
  古い読み手が新しい記録を開いても、形式 2 のかたまりは印のせいで読めずに止まる（読み違えない。試験: `tests/legacy_reader.py` に写した古い読み手と、
  変更前の版の実物）。新しい読み手は 4 以上を断る。記録を持ち運ぶ側（`merge`・`fprobe_api` の取り出し）は、形式を問わず `Recording` を通す。
- 分割した記録（`layout = segmented`）は分割ごとに普通の記録なので、分割ごとに上の規則（新しい分割は 3。古い分割はそのまま）。
  索引（`index.sqlite`）の `wave` 表の形は変えない。

## 4. 読みの同値（試験で固定する）

形式 1 と形式 2 で、同じ流れから作った記録の次が**同じ答え**になる:

- `waveforms()` の変化点（値・開始・length・channels・skew）、`waveform_view`、`Waveform`（`_wave`）、`channel_count`、`span`、`coverage`
- `export_vcd`（バイト単位）、`export_csv`、`export_json`、`density` 等の出来事の読み
- 分割した記録の `summary`（縮図）

古い記録の固定試料（変更前のコードで作ったディレクトリを `host/tests/data/recording_legacy/` に置く）を読んで、
期待（VCD の sha256・変化点の要約・CSV/JSON）が変わらないことを確かめる。

## 5. 受け取りの変更

`LaRecordingStream._run`: `rec.waveform_format == 2` なら、チャンクの拍を `RawWaveformWriter.write` で追記し、かたまりが
`chunk_s` 秒ぶんになったら `finish`（`chunks` 表に 1 行）。変化点の取り出しをしない。欠けの区間（gap）はかたまりを閉じて
欠けの後から新しく始める（今と同じ）。`waveform_format == 1` の記録（古い記録に書き足す）は今までの道。
`merge`・`add_waveform(Transitions, …)` を呼ぶ他の道（試験・`demo_recording`・`merge`）は形式 1 のファイルを書く
（変化点を渡された書き手は変化点を持っているので、そのまま書くのが事実）。

## 6. 測り方と残し事項

- 前後の受け取りの速さ: `tools/bench/recording_stream_bench.py`（新規）。偽の機器の流れ（毎サンプル変化・方形波・RLE・詰め）を
  `LaRecordingStream` に流し、受け取りのスレッドが流れを消化した速さを、形式 1（変更前の道）と形式 2 で測る。報告に足す。
- 実機: PYNQ で `recording.la_stream.start` を長時間流して `overrun` を比べるのは手元で確かめる。
- 残し: 読み手は形式 2 を読むたびに復号する（`Recording` の波形のキャッシュ `WAVE_CACHE_BYTES` で覚える）。
  読みが遅くなる場面は報告に測って書く。
