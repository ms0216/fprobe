# ホストのリンクの正しさ（届かない・遅れた応答・欠け・上限）

2026-11-03。状態: 実装（ホスト。実機は未確認）。ホストの読むだけの点検（A2〜A12・C2〜C9・D1〜D3）の残りのうち、設計の判断が要る
A3・A4・A11 の設計をここに書く。A3・A4 と A11 の前半は 99d025cd・e04bb51c で実装済みで、ここではその設計を後から書き起こす
（仕様書の無いまま入っていたため）。A11 の後半（READ のやり直しを使う側が明示する口）はこの文書で決めて実装した。

守ること（CLAUDE.md の設計原則）:

- 本体に経験則を入れない。届かなかった・欠けた・分からないは、数えた事実として返す。
- 問題を隠す恐れのある暗黙の再送・暗黙の時間の延長を入れない。やり直しは使う側が明示したときだけ、した回数を事実として返す。
- PC のメモリに溜める量には必ず上限があり、上限は使う側が明示する（既定の暗黙の値を持たない）。

## 1. リンクの事実（何が起きうるか）

| 道 | 落ちる | 順が変わる | 流量制御 | 欠けの見え方 |
|---|---|---|---|---|
| USB（Bulk） | 落ちない | 変わらない | ある（NAK） | 無い |
| UART（SerialLink） | 落ちる・化ける | 変わらない | 無い | CRC・形の崩れ（bad_frames） |
| UDP（EthLink） | 落ちる | 変わりうる（同じ経路ならふつう変わらない） | 無い | FPGA → PC は 8 ビットの seq の飛び（lost_frames。下限）。PC → FPGA は PC から見えない |
| TCP（EthLink の PS 直読みの流れ） | 落ちない（再送は TCP が持つ） | 変わらない | ある（窓） | 無い |

PC → FPGA の要求が落ちたのか、FPGA → PC の応答が落ちた・遅れたのかを、PC は区別できない（回路は要求を覚えておらず、要求の
番号も返さない。CONTROL の tag だけは応答に写る）。

## 2. A3: バイト列のチャンネルとパケットのチャンネルの上限（実装 99d025cd）

- バイト列のチャンネル（SWO・UART0・STREAM）は、チャンネルごとの決まった大きさのリング（frame_link.ByteRing）。大きさは開く人が
  `buffer_bytes` で全部を明示する（FrameLink は既定を持たない）。Session は根拠付きの値 `session.link_buffer_bytes()` を渡す。
- 上限に達したとき:
  - 流量制御のある道（TCP）: 受信のスレッドは読み手が減らすまで受け取りを止める。止めた回数と時間（full_count・full_s）を数える。
    送り手（中継）が止まり、その後ろの DDR のリングが溢れれば回路が溢れを記録する。
  - 流量制御の無い道（UDP・UART）: 入らないフレームを丸ごと捨て、捨てたフレームとバイト（dropped_frames・dropped_bytes）を数える。
    フレームの途中で切らない（切るとバイトの並びのどこが欠けたか分からなくなる）。
- 数えは `FrameLink.buffer(ch)` が StreamBuffer で返す（事実だけ。判定はしない）。
- パケットのチャンネル:
  - DAP（ch 1）: 応答のキューは送った要求の数（_dap_outstanding）までしか伸びない。それより多い応答は頼んでいない応答として捨てて
    数える（unsolicited）。時間切れの後は同期を外し、開き直し（discard）が柵（DAP_Info）で同期し直すまで送受信を断る（DapDesync）。
    柵の応答より前に届いた応答は時間切れの前の要求のもの（回路は要求を順に処理する）なので捨てて数える（stale）。
  - CONTROL（ch 0）: 要求は 1 つずつ（Device の呼び手が錠で並べる）。応答のキューに溜まるのは、時間切れの後に遅れて届いた応答だけで、
    次の要求の受け取りが tag で読み捨てる（§4）。

限界（回路を変えずには消せない）: DAP の要求が UDP で落ちると、その後の応答が 1 つずつ前の要求に対応する（要求の欠けは PC から
見えない）。要求に番号を付けて応答に写すには回路（cmsis_dap の応答）を変える必要があり、CMSIS-DAP の形から外れる。ここでは扱わない。

## 3. A4: リンクの欠けと LaStream（実装 e04bb51c）

- EthLink は FPGA → PC のデータグラムの 8 ビットの seq の飛びを lost_frames に数える。どのチャンネルのデータグラムが落ちたかは
  分からないので、UDP で受けるどのバイト列のチャンネルの StreamBuffer.lost_frames にもこの数（と形の崩れ bad_frames）を返す。
- LaStream は流れを始めるときの StreamBuffer を控え、読むたびに差を見る:
  - dropped_frames が増えた（この流れのフレームを上限で捨てた）→ StreamLinkLoss。
  - 見出しの無い流れ（イベントを流さない RAW・RLE）で lost_frames が増えた → StreamLinkLoss（欠けた可能性。流れの中に番号が
    無いので、欠けていないと確かめられない。黙って続けない）。
  - 見出し付きの流れ（イベントも流す構成）は、見出しと通し番号で確かめる（欠ければ StreamFormatError）。
  - 出し切った（reading = 0）のに機器が書いたと言う量（stream_beats）まで届かない → StreamLinkLoss（8 ビットの seq で数えきれない、
    ちょうど 256 の倍数の欠けもここで見つかる）。
  どれも、それまでに返した分は正しい。StreamStats.link_buffer にこの流れの間の差を返す。
