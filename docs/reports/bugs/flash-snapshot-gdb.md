# 不具合台帳: Flash 書き込み・Flash ブレークポイント・スナップショット・GDB サーバ・RTT・セミホスティング

検出専用（製品コードは変更していない）。再現試験は `host/tests/` の `test_*_bugs.py`（`xfail(strict=True)`）。
重さ: A=静かに誤った結果 / B=失敗が見える / C=端の条件・まれ / D=品質。
手法: 精読、偽の機器（FakeChip・ThumbChip・FakeCore）での再現、ランダム操作列の突き合わせ
（Flash の書き込み 600 通り・patch の状態保存 200 通り・RTT のリング 300 通りは差異なし）。

## 件数

A: 3 / B: 7 / C: 6 / D: 3（計 19）。ほかに「疑い」15 件（再現試験なし）。

## 不具合

### A（静かに誤った結果）

- **BUG-GDB-001** `gdb_flash`（GDB の load = vFlashDone）が Flash ブレークポイントの記録を合わせない。
  試験: `test_gdb_bugs.py::test_bug_gdb_flash_does_not_fold_or_forget_flash_breakpoints`。
  原因: `probe_gdb.py:384-387` が `MemoryLoader` で直接書く。`flash()` は `_flash_bp_program`（fold と forget）を通すが、
  こちらは通さない。書いたセクタの BKPT が `_written` に残り、(1) 読み（`flash_bp.py:375 _read`）が古い元の命令を新しい中身の
  上に重ねて見せる（逆アセンブル・検証・巻き戻しが古い命令を読む）、(2) 置いたブレークポイントは「書いてある」扱いで次の再開で
  書き直されず、その番地で止まらない、(3) セクタの他の BKPT の元の中身が消去で失われる（fold が無い）。
  直し方の案: `gdb_flash` も `_flash_bp_program` に相当する経路（FlashMemory.program に Image を渡し fold・forget）を通す。
  影響: GDB の `load` の後、ブレークポイントが効かない・読みが古い。確信度: 高（試験で読みが古い命令を返すのを確認）。
  修正済み: `gdb_flash` も `fold` / `forget` を通す（flash() と同じ経路）。
- **BUG-FLASH-006** `flash_read(address が Flash の終わりより先)` が誤りでなく 0 バイトの成功（空ファイル・CRC-32 0）。
  試験: `test_flash_bugs.py::test_bug_flash_read_past_the_end_is_an_empty_success`。
  原因: `probe_flash.py:157-160` が `length = main.end - address`（負になる）を検めず、`flash.py:462` の `while len(out) < length`
  が回らず空を返す。直し方の案: length <= 0 は ValueError（`FlashMemory.read` で検める。BUG-FLASH-005 と同根）。
  影響: 読み出して保存したつもりの空ファイルを AI が正しいと信じる。確信度: 高。
  修正済み: `flash_read` は length が 0 以下なら ValueError。
- **BUG-GDB-003** 種類の違うウォッチポイント（Z2 と Z3 等）を同じ番地に置くと、最初の種類だけが有効になる。
  試験: `test_gdb_bugs.py::test_bug_gdb_watchpoints_of_different_kinds_on_same_address`。
  原因: `probe_gdb.py:210-216 _gdb_watch` が番地だけで表を引く（種類・長さを見ない）。後の種類は持ち主に足すだけで設定しない。
  外すとき（`_gdb_unwatch`）も番地単位で、片方を外すと両方消える。直し方の案: 表の鍵を（番地・種類・長さ）にする。
  影響: `rwatch` を足したのに読みで止まらない。確信度: 高（試験で kind が write のままを確認）。
  修正済み: ウォッチポイントは番地ごとに持ち主・（種類, 長さ）を覚え、読みと書きの重なりは access・長い方の長さで置き、外すときは残りの求めで置き直す。

### B（失敗が見える）

- **BUG-FLASH-001** 守り（RegisterGuard）の `enter` が失敗すると、PRIMASK=1 のまま・停止したまま戻らない。
  試験: `test_flash_bugs.py::test_bug_flash_guard_enter_failure_leaves_primask_set`。
  原因: `flash.py:374 entered = [...]` は try の外。ここで例外が出ると 371-373 で止めて PRIMASK=1 にした状態が戻らず、
  先に enter できた守りの exit も呼ばれない。`preserving`（`flash.py:658-659`）の `guard.__enter__()` も try の外。
  直し方の案: 割り込みの停止から全体を try/finally に入れる。影響: 走っていたプログラムが割り込み禁止で止まったまま。確信度: 高。
  修正済み: `_clock_guarded` は PRIMASK を変えた直後から try/finally に入れ、守りの enter が失敗しても入れた守りの exit と PRIMASK の復元をし、exit の失敗で残りを飛ばさない。`preserving` も保存・守りの開始が失敗したら走らせ直す（S14 も）。
