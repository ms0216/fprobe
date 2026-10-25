# 不具合台帳: Linux 系（host/fprobe/probe_linux.py と host/fprobe/linux/*）

検出専用（製品コードは変更していない）。再現試験は `host/tests/test_linux_bugs.py`（すべて `xfail(strict=True)`）。
重さ: A = 静かに誤った結果を返す / B = 失敗が見える / C = 端の条件・まれ / D = 品質。

## 再現済み

### BUG-LINUX-01（A）未定義シンボルが ELF ヘッダ（offset 0）に解決される
- 試験: `test_undefined_symbol_is_not_resolved_to_elf_header`
- 原因: `host/fprobe/linux/uprobe.py` の `site_of`（symbol 分岐、約 L58-L70）。`.symtab`/`.dynsym` の UNDEF シンボル（`st_shndx == SHN_UNDEF`、`st_value == 0`）を検査せず、vaddr 0 を採る。PIE は最初の PT_LOAD が vaddr 0 から始まるので区画に入り、offset 0 が成功として返る。
- 直し方の案: `st_shndx == "SHN_UNDEF"`（と `STT_FUNC` でない・サイズ 0）のシンボルは「インポートで場所が無い」と ValueError で断る。
- 影響: 例 `symbol="nanosleep@GLIBC_2.4"`（.symtab の名前はバージョンつき）で uprobe/ptrace が「付いた」ように返るが ELF ヘッダに付き、決して発火しない。ptrace なら `\x7fELF` を命令として読み、BKPT を ELF ヘッダのページに置く。
- 確信度: 再現済み（tick_arm の実バイナリ）。

### BUG-LINUX-02（C）プローブ名の検査が末尾の改行を通す
- 試験: `test_probe_name_rejects_trailing_newline`
- 原因: `uprobe.py` L12 `NAME = ^...$` を `.match` で使う。Python の `$` は末尾の改行の前にも合う。`fullmatch` でも `\Z` でもない。
- 直し方の案: `NAME.fullmatch`（または `\Z`）にする。`_PERF_EVENT`・`FTRACE_EVENT` は fullmatch で安全。
- 影響: `name="a\n"` が通り、`probe_add` のシェル命令 `echo 1 > .../fprobe/a\n/enable`（引用なし）が 2 つの命令に割れる。任意注入ではないが検査の穴。
- 確信度: 再現済み。

### BUG-LINUX-03（A）perf script の行の dso の貪欲な正規表現でシンボルを取り違える
- 試験: `test_perf_sample_symbol_with_space_paren`
- 原因: `host/fprobe/linux/perf.py` L12-13 `_SAMPLE` の `(?P<sym>.*?)\s+\((?P<dso>.*)\)\s*$`。sym が最初の「空白 + `(`」で切れ、dso は貪欲に最後の `)` まで取る。デマングルされた C++ の名前に ` (` が含まれる（`std::function<void ()>::operator()() const` 等）と、sym = `std::function<void`、dso = `)>::operator()() const (/usr/bin/app` になる。
- 直し方の案: 行末の最後の ` (` で右から切る（`rsplit`）か、dso を `[^()]*`（+ `(deleted)` 等の特例を事実として扱う）にする。
- 影響: 行は `unparsed` にならず、probe 名（= sym）と dso が誤ったまま出来事として記録される（静かに誤る）。
- 確信度: 再現済み。

### BUG-LINUX-04（C）linux.log.read は新しい行が無いとカーソルを失う
- 試験: `test_log_read_keeps_cursor_when_nothing_new`
- 原因: `host/fprobe/probe_linux.py` L409 `next_cursor = entries[-1].get("__CURSOR") if entries else None`。
- 直し方の案: 行が無いときは渡された `since_cursor` をそのまま返す（事実: 「ここまで読んだ」は変わらない）。
- 影響: `since_cursor=next_cursor` で続けて読む呼び手が、何も来ない間に None を渡し、次は journal の末尾 N 行を読み直して重複する。
- 確信度: 再現済み（偽の ssh で）。

### BUG-LINUX-05（C）linux.gdb.batch が timeout_s を検査しない
- 試験: `test_gdb_batch_rejects_nonpositive_timeout`（0・-1・nan）
- 原因: `probe_linux.py` L566 `timeout -s TERM {timeout_s:g}`。GNU timeout は 0 を「無制限」とする。ssh 側の待ちは `timeout_s + BATCH_BACKSTOP_S` なので、PC が先に切れる。perf の各 API は範囲を検査するのにここだけ無い。
- 直し方の案: `0 < timeout_s <= 上限`（有限）を ValueError で断る。
- 影響: timeout_s=0 だと PC の ssh だけが 10 秒で切れ、ターゲットの gdb が attach したままプロセスを止め続ける（コメントが避けると言っている状況そのもの）。
- 確信度: 再現済み（検査が無いこと）。実機での gdb 残りは未確認。

