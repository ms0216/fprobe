"""ロジアナの連続ストリーム: 受け取り（LaStream）・かたまり（StreamChunk）・統計（StreamStats）。

欠けの位置決めは fprobe.la_gaps。DeepAnalyzer（fprobe.la_deep）が開始し、LaStream が機器から読む。見出しの分割は fprobe.la_segments。
fprobe.la が再公開する（使う側は fprobe.la から読んでよい）。fprobe.la には依存しない。

仕様: docs/superpowers/specs/2026-10-16-m8c-la-streaming-design.md（連続ストリーミング）
"""

import dataclasses
import time
from array import array
from collections.abc import Iterator
from dataclasses import dataclass, field

from . import generated as g
from .device import Device
from .events import KIND_LA_GAP, DapEvent, decode_events
from .frame_link import StreamBuffer
from .la_gaps import GapPlacer, StreamCursor, StreamGap, _expand_np, _expand_words  # noqa: F401  (再公開)
from .la_segments import SegmentSplitter, StreamFormatError, StreamLinkLoss
from .la_tokens import token_extent
from .la_wave import BEAT_BYTES, RLE_FORMATS, ChangeLimitExceeded, PIECE_CHANGES, decode_beats, iter_stream_transitions, stream_transitions
from .la_wave import _array_of, _extent, _np


@dataclass(frozen=True)
class StreamChunk:
    """ストリームの 1 回の受け取り（拍の整数倍）。復号は必要になったときに行う（受け取りを遅らせない）。
    data は bytes か、受け取ったバッファを写さずに指す memoryview（どちらも bytes のように読める。変えないこと）。"""
    data: bytes
    rle: bool
    first: int                   # 最初のサンプルの通し番号（ストリームの先頭 = 0）
    previous: int | None         # 直前のサンプルの値（最初のチャンクは None）
    events: tuple[DapEvent, ...] = ()   # 同じ受け取りに載っていたイベント（イベントも流す構成。古い順）
    pack: int = 0                # 詰めの種類（0 = 詰めない、1 = 8 ビット、2 = 4 ビット、3 = 2 ビット。1 語に 1 << pack サンプル）
    # 欠けの区間を記録する流れ（gaps）: このチャンクの直前の欠け（gap.start + gap.samples = first。previous は None）。None = 欠けなし
    gap: "StreamGap | None" = None
    count: int = field(default=-1, repr=False, compare=False)   # サンプル数（受け取りで数えた値。-1 = 数えていない）
    # RLE の形式（"records" = 1 レコード 4 バイト、"tokens" = トークン形式。fprobe.la_tokens）。rle = False のときは意味を持たない。
    # トークン形式では first = 直前の出来事の時刻、previous = 直前の値、length = 時刻の進み（出来事の間隔の和）
    rle_format: str = "records"

    def samples(self) -> "array[int]":
        if self.rle and self.rle_format == "tokens":
            raise RuntimeError("トークン形式のチャンクは毎サンプルに展開しない（changes / transitions を使う）")
        return _expand_words(decode_beats(self.data, self.rle), self.pack)

    @property
    def length(self) -> int:
        """このチャンクのサンプル数（次のチャンクの first = first + length）。展開しない（受け取りで数えた値があればそれ）。"""
        if self.count >= 0:
            return self.count
        if self.rle and self.rle_format == "tokens":
            return token_extent(self.data, self.first, self.previous)[0] if self.data else 0
        return _extent(self.data, self.rle)[0] << self.pack if self.data else 0

    def transitions(self, max_changes: int | None = None) -> tuple["array[int]", "array[int]"]:
        """変化点（値, 通し番号の開始）を array（"H"・"Q"）で。直前のチャンクの最後と同じ値は含めない（つなぐと全体の
        変化点になる）。中身は changes と同じ（max_changes も同じ）。"""
        values, starts = self.changes(max_changes)
        if _np is not None:
            return _array_of("H", values), _array_of("Q", starts)
        return values, starts

    def changes(self, max_changes: int | None = None):
        """transitions と同じ変化点を、numpy があれば numpy の配列（値 uint16・開始 uint64）のまま（VcdWriter.write 等へ
        写さずに渡す口）。numpy が無ければ array。詰めた語・RLE は展開せずに数える（la_wave.stream_transitions）。

        全変化点を 1 つの配列にする（メモリは変化点の数に比例する）。トークン形式の REPEAT は 1 トークンが何億もの変化点になりうる
        ので、巨大な展開は iter_changes / iter_transitions で断片ごとに扱う。max_changes（None = 上限なし。使う側が明示する）を
        渡すと、変化点がそれを超えるときは配列にせず ChangeLimitExceeded で断る（メモリは max_changes に比例する範囲）。"""
        if max_changes is None:
            return stream_transitions(self.data, self.rle, self.pack, self.first, self.previous, self.rle_format)
        pieces = []
        total = 0
        for v, s in self.iter_changes(min(max_changes + 1, PIECE_CHANGES)):
            if total + len(v) > max_changes:
                raise ChangeLimitExceeded(max_changes, total, int(s[max_changes - total]) if max_changes > total else int(s[0]),
                                          "チャンクを 1 つの配列にできない（iter_changes で断片ごとに扱える）")
            pieces.append((v, s))
            total += len(v)
        if not pieces:
            return stream_transitions(b"", False, 0, self.first, self.previous)
        if len(pieces) == 1:
            return pieces[0]
        if _np is not None:
            return _np.concatenate([v for v, _ in pieces]), _np.concatenate([s for _, s in pieces])
        values, starts = array("H"), array("Q")
        for v, s in pieces:
            values.extend(v)
            starts.extend(s)
        return values, starts

    def iter_changes(self, max_changes: int):
        """changes と同じ変化点を、(値, 開始) の断片（1 つが max_changes 個以下、空は返さない）に分けて順に返す。断片をつなぐと
        changes と要素単位で一致する。トークン形式の REPEAT は、展開の大きさに依らないメモリで復号する（max_changes は
        使う側が決める）。"""
        return iter_stream_transitions(self.data, self.rle, self.pack, self.first, self.previous, self.rle_format,
                                       max_changes=max_changes)

    def iter_transitions(self, max_changes: int):
        """iter_changes の断片に、その断片が覆うサンプルの終わり end を添えて返す: (値, 開始, end)。end は次の断片の最初の変化点の
        時刻、最後の断片は first + length。断片を順に feed する解析（Transitions(値, 開始, end)）向け。"""
        last = None
        for v, s in self.iter_changes(max_changes):
            if last is not None:
                yield last[0], last[1], int(s[0])
            last = (v, s)
        if last is not None:
            yield last[0], last[1], self.first + self.length