- **BUG-FLASH-004** 先頭が `:`（0x3A）の BIN を Intel HEX と誤認し、`Image.load` が HexRecordError。
  試験: `test_flash_bugs.py::test_bug_bin_starting_with_colon_is_taken_for_hex`。
  原因: `flash.py:184` の中身の先頭 1 バイトだけの判定。BIN は ARM 以外・ブートローダ等で先頭が 0x3A になりうる。
  直し方の案: 利用者が format を明示できる（`flash(path, format=)`）ようにする、または HEX は最初の行全体の形で判定。
  影響: 特定の BIN が書けない（明示の手段が無い）。確信度: 高。
- **BUG-FBP-001** 壊れた記録（Journal の JSON が不正）で `recover` が例外を投げ、`target_connect` が毎回失敗する。
  試験: `test_flash_bp_bugs.py::test_bug_corrupt_journal_makes_recover_raise`。
  原因: `flash_bp.py:91 Journal.load` が JSONDecodeError・形の誤り（`data.get` が dict でない等）を捕まえない。
  `probe_flash_bp.py:72 _flash_bp_attach` も包まない。直し方の案: 壊れた記録は退避（.bad に改名）して「読めなかった」を返す。
  影響: 利用者が記録ファイルを手で消すまで接続できない。確信度: 高。
  修正済み: 壊れた Journal は `JournalError`。`recover` は .bad に退避し、結果に `unreadable: {journal, reason}` を出す（Flash に残った BKPT は戻せていないことの明示）。
- **BUG-SNAP-001** registers が壊れた・無い .fsnap が `SnapshotError` でなく ValueError / KeyError を漏らす。
  試験: `test_snapshot_rtt_semihosting_bugs.py::test_bug_snapshot_load_leaks_raw_exceptions_for_bad_registers`。
  原因: `snapshot.py:190` の `manifest["registers"]` と `int(v)` が try（188）の外。直し方の案: try の中へ移す。確信度: 高。
  修正済み: registers の読みを try の中へ。
- **BUG-SNAP-002** manifest が JSON の配列等だと AttributeError。試験: `test_bug_snapshot_load_non_object_manifest`。
  原因: `snapshot.py:177` の `manifest.get` の前に型を検めない。確信度: 高。
  修正済み: manifest が dict でなければ SnapshotError。
- **BUG-RTT-001** チャンネル名の文字列がメモリの終わり近くにあると、`open()` が NAME_LIMIT(32) 固定の読みでメモリの外を読み失敗する。
  試験: `test_bug_rtt_open_fails_when_name_is_at_the_end_of_memory`。
  原因: `rtt.py:276 _read_name` が常に 32 バイト読む。名前は ID と違い RAM の末尾・Flash の末尾に置かれうる。
  直し方の案: 読みが失敗したら短くして読み直す、または名前を None にして続ける。影響: RTT が開けない。確信度: 中（実機の pyOCD で
  領域の外の読みがバスエラーになる前提）。
- **BUG-SEMI-001** SYS_WRITE0 が 64 バイト単位で読むため、文字列がメモリの終わり近くにあると読みが越えて失敗する。 **修正済み（RUNCTL-13 と同じ原因）: 読みが失敗したら半分ずつ縮めて読み直す。**
  試験: `test_bug_semihosting_write0_near_the_end_of_memory`。
  原因: `semihosting.py:271` の `read_memory(r1 + len(text), 64)`。直し方の案: 読みを 64 に揃えた境界（アドレスを 64 に
  切り上げ）にするか、失敗したら 1 バイトずつ。影響: 出力が出ずターゲットが BKPT のまま。確信度: 中。
  修正済み: SYS_WRITE0 は 64 バイトの境界までだけ読む。

### C（端の条件・まれ）

- **BUG-FLASH-002** 変更が空の `patch([])`（preserve 既定）が IndexError。試験: `test_bug_patch_with_no_changes_raises_index_error`。
  原因: `flash.py:665` の `spans[0][0]` で、addresses が空なら spans が空。ターゲットは止まったまま（`preserving` の途中で落ちる）。
- **BUG-FLASH-005** 長さが負の `FlashMemory.read` が空を成功として返す（BUG-FLASH-006 の根）。`flash.py:462`。
  修正済み: `FlashMemory.read` は負の長さを ValueError。
- **BUG-GDB-002** Z0 と Z1 を同じ番地に置き片方を外すと、もう一方も消える。`probe_gdb.py:182-208`（番地→持ち主の表が種類を持たない）。
- **BUG-FBP-002** Flash に置いた番地へ HW（Z1）を重ねて求めると `_hw` に記録されず（`flash_bp.py:182` が target へ直接渡す）、
  `remove_breakpoint` を 1 回呼んでも比較器が残る（2 回目で target に流れて消える）。
- **BUG-SNAP-003** コアファイルの PT_LOAD が `p_offset ≡ p_vaddr (mod p_align)` を満たさない（4 の倍数でない番地の区画、
  `snapshot.py:297-301`）。GDB が警告・拒否する可能性（確かめていない）。