### BUG-LINUX-06（C、セキュリティ寄り）SshTarget の user が `-` で始まるとオプション扱い
- 試験: `test_ssh_user_cannot_become_option`
- 原因: `host/fprobe/linux/ssh.py` `argv` が `f"{user}@{host}"` を `--` なしで渡す。`user="-oProxyCommand=..."` なら `-oProxyCommand=...@host` が ssh のオプションとして解釈される（ProxyCommand は PC 側でコマンドを実行する）。`host`・`key` も検証が無い。
- 直し方の案: 目的地の前に `--` を置く、または user/host を `[A-Za-z0-9._-]` かつ先頭が `-` でないものに限る。
- 影響: MCP/AI が `linux.connect` に渡す値で PC 側のコマンドが走りうる（呼び手が既にターゲットで命令を走らせられる前提でも、PC 側への越権）。
- 確信度: 再現済み（argv の形）。実際に ProxyCommand が走るかは未確認。

### BUG-LINUX-07（C）uprobe の追加が途中で失敗するとカーネルに行が残る
- 試験: `test_probe_add_uprobe_failure_does_not_leak_kernel_event`
- 原因: `probe_linux.py` L253 `echo line >> uprobe_events && echo 1 > .../enable` を 1 命令で。前半が成功し後半が失敗すると RuntimeError で抜け、`t.probes[name]` は登録されない。
- 直し方の案: 失敗時に `echo -:fprobe/name >> uprobe_events` を試みる（または先に probes に入れて remove できるようにする）。
- 影響: 以後 `linux.probe.remove` で消せず、同じ名前の `probe_add` はカーネルが「File exists」で断る。`disconnect` も残す。
- 確信度: 再現済み（命令の記録で）。

### BUG-LINUX-08（C）binary の `..` が sysroot の外へ出る
- 試験: `test_probe_add_binary_cannot_escape_sysroot`
- 原因: `probe_linux.py` L241 `_home()/target/"sysroot"/binary.lstrip("/")`。正規化も `..` の拒否も無い。`target` の名前も同じ（パス区切りを含められる）。
- 直し方の案: 解決後のパスが sysroot の下にあることを確かめて断る。
- 影響: `binary="/../../x"` で `~/.fprobe/linux/x` に scp が書く（既存ファイルの上書き）。
- 確信度: 再現済み。

### BUG-LINUX-09（C）Agent.start が失敗すると ssh の子プロセスを残す
- 試験: `test_agent_start_failure_does_not_leak_process`
- 原因: `host/fprobe/linux/agent.py` `start`: `self.request("hello")` の RuntimeError（応答なし・非対応）で `stop()` を呼ばない（watchdog 失敗側だけ `self.stop()` する）。読みのスレッドと ssh のプロセスが残り、`linux_agent_start` は `t.agent` に代入済みなので次の start でも古い Agent を止めずに上書きする。
- 直し方の案: hello の失敗でも `self.stop()` してから再送出（try/except で包む）。
- 影響: ターゲットでエージェントが動いたまま（watchdog 無し）残りうる。
- 確信度: 再現済み。

### BUG-LINUX-10（D）gdb.serve の port=0
- 試験: `test_gdb_relay_port_zero_reports_real_port`
- 原因: `gdb_relay.py` は `bind(("127.0.0.1", 0))` で OS 任せの番号を得るが、`self.port` は 0 のまま。`linux_gdb_serve` は `connect: target remote 127.0.0.1:0` を返す。
- 直し方の案: bind 後に `getsockname()[1]` を `self.port` にする（または 0 を断る）。
- 影響: 使えない接続先が返る。
- 確信度: 再現済み。

### BUG-LINUX-11（D）trace の時刻の小数部が 10 桁以上で t_ns が float
- 試験: `test_trace_line_ns_is_int_for_long_fraction`
- 原因: `uprobe.py` `parse_trace_line` の `int(frac) * 10 ** (9 - len(frac))`（len > 9 で負の指数 = float）。
- 直し方の案: 9 桁を超える分を整数除算で切る（または分解能として事実を返す）。
- 影響: 現実の trace_clock は 6 桁なので、まれ。float の ns は以後の整数前提（比較・丸め）を壊しうる。
- 確信度: 再現済み。

