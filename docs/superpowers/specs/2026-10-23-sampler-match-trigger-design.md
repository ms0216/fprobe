# サンプラの値の一致をクロストリガの源に（R21）

2026-10-23。残務 R21「『ターゲットが止まった』を源にするクロストリガが無い（MCU のブレークポイント → ロジアナの捕捉の遅れを
測れない）」への答え。利用者の承認済みの 12 時間の作業の一覧の 4 番。ここに書いた決め事は私（Claude）の判断で、利用者は
帰ってから確かめる。

## 1. 何を作るか

「ターゲットが止まった」専用の源は作らない（CLAUDE.md: 特定のための機能を足さない）。代わりに汎用の源
**bit5 `sampler_match`** を足す: DAP サンプラ（F4。ターゲットを止めずにメモリを周期的に読む回路）が読んだ 1 語が、
決めた番地で「値の条件に入った」瞬間に 1 周期のパルスを出す。

- 「止まった」は、サンプラで DHCSR（0xE000EDF0）を読み、mask = S_HALT（bit17）・value = S_HALT にすれば得られる。
- ほかにも、変数がある値になった・フラグが立った、などを、ターゲットを改造せずに源にできる。

## 2. 振る舞い（事実だけ。推定をしない）

- 比べるのは、サンプラが返した語のうち、ACK が OK で、番地が `match_addr` に等しいもの（以下「標本」）。
- 一致 = `(値 xor match_value) and match_mask = 0`。
- 発火 = 前の標本が不一致で、今の標本が一致（入った瞬間）。enable の 0→1 の後の最初の標本では発火しない
  （その前の状態を見ていないので「入った」かどうかは分からない）。そのときの状態は `matched` で読める。
- 発火の時刻は「一致した標本が返った時刻」。実際に条件に入ったのは、前の標本（不一致）と今の標本の間のどこか。
  その区間を、前の標本の時刻 `match_prev_time` と今の時刻 `match_time` として、そのまま返す（間のどこかを推定しない）。
- `match_seen`: enable してから比べた標本の数。0 のままなら、その番地はサンプラが読んでいない（事実として見える）。
- `match_count`: 発火の回数。

## 3. レジスタ（独立した窓 sampler_match、0x0C40）

最初は CSR sampler に足したが、構成で外せるように独立した窓にした（2026-10-23、下の §6）。

| 番地 | 名前 | 中身 |
|---|---|---|
| 0x00 | addr | 比べる番地 |
| 0x04 | mask | 比べるビット |
| 0x08 | value | 比べる値 |
| 0x0C | control | bit0 enable（RW。0→1 で数と状態を消す）、bit8 matched（RO。最後の標本が一致か）、bit9 seen_any（RO） |
| 0x10 | seen | 比べた標本の数 |
| 0x14 | count | 発火の回数 |
| 0x18 / 0x1C | time_lo / hi | 最後に発火した標本の時刻（48 ビット。イベントの時刻と同じ幅） |
| 0x20 / 0x24 | prev_lo / hi | その直前の（不一致の）標本の時刻（48 ビット） |

- クロストリガの源を 6 ビットに（xtrig.route の to_la・to_inject、xtrig_out.when の sources）。
- ケイパビリティ FEATURES bit10 `SAMPLER_MATCH`（構成で入れたうえで、サンプラとクロストリガの両方がある構成）。

## 4. ホスト（API）

- 源の名前 `sampler_match`（xtrig_route・xtrig_out の源の一覧に足す）。
- `sampler_trigger_set(address, mask, value, halt, la)`: 条件を書いて、経路の sampler_match のビットだけを変える。
  halt・la の少なくとも一方が要る。既定の値は持たない（mask・value は必須）。
- `sampler_trigger_status()`: レジスタの事実（address・mask・value・enable・matched・seen・count・time・prev_time と、
  halt・la の経路）。
- `sampler_trigger_clear()`: enable を 0、経路の sampler_match を外す。
- サンプラを読ませるのは既存の道具（watch 等）。この API はサンプラの持ち主にならない（読んでいなければ seen = 0 が見える）。

## 5. 試験

- cocotb: 一致に入った瞬間だけ発火・最初の標本では発火しない・番地が違う／ACK が OK でない標本は数えない・
  prev_time と time が標本の時刻・enable の 0→1 で消える。実装を壊すと落ちることを確かめる。
- 実機: R21 を持つ構成（PYNQ）+ SWD のターゲット（XIAO 等）。Primer の構成は持たない（§6）。サンプラで DHCSR を読み、ブレークポイントで止めて、発火したことと
  [prev_time, time] の区間、ロジアナの捕捉ができることを確かめる。

## 6. Primer では外す（2026-10-23）

- Primer の USB の構成 fprobe に入れると、3 つの配置のどれも配線が 1200 秒で終わらなかった。配線の設定を
  route_option 0 にすると配線はできたが、3 つともタイミングを満たさなかった。
- そこで構成ごとに入れる・外すを選べるようにし（fprobe_core の generic SAMPLER_MATCH、ケイパビリティ）、Primer の構成
  （USB・Ethernet・シリアル）では外した。PYNQ の構成は持つ。PC 側は FEATURES bit10 を見て使う。
