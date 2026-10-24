# ヒューリスティックの 2 回目の点検: host/fprobe の下位フォルダ

- 対象: main（18ef59d0）の host/fprobe の api・data・decode・dwarf・export・gdb・linux・logfmt・testpoint・rtos・profile・
  trace・tracefmt の .py すべて（131 ファイル。plugins・テスト・tests/fake_* は除く）
- 方法: 検索で候補を拾わず、各ファイルを先頭から最後まで読んだ（1 回目の報告は読んでいない）
- 基準: CLAUDE.md の「fprobe 本体にヒューリスティックを入れない」。経験則の閾値・仮定に頼る推定・それによる判定や
  並べ替え・名前の慣習での判定は H。打ち切りで「切ったこと・使った上限」を返さないものも H（原則の明文）。
  判断がつかないものは ?（理由を書く）。
- コードは変えていない。

## H（ヒューリスティック）

### H1. decode/i2c.py:141-145 — 同じサンプルに落ちた SCL・SDA の立ち下がりを START とみなす

```python
elif changed & _SDA and changed & _SCL and scl_high_before and cur is None and not now & _SDA:
    # SCL の立ち下がりと SDA の立ち下がりが同じサンプル（オーバーサンプリングが低い）: 休止中なら START
    out.append(Annotation(at, at + 1, "start", "S"))
```

本当の順序は測れていない（同じサンプル）のに、「休止中なら START だろう」という仮定で START にしている。注釈に
「順序が分からなかった」印が無いので、普通の START と区別できない。147 行の「同じサンプルで変わったら変わった後の
SDA を読む」も同じ種類の仮定。

### H2. dwarf/unwind.py:196-202 — CFI の無い最内の関数を「葉」と仮定して呼び出し元を作る

```python
if found is None:
    if frame.index != 0:
        return None                           # デバッグ情報の無い関数の中: これ以上は推測しない
    cfa = regs.read(SP)                       # 葉と仮定する（何も積んでいない）: 結果のフレームに書く
    restored = RegisterFile({SP: cfa, LR: regs.read(LR)}, parent=regs)
    note = "lr_without_cfi"
```

仮定に頼る推定でフレーム 1 以降を出す。note "lr_without_cfi" は付くので隠してはいないが、推定そのものが本体にある
（関数が LR を積んでいれば偽の呼び出し元になる）。

### H3. dwarf/unwind.py:178-186・211-216、dwarf/inspector.py:53・94-101 — 巻き戻しの打ち切り・止めた理由を返さない

```python
while len(frames) < self.max_frames:          # unwind.py:178（max_frames は既定 64。Inspector も 64）
...
if cfa < (regs.try_read(SP) or 0):
    return None                               # スタックが逆向き: 壊れている（unwind.py:215-216）
```

max_frames に達して止めても、backtrace の結果に「切った・上限 64」が無い。CFA < SP・戻り先 0 / 0xFFFFFFFF・同じ
(pc, sp) の再来で止めた理由も返らない（「壊れている」はコメントの中の判定）。

### H4. dwarf/values.py:26・89-105 — char * の先の文字列を 256 バイトで黙って切る

```python
STRING_LIMIT = 256                # char * の先を読む最大のバイト数
...
while len(out) < STRING_LIMIT:
```

NUL が来ないまま上限に達しても、返す {"pointer", "string"} に切った印も上限も無い。配列（max_elements）は
{"length", "head"} で切ったことが分かるのと対照的。

### H5. logfmt/stream.py:25・40-43 — 改行の来ない行を 4096 バイトで切って印を付けない

```python
LINE_LIMIT = 4096
...
if len(self._buf) > LINE_LIMIT:
    end = LINE_LIMIT
```

切った行も普通の LogRecord（format "text"）で、切ったこと・上限が残らない（続きは次の行に見える）。

### H6. logfmt/compact.py:32・69-84 — 語が書式の番地かを「印字できる文字の NUL 終端の列」で判定する

