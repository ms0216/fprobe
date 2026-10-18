# 部品 A〜D（DWARF・逆アセンブル/SVD・通信の解読・RTT/ITM/セミホスティング）の API・MCP への組み込み 計画

仕様: 各部品の仕様書の最後の節（本流での API・MCP への組み込みの提案）。
- docs/superpowers/specs/2026-10-17-f2-dwarf-design.md
- docs/superpowers/specs/2026-10-17-f2-disasm-svd-design.md
- docs/superpowers/specs/2026-10-17-f7-protocol-decode-design.md
- docs/superpowers/specs/2026-10-17-f1-rtt-itm-semihosting-design.md

## 形

- `Probe`（`fprobe/mcp_server.py`）は肥大しているので、新しい道具は 2 つの mixin に分ける。どちらも `Probe` の
  `_lock`・`target`・`dbg`・`_symbols`・`_recorder`・`_capture` を使う（単独では使わない）。
  - `fprobe/probe_inspect.py` `InspectTools`: 止まったターゲットを調べる（式・フレームの変数・型・逆アセンブル・SVD・
    周辺レジスタ・メモリの表示）。
  - `fprobe/probe_observe.py` `ObserveTools`: 止めずに見る（RTT・SWO の ITM 解読・セミホスティング）と捕捉の解読
    （`la.decode`）。背景のスレッド 1 本（RTT の poll と、再開後のセミホスティングの見張り）。受信は `ByteLog`
    （位置付きのバイトの貯め場。既定の位置と、画面用の明示の位置 since）。
- `debug.py`: DWARF の索引を ELF のパス・更新時刻で共有（Debugger を作り直しても読み直さない。ELF を読み替えると
  古い DWARF を使っていた不具合も消える）。`backtrace(inspector=)` で巻き戻しの結果を変数の読みと共有。
  `run_until_halt(lock=)` で待つ間はロックを放す（背景の RTT が止まらない）。
- 仕様の引数に `"x-mcp": false` を付けると MCP の道具に出さない（セミホスティングのファイルの root）。

## API（判断の基準: 機器に触らない読み = read、止まったターゲットを読む = control + observes、画面の頻繁な読み = record: false）

| メソッド | 種類 |
|---|---|
| debug.backtrace（variables・max_frames・max_elements を追加。結果の既存の欄は同じ） | control / observes |
| debug.frame_variables / debug.evaluate | control / observes |
| debug.write_expression | control |
| debug.type_info | read |
| debug.disasm.range / function / around / text | control / observes |
| target.svd.load | control（機器に触らないが状態を変える） |
| target.svd.peripherals / target.svd.registers | read |
| target.peripheral.read | control / observes |
| target.peripheral.write | control |
| target.memory.view | control / observes |
| la.decode | read |
| rtt.start / rtt.write / rtt.stop | control |
| rtt.read / rtt.status | read（機器に触らない。背景が貯めたものを返す） |
| swo.decode / swo.text | control（機器の SWO を読む） |
| debug.semihosting.configure | control（file_root・writable は MCP に出さない） |
| debug.semihosting.status | read |

## 手順（TDD）

1. debug.py の変更（DWARF の共有・backtrace の inspector・run_until_halt の lock）+ 試験。
2. InspectTools + 試験（dwarf_demo / disasm_demo / sram_demo の ELF、mini.svd、偽のターゲット）。
3. ObserveTools: ByteLog、RTT（rtt_demo の ELF を偽のメモリに置く）、SWO（偽の probe.swo_read）、
   セミホスティング（BKPT 0xAB の偽のターゲット）、la.decode（synth の波形）+ 記録への出来事。
4. API の仕様・MCP（x-mcp）・INSTRUCTIONS・gen・README。
5. `pytest host` 全体。