### BUG-LINUX-12（D）最後の組ちょうどの出来事が outside=true
- 試験: `test_clock_event_at_last_pair_is_not_outside`
- 原因: `host/fprobe/linux/clock.py` `to_host`: `bisect_right` で `i == len(p)` になり `outside = True`。`_linux_flush` は `target_ns <= last` で「後ろに組がある」とみなすので、最後の組と同時刻の出来事は outside=true の注記つきで記録される。
- 直し方の案: 端の組と一致する時刻は outside にしない（`target_ns > p[-1][0]` のときだけ）。
- 影響: 事実の注記が実際と違う（値自体は正しい）。
- 確信度: 再現済み。

## 疑い（再現試験なし）

- S-01（C）`RingReader.take`: `head < next_seq`（エージェントが同じ capacity・first のままリングを初期化し直した等）のとき、`first = max(next_seq, head - capacity)` が next_seq のままで count が負になり、何も返さず lost・torn・header_error のいずれにも出ない。以後 head が next_seq を超えるまで無言。`ring.py` L70-76。head の後退は事実として返すのが望ましい。
- S-02（C）`RingReader.take` の `slots` が要求より短いバイト列を返すと `struct.error`、空を返すと `while len(raw) < count * RECORD` が無限ループ（`ring.py` L80-83）。memap/CPU 側の読みが短い場合。
- S-03（C）`Agent._read_out`: 返事の `id` が JSON のリストなど hashable でない値だと `self._pending.pop` が TypeError で読みのスレッドが死ぬ（以後すべての要求が 10 秒待ち）。`agent.py` L96-97。
- S-04（C）`Agent.request`: 要求を登録した後にエージェントが終わり、`_read_out` が pending を入れ替えた後に登録した要求は誰も起こさず、timeout（10 秒）まで待つ。
- S-05（C）`linux_disconnect`: `t.gdb.stop()` や `t.memap.close()` が例外を出すと、後続の片づけ（ftrace 無効・プローブ除去・エージェント停止）と `del self._linux[target]` が行われない。`probe_linux.py` L158-180。
- S-06（C）`linux_trace_stop`: `trace_close` が RuntimeError だと同期の組を置かず pending も flush しないまま抜ける（`L748-751`）。
- S-07（C）`linux_trace_start` を 2 回呼ぶと `trace_open` を 2 回送る（エージェント側の挙動次第）。trace_clock（mono_raw）・tracing_on は disconnect で戻さない（ターゲットの他の利用者に影響）。
- S-08（D）`site_of`: 同名シンボルが複数ある（static 関数）と最初のものを黙って採る。`.symtab` があり `.dynsym` にだけ名前があるとき見つからない。address 指定で非 ARM に `isa="thumb"` を渡すと `thumb=True` になる。ARM の thumb 指定で奇数番地を渡すと bit0 を外さない。
- S-09（D）`SshTarget.get/put` の remote はそのまま scp に渡す（空白・シェルの特殊文字。scp の版でシェル展開の有無が違う）。`uprobe_line` の binary に空白があると uprobe_events の書式が壊れる。
- S-10（D）`linux_events_read` の返す dict は変換できない出来事だと内部の本体そのもの（コピーでない）。別スレッドの `_linux_keep` が `known["paths"]` を変えうる。
- S-11（D）`linux_deploy` の `mkdir -p` は `check_returncode()` で CalledProcessError（他は RuntimeError）。
- S-12（D）`linux_core_status`: pid を渡して存在しないプロセスだと `process=None` で、「無い」ことが明示されない。

## 調べた範囲 / 調べていない範囲

- 精読: `probe_linux.py` 全体、`linux/` の ring・clock・perf・uprobe・caps・agent.py（PC 側）・ssh・gdb_relay・memap・ptrace_plan。
- 動かして確かめた: RingReader のランダム入力と基準モデル（巻き戻し・端の分割・limit・lost の合計。1 周以上の追い越し含む。矛盾なし）、ptrace_plan の命令の網羅（分岐・PC 書き込み・Thumb の各種。矛盾なし）、TargetClock の端、上記の再現試験。
- 調べていない: `linux/agent/fprobe_agent.c`（ターゲット側の C）、`docs/linux.md` の記述と実装の突き合わせ（時間切れ）、`_linux_flush` の並行実行（スレッドの競合）の動的な試験、`parse_caps` の実機の出力の多様性、実機・ネットワーク・合成（触れない指示）。