```python
def is_start(self, address: int) -> bool:
    """番地が文字列の先頭か（前のバイトが NUL か区画の先頭で、印字できる文字の NUL 終端の列が続く）。"""
    ...
    end = data.find(b"\0", offset, offset + STRING_LIMIT)     # STRING_LIMIT = 512
    ...
    return all(c.isprintable() or c in "\t\r\n" for c in text)
```

同期の取り直しで「この語は書式の番地か」を、文字の見た目（印字できるか）と 512 バイトの暗黙の上限で決めている。
512 より長い書式・印字できない文字を含む書式は黙って「番地でない」として読み飛ばされる（undecoded の行にはなる）。

### H7. rtos/freertos.py:66-68 — 関数名の有無で port の積み方を決める

```python
def _detect_port(self) -> str:
    """ARM_CM4F・ARM_CM7 の port にだけ vPortEnableVFP がある（FP の文脈を EXC_RETURN と一緒に積む）。"""
    return "armv7m_fp" if self.function_address("vPortEnableVFP") is not None else "armv7m"
```

port="auto"（既定）で、シンボル名から文脈の並びを推定する。名前の慣習での判定。違う port（独自の port・名前の同じ
別物）なら保存された文脈を読み違え、呼び出し履歴が誤る。結果に「自動で決めた」の印は snapshot の port だけ。

### H8. rtos/freertos_objects.py:42・301-323 — 変数名の有無でヒープの方式を決め、総量を固定の 8 で引く

```python
if next_free is not None:                                       # heap_1: 先頭から順に切り出すだけ
    total = None if heap_array is None else heap_array - PORT_BYTE_ALIGNMENT
...
scheme = "heap_2" if minimum is None else ("heap_4" if heap_array is not None else "heap_5")
...
info.total = heap_array - PORT_BYTE_ALIGNMENT if scheme != "heap_2" else heap_array
```

xNextFreeByte・xMinimumEverFreeBytesRemaining・ucHeap という変数名があるかで heap_1/2/4/5 を推定し、総量は
portBYTE_ALIGNMENT を 8 と決め打ちして引く（port で違う値）。scheme・total は推定なのに事実と同じ欄で返る。

### H9. rtos/nuttx.py:305-314 — 実行中のタスクの SP を「スタックの範囲に入る方」で選ぶ

```python
for name in ("psp", "sp"):
    ...
    if end is not None and base <= value <= end:
        return value
```

psp と sp のどちらがタスクの SP かを値の見た目（範囲に入るか）で決めている。両方入る・偶然入る場合に誤り、
選んだ根拠は結果に残らない。

### H10. rtos/ucos3.py:188-191 — 「OS で始まり Ptr で終わる」名前の変数をカーネルのものとして除く

```python
return frozenset(v.name for v in self.info.global_variables()
                 if v.name.startswith("OS") and v.name.endswith("Ptr"))
```

名前の慣習での判定。アプリの変数（例 OSAppTaskPtr）も除かれ、OS_CFG_DBG_EN = 0 のときのタスクの集め方
（handles の typed_objects）やオブジェクトの名前から漏れる。

### H11. rtos/threadx_objects.py:41 — 「_tx_」で始まる変数をカーネルのものとして除く

```python
kernel = frozenset(v.name for v in self.rtos.info.global_variables() if v.name.startswith("_tx_"))
```

名前の接頭辞での判定（影響はオブジェクトの別名 aliases だけ。オブジェクト自体は作られた環から見つかる）。

### H12. testpoint/instrument.py:30・264-265 — 全大文字の語を含む戻り値の型をマクロとみなす

```python
_MACRO_LIKE = re.compile(r"^[A-Z_][A-Z0-9_]+$")
...
if rtype is None or any(_MACRO_LIKE.match(w) for w in rtype.split()):
    rtype = "__auto_type"
```