@dataclass(frozen=True)
class StreamStats:
    samples: int
    bytes: int
    elapsed_s: float             # 始めてから出し切るまで
    sample_period_s: float
    overrun: bool                # ホストが遅れて書き込みが止まった（それまでの分は正しい）
    link_buffer: StreamBuffer | None = None   # PC の受け取り済みで未読の量（この流れの間の最大・止めた回数と時間）。None = 測れない道（USB 等）
    usage: tuple[dict, ...] = ()   # リングと帯域の使用量の読み（LaStream.usage。stream_to_vcd が読んだもの、古い順）
    gaps: tuple["StreamGap", ...] = ()   # 欠けの区間（gaps の流れ。古い順。samples は値のあるサンプルだけを数える）
    unplaced_beats: int = 0      # 欠けの記録が届かず位置が決まらないので返さなかった拍（gaps の流れ。ふつうは 0）
    relay: dict | None = None    # 出し切った後の PYNQ の中継の状態（Device.relay_status: ゼロコピーの道か複写の道か・copied 等）。None = 中継が無い・返事なし
    gap_held_peak_bytes: int = 0   # gaps の流れ: 位置の決まらない拍を持った量の最大（バイト。上限 gap_hold_bytes と比べる事実）
    # この流れの間の CONTROL の数え {read_retries: 時間切れで送り直した READ, stale_responses: 読み捨てた遅れた応答}（事実）
    control: dict = field(default_factory=lambda: {"read_retries": 0, "stale_responses": 0})
    # 形式の事実: rle_format = "raw" | "records" | "tokens"、wire_bytes = 受け取ったロジアナの拍のバイト数（見出し・イベントを含まない）、
    # pad_bytes = そのうち NOP で埋めた分（トークン形式。ほかは 0）、changes = 変化点の数（復号して数えたもの。流れの間は数えず、
    # VCD への書き出し（raw_to_vcd）で数える。None = 数えていない）
    rle_format: str = "records"
    wire_bytes: int = 0
    pad_bytes: int = 0
    changes: int | None = None

    @property
    def bytes_per_change(self) -> float | None:
        """変化点 1 つあたりの拍のバイト数（wire_bytes / changes。変化点が 0 個・数えていないなら None）。割っただけの事実。"""
        return self.wire_bytes / self.changes if self.changes else None

    @property
    def gap_samples(self) -> int:
        """欠けの区間のサンプルの数の和（値の無いサンプル）。"""
        return sum(gap.samples for gap in self.gaps)

    @property
    def duration_s(self) -> float:
        """記録の長さ（流れの先頭から最後の値のあるサンプルまで。欠けの区間を含む）。"""
        return (self.samples + self.gap_samples) * self.sample_period_s

    @property
    def mb_per_s(self) -> float:
        return self.bytes / self.elapsed_s / 1e6 if self.elapsed_s else 0.0

    @property
    def link_buffer_dict(self) -> dict | None:
        """link_buffer を API の応答の形（StreamBuffer.as_dict。測れない道では None）。"""
        return None if self.link_buffer is None else self.link_buffer.as_dict()


