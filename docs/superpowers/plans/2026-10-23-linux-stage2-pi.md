# 組み込み Linux のアプリのデバッグ 段階 2（道 B、まず Pi 2B で） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** エージェントが出来事を物理メモリのリングに書き、fprobe が JTAG でそのリングを読む道 B を、Pi 2B で動かす。ネットワークが切れても最後まで読めることを、実機で確かめる。

**Architecture:**
- Pi 2B の DAP には、止めずにメモリを読む入口（AHB/AXI-AP）が無い（2026-10-22 の事実）。そのため Pi では「コアを 1 つ短く止めて、そのコアに読ませる」方式で読む。使う側が `access="halt"` と明示したときだけ使う。
- 止めている時間を短くするため、コアに読ませる部分を、ARMv7 の DCC の速いモード（LDC p14 の繰り返し）と CMSIS-DAP のブロック転送（pyOCD の `reg_read_repeat`）にする。1 語あたりの往復を無くす。FPGA の回路は変えない。
- リングは決まった長さの記録の輪にする。
  - 各記録の頭に番号（seq）を書き、書きかけ・上書きを番号の食い違いで見つける。
  - 欠けた範囲は事実として返す。推測では埋めない。
- 止めずに読む方式（Zynq の AHB-AP）は、後で読み出しの部分だけを差し替える。

**Tech Stack:** Python（既存の arm_debug・armv7a_debug・raw_dap・pyOCD の link）、C（エージェント）、Pi 2B（Raspberry Pi OS 13、Cortex-A7、PAGE_OFFSET 0x80000000、2G/2G）。

**Spec:** docs/superpowers/specs/2026-10-22-linux-app-debug-design.md（§3.1・§3.6・事実の節）

## Global Constraints

- ヒューリスティックを入れない。リングの番地・大きさ・読む仮想番地・止めるコア・読む間隔は、使う側が明示して渡す。
- 止めて読むのは `access="halt"` を明示したときだけにする（§3.6）。止めた回数・時間（ms）・読んだ語数を結果と記録に出す。
- 止めたコアは、どの失敗の経路でも必ず再開を試みる（try/finally）。
  - 再開できなければ、その事実を返す。
  - Pi のウォッチドッグは 1 分（systemd）。止めたまま 1 分を超えると、Pi は再起動する。
- PYNQ は触らない（Linux が固まると電源の入り切りが要る）。
- 文言は日本語。新しい依存を足さない。

## Review Focus

1. **止めている途中の失敗**: 読みのアボート・DAP の誤り・時間切れでも、コアが再開されること。→ Task 1 の `test_fast_read_resumes_on_abort`
2. **書きかけ・上書きの記録**: 番号の食い違う記録を、正しい記録として返さないこと。欠けた範囲を返すこと。→ Task 2 の `test_torn_and_overrun_are_reported`
3. **リングの頭が読めない・違う**: magic・版が違えば、何も解かずに事実で返すこと。→ Task 2 の `test_bad_header_is_refused`
4. **A と B の両方で同じ出来事を受けた**: 番号で 1 つにすること。→ Task 4 の `test_same_event_from_a_and_b_is_kept_once`
5. **速いモードの最後の語**: 速いモードでは、最後の 1 語だけ別の手順で読む。その語を落とさないこと。→ Task 1 の `test_fast_read_matches_slow_read`

---

### Task 1: 止めて読むメモリの読み出しを速くする（DCC の速いモード + ブロック転送）

**Files:**
- Modify: `host/fprobe/arm_debug.py`（`ArmDebugCore.read_memory_fast(address, count) -> list[int]`）
- Modify: `host/fprobe/armv7a_debug.py`（`V7DebugPort.read_dtrtx_block(count) -> list[int]`: TAR を DTRTX に置き、DRW を count 回まとめて読む。`dap.link.reg_read_repeat`）
- Modify: `host/tests/arm_a_fakes.py`（DSCR.ExtDCCmode = 2 の速いモード: ITR に書くと命令を覚えて 1 回実行し、DTRTX を読むたびに覚えた命令を実行し直す。LDC p14,c5,[r0],#4 = 0xECB05E01）
- Test: `host/tests/test_arm_a.py`

