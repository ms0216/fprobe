# F7（一部）通信の解読 実装計画

仕様: [2026-10-17-f7-protocol-decode-design.md](../specs/2026-10-17-f7-protocol-decode-design.md)

試験は `cd host && ../.venv/Scripts/python -m pytest -q -p no:cacheprovider`（worktree では venv の `fprobe` が本流を指すので、
`host` をカレントにして worktree の `fprobe` を読ませる）。各段は試験を先に書く（TDD）。

| 段 | 内容 | ファイル | commit |
|---|---|---|---|
| 1 | 仕様書・計画 | `docs/superpowers/specs/…`, `docs/superpowers/plans/…` | docs |
| 2 | 共通: `Annotation`・`Decoding`（秒・トリガ基準・JSON・絞り込み・区間）、`project`、`ChannelLevels`。発生器の土台 `Wave` | `decode/core.py`, `decode/synth.py`, `tests/test_decode_core.py` | feat |
| 3 | UART: 解読・自動推定・発生器。往復（データ長・パリティ・停止ビット・反転・ビット順）、誤り（パリティ・フレーム・ブレーク）、途中から・途中まで、4 倍のオーバーサンプリング、揺れ、速さ | `decode/uart.py`, `tests/test_decode_uart.py` | feat |
| 4 | SPI: モード 0〜3、ビット順、語長、CS の有無・極性、途中の CS、低いオーバーサンプリング | `decode/spi.py`, `tests/test_decode_spi.py` | feat |
| 5 | I2C: START/Sr/STOP、7/10 ビット、ACK/NACK、引き伸ばし、途中から | `decode/i2c.py`, `tests/test_decode_i2c.py` | feat |
| 6 | SWD: ビットの取り出し、並び（リセット・切り替え・ドーマント）、パケット、ACK、パリティ、状態の追跡（SELECT・ORUNDETECT・DLCR・先送り）、レジスタ名、TARGETSEL、ch8 の方向、速さ | `decode/swd.py`, `tests/test_decode_swd.py` | feat |
| 7 | `decode/__init__.py` の `DECODERS`・`decode()`、全試験、仕様書の訂正 | | feat/docs |