名前の慣習（大文字はマクロ）で、挿入するコードの型を変える。測定の結果ではなく生成するコードへの影響
（全大文字の typedef も __auto_type になる。GNU 拡張が要る）。

### H13. tracefmt/eventrecorder.py:213-216 — 成分の名前に "Thread" を含むかでタスクの出来事かを決める

```python
if out.get("tracking") == "Start":
    kind = "task_create" if out.get("component") and "Thread" in out["component"] else "object"
elif out.get("tracking") == "Stop":
    kind = "task_delete" if out.get("component") and "Thread" in out["component"] else "object"
```

SCVD の成分の名前の部分一致（名前の慣習）で kind を決める。kind はタスクの時間軸（task_events）に流れる。

### H14. tracefmt/tracex.py:128-130 — 出来事の文脈の変化から task_in を推定して出す

```python
if ctx not in (ISR_CONTEXT, INIT_CONTEXT) and ctx != running and isr_depth == 0:
    trace.events.append(TraceEvent(ts, "task_in", "context", ctx, {"inferred": True}))
```

切り替えの出来事が無いので、文脈が変わった最初の出来事の時刻を切り替えとする（時刻は近似）。{"inferred": True}
と notes で示しているが、推定した出来事を事実の出来事と同じ列に入れ、task_events → 時間軸（run の長さ・負荷）に
そのまま使われる。

### H15. logfmt/trice.py:330-334 — 行頭の「xxx:」をチャンネルとみなし、小文字だけなら本文から除く

```python
if m := _TAG.match(full):
    channel = m[1]
    level = self.levels.get(channel)
    if channel.islower():
        full = full[m.end():].lstrip()
```

名前の慣習（小文字のタグはチャンネル）で本文を書き換える。"value: 3" のような普通の文もチャンネルにされ、
本文から "value:" が消える（channel 欄には残る）。Trice の道具の表示に合わせたものだが、判定は本体にある。

## ?（判断がつかない）