**手順**（OpenOCD の cortex_a_read_cpu_memory_fast と同じ）:
1. r0 に番地を入れる。
2. DSCR を速いモードにして、ITR に LDC を書く。
3. DTRTX を count−1 回まとめて読む。
4. DSCR を非ブロックのモードに戻し、InstrCompl を待つ。
5. 最後の 1 語を、TXfull を待ってから読む。
6. 最後にアボートを確かめる（既存の `_check_abort`）。

- [ ] 試験を書く。
  - `test_fast_read_matches_slow_read`: 偽のコアのメモリ 64 語を、速い読みと遅い読みで読んで同じ値になる。count = 1 と 2 でも同じ。
  - `test_fast_read_reports_abort`: 番地の途中でアボートすると ArmDebugError になり、文に番地を含む。読んだ後は DSCR が非ブロックのモードに戻っている。
- [ ] 落ちることを見る → 作る → 通る。
- [ ] `ArmATarget.read_memory_block32` から使う。count ≥ 4 で、ARMv7 で、size = 4 のとき。ARM11 は今までどおり。
- [ ] 実機（Pi 2B）で確かめる。HIL の `gateware/tests/hil/test_arm_a_pi2_hw.py`（FPROBE_PI2_JTAG=1）。
  - コア 1 を止めて、カーネルの線形の写し 0x80008000 から 1024 語を速い読みと遅い読みで読み、値が同じことを確かめる。
  - 速さと止めていた時間を印字する。
  - 最後に再開し、SSH で生きていることを確かめる。
- [ ] コミット。

### Task 2: リングの形と解析（PC 側）

**Files:** Create `host/fprobe/linux/ring.py`; Test `host/tests/test_linux_ring.py`

**形**（すべてリトルエンディアン）:
- 頭（64 バイト）: magic `b"FPRB"`・version u32 = 1・record_size u32 = 48・capacity u32（記録の数）・head u64（書いた記録の総数 = 次の seq）・clock u32（1 = CLOCK_MONOTONIC_RAW）・残りは 0。
- 記録（48 バイト）: seq u64・t_ns u64・tid u32・bp u16・kind u16（1 = bp）・regs u32 × 6（順は r0..r3、sp、lr）。
- 書き手の順:
  1. 記録の seq を 0xFFFF_FFFF_FFFF_FFFF にする。
  2. dmb。
  3. 中身を書く。
  4. dmb。
  5. seq = n にする。
  6. dmb。
  7. head = n + 1 にする。

**Interfaces:**
- `HEADER = 64`, `RECORD = 48`
- `parse_header(data: bytes) -> dict`（magic・版・大きさが違えば `RingError`）
- `class RingReader`: `__init__(self, capacity: int)`, `next_seq: int | None`
- `RingReader.take(head: int, slots: Callable[[int, int], bytes]) -> dict`
  - `slots(first_slot, count)` は記録の生のバイトを返す。
  - 返す形: `{"records": [...], "lost": [[from_seq, to_seq), ...], "torn": [seq, ...], "head": head}`
  - 処理: 前回の next_seq から head−1 までを読む。capacity を超えて追い越された分は lost に入れる。番号の合わない記録は torn に入れる。

- [ ] 試験を書く。
  - `test_reads_new_records_in_order`
  - `test_torn_and_overrun_are_reported`: 追い越しの分は lost、書きかけ（seq = 0xFFFF…）や前の周の番号は torn。
  - `test_bad_header_is_refused`
  - `test_wraps_around`
- [ ] 落ちる → 作る → 通る → コミット。

### Task 3: エージェントのリング（C）

**Files:**
- Modify: `host/fprobe/linux/agent/fprobe_agent.c`
  - op `ring_open {phys, size, cached}`: /dev/mem を mmap する（cached = false なら O_SYNC）。頭を初期化し、`{capacity}` を返す。
  - op `ring_close`