_NOT_GIVEN = object()   # LaStream の buffer_start を渡していない印（None は「測れない機器」の意味）


class LaStream:
    """連続ストリーミングの受け取り（DeepAnalyzer.stream が始める）。

    デバイスは外部メモリを FIFO にして拍（リングと同じ形式）を流し続け、書いた拍の総数を stream_beats に示す。
    USB にはそれと受け取った量の差（必ず届く量）だけを求める（届かない量を待って時間切れで捨てることがない）。
    止める（run = 0）とデバイスは書き込みを止めて未読を出し切り、status.reading を 0 に戻す。

    events = (最初のイベントの番号, イベントのリングの件数) なら、イベントも流す構成（R1）: 流れは見出し付きの
    かたまりの列で、stream_beats は出すと決めた拍の総数（見出し・イベントを含む）。かたまりを分け、ロジアナの拍は
    今と同じに、イベントは StreamChunk.events に渡す。通し番号が飛べば StreamFormatError（黙って欠けない）。

    gaps = True（DeepAnalyzer.stream の gaps。イベントも流す構成だけ）: 欠けの記録（種類 19）はイベントに入れず、GapPlacer で
    欠けの区間にする。チャンクは欠けの区間で分かれ、欠けの直後のチャンクの gap に欠けを載せる（first は欠けの後の位置）。
    gap_hold_bytes（gaps のとき必ず明示する）は位置の決まらない拍を持つ上限（GapPlacer。超えたら StreamHoldExceeded で止める）。

    リンクの欠け（A4）: フレームのリンク（UDP・UART）で受けるとき、流れのバイトが欠けると後ろのサンプルの位置が決まらない。
    - リンクが流れのチャンネルのフレームを捨てた（PC に溜める上限を超えた: StreamBuffer.dropped_frames が増えた）→ StreamLinkLoss。
    - 見出しの無い流れ（イベントを流さない RAW・RLE）は流れの中に番号が無いので、流れを運ぶ道でフレームが落ちた・壊れた
      （StreamBuffer.lost_frames が増えた。どのチャンネルのものかは分からない）→ StreamLinkLoss（欠けた可能性。黙って続けない）。
      見出し付きの流れは見出しと番号で確かめる（欠ければ StreamFormatError）。
    - 出し切った（reading = 0）のに、機器が書いたと言う量（stream_beats）まで届かない（UDP の 8 ビットの seq で数えきれない欠け等）
      → StreamLinkLoss。
    どれも、それまでに返した分は正しい。StreamStats.link_buffer にこの流れの間の数え（差）を返す。

    CSR の往復は、1 回の見回りで 1 回: status から stream_beats まで（gaps の流れは gaps まで）を 1 つの READ で読む（CONTROL の
    READ は番地の順に読むので、status → stream_beats → gap_last_beat → gaps の順の約束はそのまま）。見回りの READ の応答が
    時間切れになったら、使う側が control_read_retries で明示した回数まで送り直す（既定 0。止める書きは送り直さない。
    docs/superpowers/specs/2026-11-03-host-link-correctness-design.md §4）。
    """

    READ_MAX = 4 << 20           # 1 回の読みの上限（バイト）
    READ_MIN = 64 << 10          # 走っている間はこれだけ溜まるか
    READ_INTERVAL_S = 0.05       # これだけ経つまで読まない（小さな読みを繰り返さない）
    POLL_S = 0.001

    def __init__(self, dev: Device, rle: bool, sample_period_s: float, channels: int,
                 events: tuple[int, int] | None = None, pack: int = 0, fine_timing: dict | None = None,
                 first_channel: int = 0, gaps: bool = False, gap_hold_bytes: int | None = None,
                 control_read_retries: int = 0, buffer_start: "StreamBuffer | None | object" = _NOT_GIVEN,
                 rle_format: str = "records"):
        """buffer_start: 欠けの数えの基準（機器に流れを頼む前に取った _stream_buffer(dev, reset_peak=True)）。渡さなければここで取る
        （流れを頼んだ後に作るなら、その間に落ちたフレームを数えない）。"""
        self.dev, self.rle, self.sample_period_s, self.channels = dev, rle, sample_period_s, channels
        if rle_format not in RLE_FORMATS:
            raise ValueError(f"rle_format は {RLE_FORMATS} のどれか: {rle_format!r}")
        if rle_format == "tokens" and not rle:
            raise ValueError("rle_format = tokens は rle = True のときだけ")
        if rle_format == "tokens" and pack:
            raise ValueError("トークン形式の RLE は詰める（pack_channels）と一緒に使えない（RLE は詰めない）")
        self.rle_format = rle_format                        # RLE の形式（rle = False のときは意味を持たない）
        if control_read_retries < 0:
            raise ValueError(f"control_read_retries は 0 以上: {control_read_retries}")
        self._control_read_retries = control_read_retries   # 見回りの READ を時間切れで送り直してよい回数（使う側が明示する）
        self._control_start = dev.control_counters()        # この流れの間の CONTROL の数えを測り始める
        self.pack = pack                                    # 詰めの種類（1 語 = 1 << pack サンプル。0 = 詰めない）
        # 1 周期に複数のサンプルの構成の時刻の式の材料（lanes・divisor・latency。sample_time で使う。None = 1 周期 1 サンプル）
        self.fine_timing = fine_timing
        self.first_channel = first_channel                  # 値のビット i = ch first_channel + i（詰めた流れ）
        self._usage_prev: dict | None = None                # usage の前の読み（毎秒の量の差を取る）
        self._written_total = 0                             # la_written（32 ビットで折り返す）を伸ばした総数
        self.usage_history: list[dict] = []                 # usage を呼んだ結果の並び（古い順）
        self._splitter = SegmentSplitter() if events is not None else None
        if gaps and events is None:
            raise RuntimeError("欠けの区間の記録はイベントも流す構成だけ（欠けの記録はイベントの流れで届く）")
        if gaps and gap_hold_bytes is None:
            raise ValueError("gaps の流れは gap_hold_bytes（位置の決まらない拍を持つ上限。既定は持たない）を明示する")
        self._cursor = StreamCursor(rle, pack, rle_format)              # サンプルの位置の数え（ここ 1 か所。GapPlacer もこれを進める）
        self._placer = GapPlacer(rle, pack, hold_bytes=gap_hold_bytes, cursor=self._cursor) if gaps else None
        self._events_next, self._events_capacity = events if events is not None else (0, 0)
        self._events_received = 0
        # ケイパビリティは 1 回だけ読む（usage が毎回読み直すと UDP で何往復もする）
        self._caps = dev.known_capabilities()
        self._line_time = self._caps.dap_line_time if events is not None else False
        self._la_beats = 0                                  # 受け取ったロジアナの拍（見出しの通し番号と照らす）
        self._received = 0                                  # 受け取ったバイト数
        self._pending = b""                                 # 拍の途中（次の受け取りとつなぐ）
        self._overrun = False
        self._started = time.monotonic()
        self._deadline = float("inf")                       # chunks が duration_s から決める。stop で早める
        self._elapsed: float | None = None
        self._relay: dict | None = None                     # 出し切った後の中継の状態（StreamStats.relay）
        # この流れの間の最大・止めた回数・欠けを測り始める
        self._buffer_start = _stream_buffer(dev, reset_peak=True) if buffer_start is _NOT_GIVEN else buffer_start
        # 1 回の見回りで読む CSR（status から stream_beats まで。gaps の流れは gaps まで）
        self._poll_last = g.LA_DEEP_GAPS if gaps else g.LA_DEEP_STREAM_BEATS
        self._poll_count = (self._poll_last - g.LA_DEEP_STATUS) // 4 + 1

    def chunks(self, duration_s: float, timeout_ms: int = 2000) -> Iterator[StreamChunk]:
        """duration_s 秒受け取ってから止め、残りを出し切るまでチャンクを返す。溢れたら（overrun）その時点で止める。
        途中で抜けると（呼び出し側が閉じる・GC）、機器の書き込みを止めて終わる（残りを読み捨てない: 次の流れの開始
        （DeepAnalyzer._discard_stream）が読み捨てる。閉じる側で時間のかかる読みをしない）。stop（別のスレッドから）で時間の前に
        終えられる（残りも返す）。誤りで抜けるときも書き込みを止める（止める書き込みの失敗は元の誤りに注として足す）。"""
        if self._elapsed is not None:
            raise RuntimeError("このストリームは終わっている")
        self._deadline = min(self._deadline, self._started + duration_s)      # 先に stop されていればそのまま
        running, last_read = True, time.monotonic()
        progress = last_read                                # 最後に進んだ（始めた・止めた・データが届いた）時刻
        completed = False
        try:
            while True:
                status, available, last_beat, reported = self._poll()
                self._overrun |= bool(status >> g.LA_DEEP_STATUS_OVERRUN[0] & 1)
                drained = not running and not status >> g.LA_DEEP_STATUS_READING[0] & 1
                if self._placer is not None:
                    yield from self._chunks_of(self._placer.release(self._placer.received_beats, reported, last_beat), ())
                if running and (self._overrun or time.monotonic() >= self._deadline):
                    self.dev.write32(g.LA_DEEP_CONTROL, 0)
                    running = False
                    progress = time.monotonic()
                # 足りなければ前の読みから READ_INTERVAL_S まで待つ（1 ms ごとに CSR を読み直すと、UDP のリンクでは
                # リンクと機器のロックをほぼ使い続け、ほかの道具が毎回 1 秒待った。2026-10-21 実機）。出し切りの間は短く
                wait = (self.POLL_S if not running else
                        max(self.POLL_S, self.READ_INTERVAL_S - (time.monotonic() - last_read)))
                if not available:
                    if drained:
                        yield from self._finish_gaps(reported, last_beat)   # 欠けの区間を記録する流れ: 最後に読んだ位置で出せる分
                        completed = True
                        return
                    if not running and time.monotonic() - progress > timeout_ms / 1000:
                        # 止めたのに、機器が読み出し中のまま、データも届かない（PS 直読みを提供できない中継の先など）。永久に待たない
                        raise TimeoutError(f"止めた後、機器が読み出し中のまま出し切らない（{timeout_ms} ms 進まない）")
                    time.sleep(wait)
                    continue
                if running and available < self.READ_MIN and time.monotonic() - last_read < self.READ_INTERVAL_S:
                    time.sleep(wait)
                    continue
                try:
                    data = self.dev.read_stream_some(min(available, self.READ_MAX), timeout_ms)
                except TimeoutError as e:
                    if drained:                             # 機器は出し切ったのに、書いたと言う量まで届かない
                        raise self._loss(f"機器は出し切ったが、書いたと言う量のうち {available} バイトが {timeout_ms} ms 届かない"
                                         "（リンクで欠けた）") from e
                    raise
                last_read = progress = time.monotonic()
                taken = self._take_all(data)
                self._check_link()
                yield from taken
        except GeneratorExit:                               # 呼び出し側が抜けた: 書き込みを止めて終わる（読み捨てない）
            if running:
                self.dev.write32(g.LA_DEEP_CONTROL, 0)
            raise
        except BaseException as error:
            if running:
                try:
                    self.dev.write32(g.LA_DEEP_CONTROL, 0)
                except Exception as cleanup:  # noqa: BLE001 - 元の誤りを隠さない（止める書き込みの失敗は注として足す）
                    error.add_note(f"止める書き込み（la_deep.control = 0）も失敗した: {type(cleanup).__name__}: {cleanup}")
            raise
        finally:
            self._elapsed = time.monotonic() - self._started
            if completed:                                   # 出し切ったときだけ中継の状態を問い合わせる（抜ける・誤りの後は待たない）
                get = getattr(self.dev, "relay_status", None)
                try:
                    self._relay = None if get is None else get()
                except OSError:
                    self._relay = None                      # リンクが壊れた後（記録は出し切った分まで）

    def stop(self) -> None:
        """時間の前に終える（別のスレッドから呼んでよい）。chunks は書き込みを止め、残りを出し切ってから終わる。"""
        self._deadline = float("-inf")

    @property
    def carries_events(self) -> bool:
        """イベントも流れている（イベントも流す構成）。"""
        return self._splitter is not None

    @property
    def events_next(self) -> int:
        """次に届くはずのイベントの番号（events.written で数える。ここの前までを受け取った）。"""
        return self._events_next

    @property
    def events_received(self) -> int:
        return self._events_received

    @property
    def gaps(self) -> tuple[StreamGap, ...]:
        """これまでに位置を決めた欠けの区間（gaps の流れ。古い順）。"""
        return tuple(self._placer.gaps) if self._placer is not None else ()

    def stats(self) -> StreamStats:
        elapsed = time.monotonic() - self._started if self._elapsed is None else self._elapsed
        return StreamStats(self._cursor.samples, self._received, elapsed, self.sample_period_s, self._overrun, self.link_buffer(),
                           rle_format=self.rle_format if self.rle else "raw", wire_bytes=self._cursor.bytes,
                           pad_bytes=self._cursor.pad_bytes,
                           gaps=self.gaps, unplaced_beats=self._placer.unplaced_beats if self._placer is not None else 0,
                           relay=self._relay,
                           gap_held_peak_bytes=self._placer.peak_held_bytes if self._placer is not None else 0,
                           control=self.control_counters())

    def control_counters(self) -> dict[str, int]:
        """この流れの間の CONTROL の数え（Device.control_counters の差。事実）。"""
        now = self.dev.control_counters()
        return {name: now[name] - self._control_start[name] for name in now}

    def link_buffer(self) -> StreamBuffer | None:
        """PC の受け取り済みで、まだ読んでいない量（リンクの StreamBuffer）。最大・止めた回数・時間・捨てた数・落ちた数は、この流れを
        始めてからの分。受け取り側に溜める上限の無い道（USB・偽の機器）では None。"""
        now = _stream_buffer(self.dev)
        start = self._buffer_start
        if now is None or start is None:
            return None
        return StreamBuffer(now.capacity_bytes, now.used_bytes, now.peak_bytes, now.full_count - start.full_count,
                            now.full_s - start.full_s, now.backpressure, now.dropped_frames - start.dropped_frames,
                            now.dropped_bytes - start.dropped_bytes, now.lost_frames - start.lost_frames)

    def _check_link(self) -> None:
        """リンクの欠けの数え（この流れの間の差）を見て、欠けた・欠けた可能性があれば StreamLinkLoss（クラスの説明）。"""
        if self._buffer_start is None:
            return
        buf = self.link_buffer()
        if buf.dropped_frames:
            raise self._loss(f"リンクが流れのフレームを {buf.dropped_frames} 個（{buf.dropped_bytes} バイト）捨てた"
                             f"（PC に溜める上限 {buf.capacity_bytes} バイトを超えた）", buf)
        if buf.lost_frames and self._splitter is None:
            raise self._loss(f"流れを運ぶリンクでフレームが {buf.lost_frames} 個以上落ちた・壊れた（どのチャンネルのものかは分からない。"
                             "この流れには番号が無いので、欠けていないと確かめられない）", buf)

    def _loss(self, message: str, buf: StreamBuffer | None = None) -> StreamLinkLoss:
        buf = buf if buf is not None else self.link_buffer()
        lost = {"received_bytes": self._received, "samples": self._cursor.samples}
        if buf is not None:
            lost.update(dropped_frames=buf.dropped_frames, dropped_bytes=buf.dropped_bytes, lost_frames=buf.lost_frames)
        return StreamLinkLoss(message, lost)

    def usage(self) -> dict:
        """流している間のリング（DDR）と帯域の使用量の事実（CSR la_deep.la_written・ring_unread・ring_peak・ring_beats・status を
        1 回ずつ読む。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §6）。判定はしない（止めるかは使う側が決める。
        本体も自動では止めない）。呼ぶたびに usage_history に足す。

        - ring_beats: リングの拍の数、ring_unread_beats: 書いた − 読んだ（約 16 周期ごとの値）、ring_peak_beats: 流れの開始からの最大
        - encoded_bytes: 符号のバイト数の総数（リングに書いたロジアナの拍 × 16）、received_bytes: PC が受け取ったバイト数（見出し・イベントを含む）
        - encoded_bytes_per_s・received_bytes_per_s: 前の usage から（最初は流れの開始から）の差 ÷ 時間（PC の時計）
        - overrun: 溢れて書き込みを止めた（それまでに届いた分は欠けの無い先頭の部分）
        回路がこの数えを持たない古い構成（ケイパビリティ la_stream_usage が無い）では、その欄を None にし unknown に名前を入れる。"""
        now = time.monotonic()
        out: dict = {"t_s": now - self._started, "received_bytes": self._received, "unknown": []}
        if self._caps.la_stream_usage:
            # status（0x708）から ring_peak（0x73C）までを 1 回で読む（番地の順に読むので値の順は個別の読みと同じ）
            words = self.dev.read(g.LA_DEEP_STATUS, (g.LA_DEEP_RING_PEAK - g.LA_DEEP_STATUS) // 4 + 1)
            at = lambda address: words[(address - g.LA_DEEP_STATUS) // 4]   # noqa: E731
            status, ring = at(g.LA_DEEP_STATUS), at(g.LA_DEEP_RING_BEATS)
            written, unread, peak = at(g.LA_DEEP_LA_WRITTEN), at(g.LA_DEEP_RING_UNREAD), at(g.LA_DEEP_RING_PEAK)
        else:
            written = unread = peak = ring = None
            out["unknown"] += ["ring_beats", "ring_unread_beats", "ring_peak_beats", "encoded_bytes", "encoded_bytes_per_s"]
            status = self.dev.read32(g.LA_DEEP_STATUS)
        out["overrun"] = bool(status >> g.LA_DEEP_STATUS_OVERRUN[0] & 1)
        if written is not None:
            prev_raw = self._usage_prev["written_raw"] if self._usage_prev else 0
            self._written_total += (written - prev_raw) % (1 << 32)
            out.update(ring_beats=ring, ring_unread_beats=unread, ring_peak_beats=peak,
                       encoded_bytes=self._written_total * BEAT_BYTES)
        else:
            out.update(ring_beats=None, ring_unread_beats=None, ring_peak_beats=None, encoded_bytes=None)
        prev = self._usage_prev or {"t_s": 0.0, "received_bytes": 0, "encoded_bytes": 0}
        dt = out["t_s"] - prev["t_s"]
        out["interval_s"] = dt
        out["received_bytes_per_s"] = (out["received_bytes"] - prev["received_bytes"]) / dt if dt > 0 else None
        out["encoded_bytes_per_s"] = ((out["encoded_bytes"] - (prev["encoded_bytes"] or 0)) / dt
                                      if dt > 0 and out["encoded_bytes"] is not None else None)
        self._usage_prev = {**out, "written_raw": written if written is not None else 0}
        self.usage_history.append(out)
        return out

    def sample_time(self, s: int, start_time: int | None = None) -> dict:
        """流れのサンプル s（先頭 = 0）の値に変わったピンの変化の時刻（細かいサンプルの辺の番号。1 辺 = 1 / la_sample_hz）。
        1 周期に複数のサンプルの構成だけ（fine_timing）。式は la_lane_encoder: n = start_time × lanes + s × divisor − latency
        （変化は辺 n − divisor + 1〜n の後のどこか）。start_time は CSR la_deep.start_time（None なら読む）。
        欠けの区間を記録する流れ（gaps）でも同じ式: s は欠けを数えた位置（StreamChunk.first・StreamGap.start。回路の符号器は欠けの
        間も止まらず、捨てたのは符号の後の拍なので、欠けの後のサンプルの番号は止めずに流した場合と同じ）。
        返す: {"edge": n, "edge_lo": 変化が起きえた最初の辺の後, "edge_hi": n, "edge_hz": la_sample_hz の辺の周波数}。"""
        if self.fine_timing is None:
            raise RuntimeError("この流れは 1 周期 1 サンプル（細かいサンプルの時刻の式は無い）")
        if start_time is None:
            if not self.dev.read32(g.LA_DEEP_INFO) >> g.LA_DEEP_INFO_START_VALID[0] & 1:
                raise RuntimeError("起点の時刻がまだ無い（流れの最初の語を符号にする前）")
            lo, hi = self.dev.read(g.LA_DEEP_START_TIME_LO, 2)
            start_time = hi << 32 | lo
        t = self.fine_timing
        n = start_time * t["lanes"] + s * t["divisor"] - t["latency"]
        return {"edge": n, "edge_lo": n - t["divisor"] + 1, "edge_hi": n, "edge_hz": round(1 / self.sample_period_s * t["divisor"])}

    def _poll(self) -> tuple[int, int, int, int]:
        """1 回の見回りの CSR（1 つの READ）: (status, 書かれたが受け取っていないバイト数, gap_last_beat, gaps)。
        stream_beats は 32 ビットで折り返す。gaps の流れでなければ後ろの 2 つは 0。"""
        words = self.dev.read(g.LA_DEEP_STATUS, self._poll_count, timeout_retries=self._control_read_retries)
        status, total = words[0], words[(g.LA_DEEP_STREAM_BEATS - g.LA_DEEP_STATUS) // 4]
        beats = (total - self._received // BEAT_BYTES) % (1 << 32)
        available = beats * BEAT_BYTES - self._received % BEAT_BYTES
        if self._placer is None:
            return status, available, 0, 0
        return status, available, words[(g.LA_DEEP_GAP_LAST_BEAT - g.LA_DEEP_STATUS) // 4], words[-1]

    def _take(self, data) -> StreamChunk | None:
        self._received += len(data)
        events: tuple[DapEvent, ...] = ()
        if self._splitter is not None:
            data, events = self._split(data)
            if not data:
                return (StreamChunk(b"", self.rle, self._cursor.position, self._cursor.previous, events, self.pack, count=0, rle_format=self.rle_format)
                        if events else None)
        else:
            data = self._whole_beats(data)
        if not data:
            return None
        first, previous, count = self._cursor.take(data)
        return StreamChunk(data, self.rle, first, previous, events, self.pack, count=count, rle_format=self.rle_format)

    def _whole_beats(self, data):
        """見出しの無い流れ: 拍の境に揃える（前の受け取りの拍の途中をつなぎ、末尾の拍の途中は次へ）。揃っていれば写さない。"""
        if self._pending:
            data = self._pending + bytes(data)
        whole = len(data) - len(data) % BEAT_BYTES
        if whole == len(data):
            self._pending = b""
            return data
        self._pending = bytes(data[whole:])
        return memoryview(data)[:whole] if whole else b""

    def _take_all(self, data) -> list[StreamChunk]:
        """受け取ったバイト列のチャンク（欠けの区間を記録する流れは、位置の決まった分を欠けで分けたもの。ほかは _take の 1 つ）。
        欠けの区間の流れの位置は、次の見回りの CSR（受け取った後に読む gap_last_beat・gaps）で決まる（chunks が release する）。"""
        if self._placer is None:
            chunk = self._take(data)
            return [] if chunk is None else [chunk]
        self._received += len(data)
        la, events = self._split(data)
        self._placer.add_beats(la)
        if not events:
            return []
        return [StreamChunk(b"", self.rle, self._cursor.position, self._cursor.previous, events, self.pack, count=0, rle_format=self.rle_format)]

    def _finish_gaps(self, reported: int, last_beat: int) -> list[StreamChunk]:
        if self._placer is None:
            return []
        return self._chunks_of(self._placer.finish(reported, last_beat), ())

    def _chunks_of(self, pieces: list[tuple], events: tuple[DapEvent, ...]) -> list[StreamChunk]:
        """GapPlacer の並び → チャンク。欠けは直後のチャンクの gap に載せる（欠けで終わるなら中身の無いチャンク）。
        イベントは最初のチャンクに載せる。"""
        out: list[StreamChunk] = []
        gap = None
        for piece in pieces:
            if piece[0] == "gap":
                if gap is not None:                         # 欠けが続く（同じ位置の続きの件）: 前の欠けを中身なしで出す
                    out.append(StreamChunk(b"", self.rle, gap.start + gap.samples, None, (), self.pack, gap, count=0, rle_format=self.rle_format))
                gap = piece[1]
                continue
            _, data, first, previous, count = piece
            out.append(StreamChunk(data, self.rle, first, previous, (), self.pack, gap, count=count, rle_format=self.rle_format))
            gap = None
        if gap is not None:
            out.append(StreamChunk(b"", self.rle, gap.start + gap.samples, None, (), self.pack, gap, count=0, rle_format=self.rle_format))
        if events:
            if out:
                out[0] = dataclasses.replace(out[0], events=events)
            else:
                out.append(StreamChunk(b"", self.rle, self._cursor.position, self._cursor.previous, events, self.pack, count=0, rle_format=self.rle_format))
        return out

    def _split(self, data) -> tuple[bytes, tuple[DapEvent, ...]]:
        """見出し付きの流れを、ロジアナの拍（つないだもの。かたまりが 1 つなら写さない）とイベントに分ける。通し番号を確かめる。"""
        la, events = [], []
        la_beats = self._la_beats
        for seg in self._splitter.feed(data):
            beats = len(seg.data) // BEAT_BYTES
            if seg.kind == g.STREAM_SEG_LA:
                if seg.seq != la_beats & 0xFFFFFFFF:
                    self._la_beats = la_beats
                    raise StreamFormatError(f"ロジアナの拍の番号が飛んだ: {seg.seq}（{la_beats} のはず）")
                la_beats += beats
                la.append(seg.data)
            else:
                expected = self._events_next % self._events_capacity
                if seg.seq != expected:
                    self._la_beats = la_beats
                    raise StreamFormatError(f"イベントの位置が飛んだ: {seg.seq}（{expected} のはず。"
                                            "流している間にイベント記録を始め直した?）")
                self._events_next += beats
                self._events_received += beats
                for e in decode_events(seg.data, self._line_time):
                    if self._placer is not None and e.kind == KIND_LA_GAP:
                        self._placer.add_record(e.address, e.data, e.time)   # ロジアナの欠けの記録（DAP のイベントではない）
                    else:
                        events.append(e)
        self._la_beats = la_beats
        if not la:
            return b"", tuple(events)
        return (la[0] if len(la) == 1 else b"".join(la)), tuple(events)


# PC の受け取り済みのストリームを溜める上限（EthLink.open_stream_tcp の buffer_bytes。Session.open_eth が渡す）。
# 根拠（docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 1.7.7）: 消費側（LaStream.chunks）は 1 回に最大 READ_MAX を
# 読む。その 1 回を処理している間に、次の 1 回分（READ_MAX）が届いて溜まれば、消費側が付いていける限り受け取りは止まらない
# （二重のバッファ）。それより多く溜めても、消費側が遅れている時に止まるのが遅れるだけで、溢れるかどうかは変わらない
# （遅れを溜める場所は機器の DDR のリング。溢れれば機器が overrun を記録する）。値は速さを左右するだけで、正しさは左右しない。
STREAM_BUFFER_BYTES = 2 * LaStream.READ_MAX


def _stream_buffer(dev, reset_peak: bool = False) -> StreamBuffer | None:
    """機器（Device）のリンクの StreamBuffer。測れない機器（USB・偽の機器）なら None。"""
    get = getattr(dev, "stream_buffer", None)
    return None if get is None else get(reset_peak=reset_peak)