| # | 場所 | 中身 | 判断がつかない理由 |
|---|---|---|---|
| ?1 | api/daemon.py:566 | `"timestamps": not name.startswith("swo")` | 流れの名前で行頭の時刻を付けるか決める。名前は fprobe 自身の識別子（rtt0〜/swo/uart）なので慣習とは言い切れないが、接頭辞での判定 |
| ?2 | api/daemon.py:50・157-159 | `if conn.sending >= NOTIFY_LIMIT: conn.dropped += 1` | 通知を 1000 件で捨てて数えるが、dropped をどこにも返さない（打ち切りの未報告）。輸送の層で測定の結果ではない |
| ?3 | decode/can.py:25・180 | `IDLE_BITS = 10.5` | 規格の 11 ビットに半ビットの余裕を足した値（サンプルの量子化のため）。規格由来か経験の余裕か |
| ?4 | decode/i2s.py:36・42-43・73-74 | `emit(tr.length, len(bits) < expected)` | 最後のスロットの truncated を「前のスロットのビット数」と比べて決める（スロット長が一定という仮定） |
| ?5 | decode/onewire.py:14-20 | `TIMING = {False: {"sample": 15.0, ...}, True: {...}}` | 省略時の区別の時間を特定のデバイス（DS18B20・DS2431）のデータシートの値にしている。「規格の値」とあるが暗黙の既定（使った値は params に入る） |
| ?6 | decode/spiflash.py:14-32・80-83 | `COMMANDS`・`_WRITES` の表 | ベンダで意味の違う命令（0x35・0x15・0x31・0x11・0x75・0x7A・0x4B 等）を固定の意味で読み、write_disabled の誤りもこの表に依存 |
| ?7 | decode/swd.py:168-174 | `if a + 3 <= n and bits[a] and bits[a + 1] and bits[a + 2]:` | パリティの誤った要求を「続く 3 ビットが 1（プルアップ）」なら要求と認める。プロトコル由来だが値の見た目での認定 |
| ?8 | dwarf/cexpr.py:334-362・387 | `elif longs == 1 ...: name, size = ..., 4` / `size = s.size or 4` | DWARF に型名が無いとき long・int 等の大きさを 32 ビットの ABI と仮定（ARM 前提。Linux の aarch64 では誤る） |
| ?9 | dwarf/info.py:425-429、dwarf/location.py:122-124・248-249 | `Unavailable("最適化で消えた")` | 位置の無い変数・空の位置式の理由を「最適化」と書く（DWARF の慣例どおりだが、理由の推定の文言） |
| ?10 | gdb/description.py:101 | `r.blocksize or 0x400` | flash の消去の単位が分からないとき 0x400 を GDB に申告（暗黙の既定。GDB の vFlashErase の範囲に影響） |
| ?11 | gdb/trace.py:126-127 | `if value == 0xFFFF_FFFF and len(text) == 8: return -1` | 8 桁の ffffffff を番地でなく -1 とみなす（GDB の書き方の慣例） |
| ?12 | gdb/session.py:323-325 | `next((t["id"] for t in threads if t["current"]), threads[0]["id"])` | 実行中のスレッドが無いとき先頭のスレッドを現在とする |
| ?13 | logfmt/defmt.py:39、logfmt/printf.py:26 | `_INTS.update(usize=(32, False), isize=(32, True))` / `"l": 32, "z": 32, "t": 32` | ターゲットを 32 ビットと仮定（docstring に「32 ビットの ABI」と明記） |
| ?14 | logfmt/defmt.py:231 | `encoding or "rzcobs"` | `_defmt_encoding_` が無い ELF で枠を rzcobs と仮定 |
| ?15 | logfmt/framing.py:17-37 | `self.dropped += len(self._buf)` | 6.4 万バイト区切りが来ないと捨てて数えるが、LogStream.errors 等に出す経路が範囲内に無い（範囲外の呼び手が出しているかは未確認） |
| ?16 | testpoint/decode.py:214-215・244-250 | `if self._last is not None and cyc < self._last: high += 1 << 32` | CYCCNT の回り込みを「記録の間 < 2^32 クロック」と仮定（docstring に明記、結果に印は無い） |
| ?17 | testpoint/live.py:244-255 | `"uncovered": uncovered[:limit]` | 切る前の数（*_total）は返すが、使った limit を返さない |
| ?18 | testpoint/analysis.py:242、rtos/analysis.py:363、rtos/flow.py:32 | `bucket_s=1.0` / `points=200` / `POINTS = 500` | 集計の幅・点の数の暗黙の既定（使った値 bucket_s・points_width・points_total は返す） |
| ?19 | profile/aggregate.py:117-128 | `parts[key].most_common(members)` / `rows[:top]` | グループの関数を members=5 で切るが切った数を返さない。groups_total は返すが使った top は返さない |
| ?20 | profile/cycles.py:85-93 | `return (end - start) % modulo`（溢れの数が無い・合わないとき） | 「1 周未満」と仮定して増分を出す（docstring に明記、結果に印なし） |
| ?21 | profile/dwt.py:53-57・150-152・259-260 | `_PC_PACKET = 5  # 多い方で見積もる` / `while usage(index)[1] > headroom ...: index += 1` / `plan.rate_hz = estimate["max_rate_hz"]` | 帯域・1 巡回の見積もり（仮定のパケット長）でサンプル間隔・率を自動で変える。設定の計画で測定ではなく、fitted・warnings で示す |
| ?22 | rtos/analysis.py:547 | `"unbounded": bool(interference)` | 中間の優先度のタスクが動いた事実に「際限の無い逆転」という判定の名前を付ける |
| ?23 | rtos/timeline.py:130-131、rtos/analysis.py:485 | `achieved = statistics.median(diffs)` / `edge = int(tl.resolution // 2)` | 間隔の中央値を分解能とし、逆転の「確かな範囲」を縮める幅に使う（実際の間隔はサンプルごとに分かっている） |
| ?24 | rtos/flow.py:169-175 | `elif op.obj == w.obj and (op.kind in _SUCCESS or op.kind in _FAILED): w.outcome = ...` | 待ちの結果を「起きた後の同じオブジェクトへの次の操作」の成否に対応付ける（対応付けの仮定） |
| ?25 | rtos/flow.py:397-399 | `stack.pop()  # 出の欠けた内側の回は捨てる` | 捨てた回の数を返さない |
| ?26 | rtos/flow.py:437 | `5 * words * 10 / baud / seconds` | SWO を 1 バイト 10 ビット（NRZ）と仮定（Manchester では違う） |
| ?27 | rtos/nuttx.py:325-328 | `except ValueError: return read_nuttx_context(..., self.fpu_default)` | 文脈の大きさで FPU の枠が決まらないとき、黙って ELF の Tag_FP_arch で決める |
| ?28 | rtos/nuttx_objects.py:43 | `{"count": max(count, 0)}` | NuttX の負の semcount（= 待ち手の数）を 0 にして事実を失う |
| ?29 | rtos/zephyr.py:102-104 | `state = TaskState.BLOCKED; wait = WaitInfo("sleep")` | QUEUED でも PENDING でもないスレッドを一律「sleep」と呼ぶ（起動前も含む。分からないと言わない） |
| ?30 | rtos/zephyr_objects.py:179 | `{"size": (top - base) // 4, "used": (nxt - base) // 4}` | stack_data_t を 4 バイトと仮定 |
| ?31 | rtos/freertos_objects.py:214・233、rtos/rtx5.py:34、rtos/threadx.py:34、rtos/ucos3.py:34、rtos/nuttx.py:32 | `cstring(name_ptr, 32)` / `NAME_LIMIT = 64` | 名前を上限で黙って切る（印なし） |
| ?32 | rtos/stackcheck.py:54-56 | `needle = (pattern * 4)[:max(4, len(pattern))]` / `_sample` の CHUNK | fill_above の「塗り 4 回分の並びが底から CHUNK までにあるか」は暗黙の閾値を含む事実 |
| ?33 | trace/coresight.py:92・172 | `vmid_bytes=self.vmid_bytes or 1, cid_bytes=self.cid_bytes or 4` / `& 0xF00 or 0x300` | ID レジスタが「無い」と言う値を暗黙の既定で置き換える（M-profile では使われない欄） |
| ?34 | tracefmt/ctf.py:90・175、tracefmt/stream.py:49・67・105 | `else _header("ts32_id8")` / `header: str = "ts32_id8"` | 見出しの形（Zephyr 4.3 まで）を暗黙の既定にする。違えば以後が化ける |
| ?35 | tracefmt/ctf.py:262 | `kind = "object" if name.startswith(_OBJECTS) else "other"` | 出来事の名前の接頭辞で kind を分類（Zephyr の名前の表への写しだが、metadata の任意の名前にも効く） |
| ?36 | tracefmt/systemview.py:248-249 | `self.trace.lost += max(lost, 1)` | 失った数を最低 1 と数える（docstring で「確かでない」と明記） |
| ?37 | tracefmt/tracealyzer.py:237-247 | `if self._isr_depth > 1: ... isr_exit` / TASK_ACTIVATE で残りの ISR をすべて閉じる | ISR の出を ISR_RESUME・TASK_ACTIVATE から推定 |
| ?38 | tracefmt/eventrecorder.py:172 | `{"level": "info"}` | stdout の行に記録に無い重要度 info を付ける |

