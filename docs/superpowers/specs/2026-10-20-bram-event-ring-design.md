# 外部メモリの無い構成のイベント記録（BRAM のリング）

2026-10-20。利用者の離席中に私が起こした（「10 時間、止まらず検討を進めて」）。判断はすべて私のもので、戻ったら確かめてもらう。

## 1. なぜ

DAP のイベント記録（転送・割り込み書き込み・SWO / UART のバイト・補助の計測値・**サンプラの値**）は、深いバッファの部品
（`la_deep_buffer`）が外部メモリのリングに書いている。外部メモリの無い構成（PYNQ-Z2 の `fprobe_pynq`、Tang Nano 9K、
Primer の `fprobe_serial`）では、イベント記録が丸ごと無い（EVENTS_CAPACITY = 0）。そのため、そこでは次が使えない:

- サンプラ（F4、変数を止めずに見続ける。ライブウォッチ・`watch.*`）— 値はイベントとして届くため。
- 共通の時刻での記録（ロジアナの立ち上がり ⇄ 変数の変化）。2026-10-20、XIAO の「押してから状態が変わるまで」を
  PYNQ で精密に測れなかったのはこれが理由（PC から読み続ける粗い測定に落とした）。

設計原則（CLAUDE.md）の「小さい FPGA ではバッファの深さが減るだけで、同じ PC ソフトが動く」に反している。

## 2. ゴール・非ゴール

- ゴール: 外部メモリの無い構成でも、内蔵 BRAM の小さなリング（構成ごとの件数、例 1024 件 = 16 KB）でイベント記録を持つ。
  **PC から見えるもの（CSR の events・la_deep の読み出しの窓・拡張のストリームの形式）は深いバッファと同じ**で、容量
  （EVENTS_CAPACITY）が小さいだけ。PC 側のソフトは変えない。
- ゴール: 外部メモリのある構成（Primer の USB・Ethernet 版）は何も変わらない（generate の別の枝）。
- 非ゴール: ロジアナの深いバッファの代わり（内蔵ロジアナは今のまま `la_capture`）。ストリームへのイベントの一本化（R1）。

## 3. 形

- 新しい部品 `rtl/la/event_ring.vhd`（ボード非依存）: generic `DEPTH_BITS`。口は `la_deep_buffer` のイベントと読み出しの
  部分と同じ名前・意味:
  - 書き: `ev_enable`・`ev_valid`・`ev_data(127:0)`。`ev_enable` の立ち上がりで書いた数・失った数を 0 に戻し、先頭から
    書く。1 周期に 1 件書けるので失うことは無い（`ev_lost` = 0。核の byte_lost は核が足す）。古いものから上書きする。
  - 数: `ev_written`（総数）・`ev_lost`・`ev_beats`（= 2**DEPTH_BITS、EVENTS_CAPACITY）。
  - 読み: `read`（1 周期）・`read_region`（'1' = イベント。'0' は内蔵ロジアナで別の窓なので、空の読みとして 0 拍で終える）・
    `read_first`（リングの中の位置）・`read_count`（件数）→ `out_valid/ready/data`（8 ビット、1 件 16 バイト、下位の
    バイトから。深いバッファのストリームと同じ）、`reading`。
- `fprobe_core` の generic `EVENT_RING_BITS : natural := 0`。`MEM_ADDR_BITS = 0` かつ `EVENT_RING_BITS > 0` のとき
  `event_ring` を置き、CSR の events・la_deep の読み出しの窓（`deep_read` 等）・`stream_out` をそれにつなぐ。
  それ以外は今と同じ（外部メモリ無しなら 0、外部メモリありなら `la_deep_buffer`）。
- 構成: ケイパビリティの生成パッケージに定数 `EVENT_RING_BITS` を出す（`Capabilities.event_ring_bits`。ROM には入れない。
  PC は EVENTS_CAPACITY を読む）。判断: `fprobe_pynq` と `fprobe_serial` は 10（1024 件。どちらもビルドで資源とタイミングを
  確かめた）。Nano 9K は 0（リング無しでも容量を超えていた。台帳 R13）。
- 記録を始める周期に来たイベントは 0 番に書く（書き番地を組み合わせで選ぶ。レビューの指摘）。
- リングが小さく、リンクが遅いと、PC が読み終える前に書き手が読み手を追い越し、要求より新しいイベントにすり替わる。
  PC（`EventLog.read`）は読み終えた後に書いた総数を読み直し、上書きされた先頭を捨てて `overwritten` に数える
  （`EventCursor` は lost に足す）。深いバッファにも効く汎用の扱い（レビューの指摘。PC 側の変更はこの 1 か所）。

## 4. 確かめ方

- cocotb: `event_ring` 単体（書いて・読む・折り返す・enable で戻る・読みの間も書ける）。`fprobe_core` の
  ハーネスに外部メモリ無し + EVENT_RING_BITS の構成を足し、サンプラのイベントが `EventLog` と同じ手順で読める。
- 実機（PYNQ、配線のまま）: XIAO の `position_state` をサンプラで読み、ロジアナで行の立ち上がりを取り、同じ時刻で
  「押してから変わるまで」を測る（2026-10-20 の粗い測定の置き換え）。
- Primer の構成のビルド（変わらないこと）。