- 当たりの出来事に、エージェントの通しの番号 `seq` を付ける。その番号で stdout（道 A）とリング（道 B）の両方に出す。リングに書くのは bp の出来事だけ。

**Pi の準備**（Ruling: 一度だけ手で行い、docs/linux.md に書く）:
- device tree のオーバーレイ `fprobe-ring.dtbo` を入れる。
  - reserved-memory に `fprobe@20000000 { reg = <0x20000000 0x100000>; }` を置く。no-map は付けない（線形の写しに残し、止めたコアが PAGE_OFFSET + 物理番地で読めるようにする）。
- config.txt に `dtoverlay=fprobe-ring` を足して再起動する。
- /proc/iomem で予約を確かめる。

- [ ] 実機で確かめる。エージェントを sudo で起動し、`ring_open(0x20000000, 0x100000, cached=true)` をする。ptrace のプローブを付け、300 回の当たりを出す。
  - 止めて読んだリングが、stdout の出来事と seq・t_ns・r0 で一致すること（Task 4 の試験で行う）。
- [ ] コミット。

### Task 4: デーモンの API（linux.ring.*）と記録

**Files:** Modify `host/fprobe/probe_linux.py`・`host/api/fprobe_api.json`（生成物）; Test `host/tests/test_probe_linux.py`

**Interfaces:**
- `linux.ring.open {target, phys, size, cached}`: エージェントにリングを開かせる。
- `linux.ring.read {target, virtual, access: "halt", core}`:
  - fprobe の JTAG の接続（target.connect で bcm2836 等。接続済みであること）で、コア core を止める。
  - virtual（使う側が明示する仮想番地。Pi では 0x80000000 + 物理番地）から頭と新しい記録を速い読みで読み、再開する。
  - 返す形: `{records, lost, torn, head, halted_ms, words}`
  - 出来事は道 A と同じ形にし、`path: "b"` を付ける。seq で重複を除いてメモリと記録に入れる。
- `linux.ring.watch {target, interval_s, virtual, access, core}` / `linux.ring.unwatch {target}`: 間隔ごとに read を行う。止めた回数と時間の合計を status に出す。
- 道 A の出来事にも seq を付けて、同じ seq は 1 つにする（先に来た方を残し、`paths: ["a", "b"]`）。

- [ ] 試験を書く（偽のコアのメモリにリングを置く）。
  - `test_ring_read_returns_records_and_halts_once`
  - `test_same_event_from_a_and_b_is_kept_once`
  - `test_ring_read_requires_halt_access`: access の無い呼び出しは断る。
- [ ] 落ちる → 作る → 通る → 生成物 → 全体 → コミット。

### Task 5: 実機: ネットワークが切れても読める（Pi 2B）

**Files:** Create `gateware/tests/hil/test_linux_ring_pi_hw.py`

- [ ] 試験を書いて走らせる。
  1. エージェントを sudo で起動し、リングを開き、題材のアプリ（10 ms ごとに 3000 回 = 30 秒）に ptrace のプローブを付ける。
  2. ring.watch（interval_s = 0.5、core = 1）を始める。
  3. Pi で `sudo sh -c 'ip link set eth0 down; sleep 15; ip link set eth0 up'` を nohup で走らせる。
  4. アプリが終わるまで待つ。
- [ ] 合格の条件:
  - B（リング）で受けた seq が 1..3000 で欠けない（lost と torn が空）。
  - ネットワークが切れていた 15 秒の間の出来事も含まれる。
  - 止めた合計の時間と回数を印字する（合否には使わない）。
  - Pi が生きている（終わった後に SSH が通る）。
- [ ] docs/linux.md に事実を書いてコミット。

### Task 6: 最終レビューと修正

- [ ] review-package → 別のエージェント（opus）にレビューを頼む → Critical・Important を 1 回でまとめて直す → 台帳。