- 偽物の遅れでなく、データグラムを落とす偽の UDP から本物の EthLink・LinkTransport・LaStream を通す試験
  （test_la_stream_link_loss.py。D3）。

## 4. A11: CONTROL の「届かなかった」と「実行されたか分からない」

### 4.1 事実の区別

CONTROL の要求に応答が timeout のうちに届かないとき、PC に分かるのは「応答が届かなかった」ことだけで、要求が実行されたかは
分からない（要求が落ちた・応答が落ちた・応答が遅れている、の 3 つを区別できない）。だから:

- 時間切れは `ControlTimeout`（TimeoutError の子）。op・address・count と、`executed = None`（分からない、という事実）を持つ。
  `attempts` = 送った要求の数（やり直しを頼まなければ 1）。
- 時間切れの後に遅れて届いた応答を、次の要求の応答に読まない。要求の tag（8 ビット）を応答と照らし、違う tag の応答は読み捨てて
  数える（`ControlChannel.stale_responses`）。
  限界: tag は 8 ビットなので、ちょうど 256 個後の要求と同じ tag の遅れた応答は区別できない。要求は 1 つずつで、時間切れは
  1 回 2 秒なので、256 個の要求の間（少なくとも 256 往復）より遅れて届く応答でだけ起きる。
- 時間の長さは今の 2 秒（transport.TIMEOUT_MS）のまま。暗黙に延ばさない。

### 4.2 やり直してよいもの・いけないもの

- WRITE・EXCHANGE は書く。回路は要求を覚えていないので、送り直すと 2 回実行されうる（カウンタを進める書き・FIFO に積む書き・
  パルスの書き）。だから **やり直しの口を持たない**（`write`・`exchange` に引数が無い）。時間切れは ControlTimeout をそのまま上げ、
  使う側が状態を読み直して決める。
- READ は書かないが、読みに副作用のあるレジスタがある（CSR の `read_strobe`: 今は timebase.time_lo が読まれた瞬間の上位を
  time_hi に保持する）。副作用のあるレジスタをやり直すと、副作用が 2 回起きうる。どのレジスタを読むかを知っているのは使う側なので、
  **READ のやり直しは使う側が明示したときだけ**（既定 0 回）。

### 4.3 口

- `ControlChannel.read(address, count, *, timeout_retries=0)`・`Device.read(..., timeout_retries=0)`:
  READ が時間切れになったら、新しい tag で同じ READ を最大 timeout_retries 回送り直す。前の要求の応答が後から届いても tag が違うので
  読み捨てる（stale_responses に数える）。送り直した回数を `ControlChannel.read_retries` に数える（事実）。最後まで届かなければ
  ControlTimeout（attempts = timeout_retries + 1）。長い読み（MAX_READ_WORDS ごとに分ける）は、分けた要求ごとに数える。
- `ControlChannel.counters()` → `{"read_retries", "stale_responses"}`（開いてから。事実）。
- LaStream（`DeepAnalyzer.stream`・`stream_to_vcd`）の `control_read_retries`（既定 0）: 流している間の見回りの READ
  （la_deep.status〜stream_beats、gaps の流れは gaps まで。どれも読みに副作用が無い）を、時間切れのときに送り直してよい回数。
  使う側が明示する。StreamStats.control に、この流れの間の `read_retries`・`stale_responses`（差）を返す。
  実機で、PC が重いときに見回りの READ の応答が 2 秒を越えて長い流しが止まった（中継は詰まっていなかった）。その対処を、
  黙って延ばす・送り直すのでなく、使う側の選択と、した回数の事実にする。
- API: `la.stream` の引数 `control_read_retries`（0 以上の整数、既定 0）、応答の `control`（{read_retries, stale_responses}。
  USB でも数える）。`recording.la_stream.start` も同じ引数を受ける。
- 止める書き（la_deep.control = 0）は WRITE なのでやり直さない（時間切れなら誤りとして伝える）。

### 4.4 DAP（変えない）

CMSIS-DAP の応答には要求の番号が無い。時間切れの後は同期を外し、pyOCD の開き直しが柵で同期し直す（§2）。pyOCD の層は
DAP の転送の誤りを自分で扱う（TransferError）。DAP のやり直しは pyOCD の判断で、fprobe の本体は入れない。

## 5. 試験

- test_control.py: 既定では READ を送り直さない／明示した回数まで送り直し、前の要求の遅れた応答を読み捨てる／やり直しを使い切ると
  attempts 付きの ControlTimeout／WRITE・EXCHANGE にやり直しの口が無い。
- test_la_stream.py: 見回りの READ が 1 回時間切れになったとき、既定では流れが TimeoutError で止まり、control_read_retries = 1 なら
  続いて StreamStats.control.read_retries = 1。
- test_frame_link.py・test_eth_link.py・test_la_stream_link_loss.py（A3・A4。実装済み）。