## 範囲の中で、原則どおりと確かめた主な所（参考）

- decode の解読器は枠の設定をすべて必須にしている（uart のボー推定なし・spi/i2s/parallel/manchester/can の既定なし・
  registry.check）。
- rtos の列の辿りは stopped_note で止めた理由と上限を warnings に残す。typed_variables は TypedSearch に上限で見ていない
  所を残す。
- testpoint の Report・report_dict・calls_dict は *_total・truncated・limits・dropped を返す。long_call・silence・
  no_exit は使う側が明示したときだけ。
- profile の aliasing は経験則の余裕を使わず信頼区間の事実だけ。timeline_from_samples の gap_factor は明示のときだけ。
- tracefmt の scvd の readlist・list・文字列の上限は notes に出す。stream の ctf のスナップショットは truncated を返す。

## 読んだファイル（131。すべて先頭から最後まで読んだ）

api（9）: `__init__.py`・`__main__.py`・`activity.py`・`arbiter.py`・`binary.py`・`client.py`・`daemon.py`・`gen.py`・`spec.py`

data（2）: `connect_scripts/reset_with_nreset.py`・`connect_scripts/stm32_debug_low_power.py`

decode（17）: `__init__.py`・`can.py`・`core.py`・`eeprom.py`・`i2c.py`・`i2s.py`・`jtag.py`・`manchester.py`・`modbus.py`・
`onewire.py`・`parallel.py`・`registry.py`・`spi.py`・`spiflash.py`・`swd.py`・`synth.py`・`uart.py`

