# 画面が呼ばない API の分類（下書き、2026-10-22）

API 305 のうち画面（host/ui/src）が呼ぶのは 159。残り 146 を、画面のコードを読んで分けた下書き（調査のエージェント。
コードは変えていない）。残務 R23。どこまで画面に入れるかは利用者が決める。

- A「別の手段で画面にある」: 28（例: target.memory.read → Memory の target.memory.view、recording.events → EventLog の
  recording.query・recording.find、rtos.timeline.inversions・events・blocking → Timeline の行）
- B「画面に無く、人が使う画面に要る」: 69
- C「AI・スクリプト用で画面には不要」: 42（probe.csr.*・jtag.shift 等の生の操作、xtrig の細かい設定、rules・inject の台本、
  stream.serve、testpoint.instrument 等）
- 判断できない: 7（hooks.add / remove、rules.states、trace.start / stop / status / events）

注意: 名前の抜き出しは最初 timing.* を漏らしていた（後で直した）。A のうち時間軸の行を根拠にしたものは、表示の名前が
lib/timeline/titles.ts と i18n にあることだけで判断している（行を作る処理は読んでいない）。

## B のうち、人の使い勝手への効き目が大きい順の上位 10（下書きの判断）

1. target.diagnose（と probe.pins.read）: 接続の失敗の理由を接続ダイアログで見せる
2. snapshot.save / list / open / restore: 故障の状態の保存・後で見る（Fault・Recordings）
3. la.export: 捕捉を PulseView / VCD で保存（LogicAnalyzer に保存のボタンが無い）
4. target.option_bytes.read / write と target.unlock: 保護のかかった部品の出口（Flash）
5. trace.etm.history（と etm.start / stop）: 故障の直前の命令の履歴（Fault）
6. heap.scan と heap.trace.*: ヒープの使用量・二重の free（置き場所が無い）
7. cycles.measure（と cycles.read）: 2 点の間のサイクル数（Source・Disassembly）
8. target.stack.paint / usage と rtos.timeline.memory: スタックのあふれ（Threads）
9. log.start / read（defmt / trice）と log.annotate: Terminal の源に
10. target.dp.* / target.ap.*: DAP の直接の読み書き

中くらい: テストポイント一式（始める操作と集計の表示が無い）、target.core.select、debug.elf.size、probe.design.switch、
scvd.view、rtos.timeline.queues / flow。

## B の全体（69）

target.diagnose・probe.pins.read・probe.design.get / switch・probe.name.set・target.core.select・target.dp.read / write・
target.ap.read / write・target.option_bytes.read / write・target.unlock・jtag.scan・la.export・la.decoder_load・rtos.read・
rtos.timeline.queues / flow / jobs / memory・cycles.measure / read・testpoint.start / stop / status / report / calls / coverage・
gpio.read / set / release・uart.flow / lines・user.trace.start / stop / stats・snapshot.save / list / open / close / info / restore・
heap.scan・heap.trace.start / stop / report・watch.buffer_start / read / stop・log.start / stop / read / status・log.annotate・
trace.import / snapshot・trace.etm.start / stop / instructions / history・scvd.load / view・debug.elf.size・
target.stack.paint / usage・profile.export・hooks.list / log
