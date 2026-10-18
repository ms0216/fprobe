# F3a 記録（ホスト側）Implementation Plan

**Goal:** すべての出来事を 1 つの時刻の上の 1 つの記録に入れ、読み出し・検索・統計・要約・書き出しを提供する（ホスト側）。

**Spec:** docs/superpowers/specs/2026-10-17-f3-timeline-recording-design.md

**Architecture:** `host/fprobe/recording.py` に、時計の対応付け（`ClockMap`）、記録の書き手（`Recorder`、SQLite と波形の
かたまりのファイル、書き込み用のスレッド）、読み手（`Recording`）を置く。デバイスとデーモンへの接続（取り込みの元・API）は
Task 3・4 で。

## Tasks

1. **ClockMap**: 点（ホストの時刻の中点、タイムベース、往復時間）から直線を当てはめ、ホストの時刻 → (タイムベース, 不確かさ)。
   試験: ずれ・傾きのある偽の点、不確かさ = 往復時間の半分の最大、点が 1 つのとき（傾きは公称値）。
2. **Recorder と Recording（ストア）**: 出来事の追加（まとめて書く）、波形のかたまり、記録中の読み、範囲の読み、検索、
   間隔の統計、縮図、波形の要約、書き出し（VCD・CSV・JSON）、落ちた記録を開く、100 万件の性能。
3. **取り込みの元**: `Recorder.attach_*`（ロジアナの捕捉・LaStream のチャンク・EventLog の差分）。偽のデバイスで試験。
4. **API**: `recording.*` を仕様に足し、デーモンが記録中に activity と target.state を書く。偽の機器で試験。
5. **HIL**: 実機でロジアナの連続捕捉・イベント・AI の操作を同じ記録に入れ、順序を確かめる。