dwarf（9）: `__init__.py`・`access.py`・`cexpr.py`・`info.py`・`inspector.py`・`location.py`・`typesys.py`・`unwind.py`・`values.py`

export（3）: `__init__.py`・`perfetto.py`・`profile_formats.py`

gdb（8）: `__init__.py`・`agent.py`・`client.py`・`description.py`・`rsp.py`・`server.py`・`session.py`・`trace.py`

linux（11）: `__init__.py`・`agent.py`・`caps.py`・`clock.py`・`gdb_relay.py`・`memap.py`・`perf.py`・`ptrace_plan.py`・`ring.py`・
`ssh.py`・`uprobe.py`

logfmt（9）: `__init__.py`・`addresses.py`・`compact.py`・`defmt.py`・`framing.py`・`printf.py`・`record.py`・`stream.py`・`trice.py`

testpoint（12）: `__init__.py`・`__main__.py`・`analysis.py`・`clex.py`・`cparse.py`・`decode.py`・`instrument.py`・`live.py`・
`probes.py`・`record.py`・`synth.py`・`trace.py`

rtos（25）: `__init__.py`・`analysis.py`・`base.py`・`contention.py`・`cortexm.py`・`flow.py`・`frames.py`・`freertos.py`・
`freertos_objects.py`・`kernel_trace.py`・`model.py`・`nuttx.py`・`nuttx_objects.py`・`objects.py`・`rtx5.py`・`rtx5_objects.py`・
`stackcheck.py`・`structs.py`・`threadx.py`・`threadx_objects.py`・`timeline.py`・`ucos3.py`・`ucos3_objects.py`・`zephyr.py`・
`zephyr_objects.py`

profile（11）: `__init__.py`・`aggregate.py`・`aliasing.py`・`coverage.py`・`cycles.py`・`dwt.py`・`exceptions.py`・`pcprofile.py`・
`samples.py`・`stats.py`・`timing.py`

trace（6）: `__init__.py`・`coresight.py`・`elements.py`・`etmv4.py`・`reconstruct.py`・`tpiu.py`

tracefmt（9）: `__init__.py`・`ctf.py`・`eventrecorder.py`・`model.py`・`scvd.py`・`stream.py`・`systemview.py`・`tracealyzer.py`・
`tracex.py`

上の 131 ファイルは、すべて先頭の行から最後の行まで通して読んだ（行番号付きで全文を表示して読んだ。decode/synth.py・
testpoint/synth.py は試験・デモの波形の発生器で、数字は題材なので所見に入れていない）。