- **BUG-GDB-004** RSP の受信が `$` で読み直さない（`rsp.py:79` は本文の途中の `$` を本文に入れる）。途中で切れたパケットの
  次のパケットまで CORRUPT として失われ、再送で回復するが NAK が 1 つ多い。

### D（品質）

- **BUG-FLASH-003** 進捗の終わりが間引かれうる: `flash.py:300` が丸め前の fraction が 1.0 でない（0.9999999999999999）ため
  interval 内なら捨てる。`program()` の `offset + f * share` は複数の領域で 1.0 に届かないことがある。「終わりは必ず送る」を破る。
  直し方の案: 丸めた後の値で判定。
- **BUG-GDB-005** 部分的に重なる区画の後ろの部分の種類が失われる（`description.py:81-83`、重なる区画を丸ごと捨てる）。pyOCD の
  実際のメモリマップでは起きにくい。
- **BUG-GDB-006** 末尾が `}` で終わる不完全な逃がしの `unescape` が NUL を捏造（`rsp.py:50`）。

## 疑い（再現試験なし）

- S1（修正済み: 書く・戻すを `_flash_bp_program` 経由に。試験 `test_flash_benchmark_keeps_flash_breakpoint_ledger_consistent`）`flash_benchmark(program_address=)`（`probe_flash.py:256-275 _bench_program`）が `_flash_bp_program` を通さない。
  そのセクタに Flash BKPT があると、元の中身（BKPT を隠して読む）で書き戻されて BKPT が消え、`_written` だけが残る（A になりうる）。
- S2 `flash()` が途中で例外のとき `_watch_stops(False)`（`probe_flash.py:93`）を戻さない（見張りが止まったまま）。
- S3 `_clock_guarded` の finally（`flash.py:380-385`）で守りの exit が例外を出すと、残りの守りの exit と PRIMASK の復元が飛ぶ。
- S4 `snapshot_open`（`probe_snapshot.py:330-335`）が `Symbols(elf_path)` で失敗すると、スナップショットは開いたが `_symbols` は古いまま。
- S5 `SnapshotAccess.read_register` が `r14` / `r15` を受けない（`lr` / `pc` のみ。`r13` だけ別名）。今の呼び手は名前で呼ぶので顕在化していない。
- S6 `Rtt.poll` が初期化し直しを見て `open()` を呼ぶと、チャンネルの累計（transferred・resyncs）が 0 に戻り `stats()` が減る。
- S7 `Rtt.poll(channels=[0, 0])` はチャンネルを 2 度数え、transferred が二重になる。
- S8 `GdbSession._xfer` が未対応の object に `E00` を返す（空の応答が約束）。広告していないので GDB は問い合わせない。
- S9 `FlashBreakpoints.reset()` が `_stepped` を戻さず、次の停止の再開が休眠 BKPT の「当たり」として数えられる（数え方だけの差）。
- S10 `vCont;s:1;c:2` のようなスレッド別の動作を、いずれかが `s` なら全体のステップとして扱う（`session.py:235`）。all-stop では許容。
- S11 セミホスティングのファイル書き込みは close まで flush されない（SYS_EXIT で閉じずに終えると失われる。`close()` を呼ぶ側の責務）。
- S12 `snapshot_save(ranges=重なる)` はすべて読んだ後に `SnapshotError`（`snapshot.py:122`）。早く検めるべき。
- S13 `PacketSize=0x4000` を申告し、`m` の応答が最大で 0x4000 文字（`session.py:396`）。GDB が PacketSize を枠（`$`・`#xx`）込みで
  数えるなら 3 バイト溢れる。実機の GDB で確かめていない。
- S14（修正済み、BUG-FLASH-001 と同時）`preserving()` の保存の途中（`flash.py:652-659`）で失敗すると、走っていたターゲットを止めたまま戻さない（BUG-FLASH-001 と同種）。
- S15 `snapshot_restore` が `_check_code` で失敗したとき、ターゲットを止めて見張りも止めたまま戻さない（`probe_snapshot.py:437-440`）。

## 調べた範囲 / 調べていない範囲

調べた: `flash.py`（全体）、`flash_bp.py`（全体）、`probe_flash.py`、`probe_flash_bp.py`、`snapshot.py`、`probe_snapshot.py`、
`gdb/rsp.py`、`gdb/session.py`、`gdb/description.py`、`probe_gdb.py`、`rtt.py`、`semihosting.py`、`memory.py`（外部メモリの試験）。
調べていない: `gdb/agent.py`・`gdb/client.py`・`gdb/server.py`・`gdb/trace.py`・`probe_gdb_trace.py`（トレースポイント）、`memview.py`、
RTT のホスト側の連携（`probe_*` の rtt 呼び出し）・SEGGER RTT の下りの競合、`thumb_step.py` の命令の模擬、`packs.py`、
`stm32_option_bytes.py`。pyOCD の実際の FlashBuilder と偽の書き込みアルゴリズムの外（実チップのタイミング・保護）。実機・合成は触れていない。

未修正（別の担当が rtt.py を触るため）: BUG-RTT-001。
