# F7 テストポイント 実装計画

仕様: `docs/superpowers/specs/2026-10-17-f7-testpoint-design.md`。TDD（試験を先に書き、通してから commit）。
試験: `cd host; PYTHONPATH=. ..\.venv\Scripts\python -m pytest -q -p no:cacheprovider tests/test_testpoint_*.py`。

| # | 作業 | 試験 | commit |
|---|---|---|---|
| 1 | 仕様書・計画 | — | docs |
| 2 | `clex.py`: 字句（コメント・文字列・接頭辞・継続・前処理の指令・区切り記号）。位置と行 | `test_testpoint_clex.py` | 1 |
| 3 | `cparse.py`: 関数の定義の発見（属性・複雑な宣言子・extern "C"・初期化子・K&R の除外）、文の木（if/else・switch・ループ・return・ラベル・マクロの塊・式文）、前処理の条件の位置の検査 | `test_testpoint_cparse.py` | 2 |
| 4 | `probes.py`（Probe・ProbeTable・JSON）、`instrument.py`（挿入の編集・包み・暗黙の else / default・return の書き換え・変数・ID・指紋・差分・写しの出力）、`fprobe_tp.h`、CLI（`python -m fprobe.testpoint`） | `test_testpoint_instrument.py`: 対応表・写しの形・zig cc -fsyntax-only（全方式）・ホストで実行して ID の列が実行の順と一致（再帰・goto・早期 return・fallthrough）・差分の ID の保持 | 3 |
| 5 | `synth.py`（Hit → 並列・2 線の波形、ITM のバイト列、RTT のデータ）、`decode.py`（4 つの解読器、かたまりの境目、Loss） | `test_testpoint_decode.py`: 往復、捕捉の途中から、ずれ、壊れた記録、回り込み | 4 |
| 6 | `analysis.py`（Analyzer・統計・木・2 点間・カバレッジ・要約・異常・render_calls・to_dict）、`record.py` | `test_testpoint_analysis.py`（記録への書き出しを含む） | 5 |
| 7 | 試験用ファーム（src/demo.c・build.py・link.ld・3 つの ELF・testpoints.json）。命令数を数えて仕様書の負荷の表を更新 | `test_testpoint_demo.py`: ELF の存在・対応表と写しの一致・ホストで同じソースを実行して解析 | 6 |
| 8 | 全試験を通す・報告 | `pytest` 全体 | — |
