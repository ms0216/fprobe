"""EthLink: 論理チャンネルを UDP で運ぶホストリンク（Ethernet の PHY を持つボード用。PC は標準のソケットだけで使う）。

仕様: docs/superpowers/specs/2026-10-17-m11-ethernet-link-design.md §2

    UDP（FPGA のポート 63920）の payload: ch(1) | seq(1) | payload(0..512)

- チャンネルの意味と振り分けは frame_link.FrameLink（SerialLink と共通）。CRC は Ethernet の FCS が受け持つ。
- seq: FPGA → PC のデータグラムの通し番号（全チャンネル共通、8 ビット）。飛びを lost_frames に数える
  （UDP は落ちうる。バイト列のチャンネルの欠けを黙って見逃さないため）。PC → FPGA の seq は 0。
  lost_frames は下限: 8 ビットなので、続けて 256 個以上落ちると 256 の倍数の分は数えられない（ちょうど 256 の倍数なら飛びが
  見えない）。ストリーム（ch 4）は、LaStream が機器の書いた量（stream_beats）と受け取った量を出し切りの後に照らし、足りなければ
  誤りにする（欠けの数え漏れを黙って見逃さない）。どのチャンネルのデータグラムが落ちたかは分からないので、FrameLink.buffer の
  lost_frames には、UDP で受けているどのバイト列のチャンネルにもこの数を返す（そのチャンネルが欠けた可能性の事実）。
- まとめ送り（任意）: ボードが PYNQ-Z2 の PS の中継（host/pynq/fprobe-relay）のとき、PYNQ の UDP の送信は 1 パケットあたり
  約 35 µs（大きさによらない）で、512 バイトのフレームを 1 つずつだと約 8 MB/s で頭打ちになる。request_bundle(n) で
  「n バイトまで 1 つの UDP にまとめて」と頼むと、中継は byte チャンネル（SWO・UART0・STREAM）のフレームをまとめて送る:
      CH_BUNDLE(0x40) | seq（先頭のフレームの seq。中のフレームは連続） | { ch(1) | 長さ(2, LE) | payload } × n
  受け取りはいつでも解く。頼まなければ（既定）、ボードは従来のデータグラムだけを送る。ほかのボードは未知の ch を捨てる。
- FPGA は最後に受けた正しいデータグラムの送り主へ送るので、開いたら最初に空の CONTROL を 1 つ送って結びつける
  （FPGA 側は payload の無いフレームを捨てる）。
- 中継の状態（任意。relay_status）: PYNQ の中継に CH_RELAY_STATUS(0x41) | 番号 を送ると、中継が
      CH_RELAY_STATUS | 番号 | 版(u16) | zc_state(u8) | tcp_path(u8) | 数 n(u16) | u64 × n
  を返す（relay.h の relay_status_encode。値の名前は RELAY_STATUS_FIELDS の順）。TCP のストリームをゼロコピーの道と複写の道の
  どちらで送っているか、完了通知の copied・ENOBUFS 等の事実。番号は問い合わせごとに変え（1〜255）、同じ番号の返事だけを受ける
  （遅れて届いた前の問い合わせの返事を、次の問い合わせの返事にしない。中継の版 2）。PL のフレームの通し番号とは別（seq は数えない）。
  ほかのボードは返さない。版 1 の中継は番号を写さない（0）ので、返事は無いものとする。
"""

import socket
import threading
import time
from collections.abc import Mapping

from . import generated as g
from .frame_link import (CH_CONTROL, CH_DAP, CH_LINE_CODING, CH_STREAM, CH_SWO, CH_UART0, MAX_PAYLOAD,
                         FrameLink)

__all__ = ["CH_BUNDLE", "CH_CONTROL", "CH_DAP", "CH_LINE_CODING", "CH_RELAY_STATUS", "CH_STREAM", "CH_SWO", "CH_UART0",
           "DEFAULT_HOST", "BUNDLE_BYTES", "DEFAULT_PORT", "LINK_ETH", "MAX_PAYLOAD", "RELAY_STATUS_FIELDS", "TCP_PORT", "EthLink",
           "decode_relay_status", "relay_status_request"]

CH_BUNDLE = 0x40                                      # まとめ送りの要求と、まとめたデータグラムの ch（relay.h と同じ）
CH_RELAY_STATUS = 0x41                                # 中継の状態の問い合わせと返事（relay.h の RELAY_CH_STATUS）
# 中継の状態の値の名前（relay.h の RELAY_STATUS_FIELDS と同じ順。試験 test_pynq_relay_zc.py が照らす）。意味は relay.h
RELAY_STATUS_FIELDS = (
    "connections", "zc_copied_total", "tcp_bytes", "tcp_calls", "tcp_eagain", "copy_ring_full",
    "ps_bad_la_positions", "ps_bad_ev_positions", "pl_stale_writes",
    "zc_calls", "zc_completed", "zc_copied_calls", "zc_copied_bytes", "zc_unknown_completions", "zc_enobufs", "zc_eagain",
    "zc_bytes", "zc_bounce_bytes", "zc_chunks", "zc_latency_count", "zc_latency_sum_ns", "zc_latency_max_ns",
    "zc_outstanding_calls", "zc_outstanding_max", "zc_la_held_beats", "zc_la_held_max_beats", "zc_ev_held_beats",
    "zc_ev_held_max_beats", "zc_desc_full", "zc_calls_full", "zc_abandoned_chunks", "zc_abandoned_bytes")
# relay.h の RELAY_ZCS_*（/dev/fprobe_ring が無い・使える・copied でやめた・あるが使えない）と RELAY_PATH_*
RELAY_ZC_STATES = ("none", "ready", "stopped_copied", "unusable")
RELAY_TCP_PATHS = ("none", "copy", "zerocopy", "zerocopy_draining")
LINK_ETH = getattr(g, "LINK_ETH", 3)                 # ケイパビリティ host_link の値
DEFAULT_HOST = "192.168.77.2"                         # ボードの既定（boards/primer20k.py の eth）
DEFAULT_PORT = 63920
TCP_PORT = 63921                                      # PS 直読みのストリームを運ぶ中継の TCP の口（open_stream_tcp）
BUNDLE_BYTES = 1400                                   # まとめ送りの既定の上限。UDP の payload が 1472 以下なら IP の分割が起きない（余裕を見た値）
_TCP_RECV = 1 << 20                                    # TCP の 1 回の読みの上限（バイト）
_TCP_BATCH = 64 << 10                                  # 1 回の recv がこれより少なければ、_TCP_BATCH_WAIT_S 眠って、次はまとめて取る。PC の
_TCP_BATCH_WAIT_S = 0.001                              # 受信は 1 回約 8 KB・毎秒約 6000 回の recv で CPU を使っていた。1 ms 眠ると 1 回約 50 KB になり、
                                                       # 同じ 57 MB/s で Python の CPU が 12.5 s → 7.0 s（27 秒、PYNQ の sendbench を相手に実測 2026-10-31）。
                                                       # 受信バッファ 8 MB は 1 ms で埋まらない。ストリームの遅れは最大 1 ms
_MAX_DATAGRAM = 65535                                # まとめ送り（request_bundle）は 1 つの UDP が 2048 バイトを超えうる
_RCVBUF = 8 << 20                                     # ストリーム（〜11 MB/s）を受けきるための受信バッファ


def relay_status_request(token: int) -> bytes:
    """中継の状態の問い合わせ（番号 token は 1〜255。返事に写される）。"""
    if not 1 <= token <= 255:
        raise ValueError(f"問い合わせの番号が不正: {token}")
    return bytes([CH_RELAY_STATUS, token])


def decode_relay_status(data: bytes) -> dict:
    """中継の状態の返事（CH_RELAY_STATUS のデータグラム全体）を辞書にする（"token" = 写された問い合わせの番号）。知らない名前の値
    （新しい中継）は unknown_fields = {"field_<i>": 値} に残す（名前の決まった欄と混ぜない: 応答の形を閉じるため）。
    古い中継は値が少ない（RELAY_STATUS_FIELDS の後ろの名前の欄が無い）。形が崩れていれば ValueError。"""
    if len(data) < 8 or data[0] != CH_RELAY_STATUS:
        raise ValueError(f"中継の状態の返事の形でない（{len(data)} バイト）")
    version = int.from_bytes(data[2:4], "little")
    count = int.from_bytes(data[6:8], "little")
    if len(data) != 8 + 8 * count:
        raise ValueError(f"中継の状態の返事の長さが合わない（{len(data)} バイト、値 {count} 個）")
    state, path = data[4], data[5]
    out: dict = {"version": version, "token": data[1],
                 "zc_state": RELAY_ZC_STATES[state] if state < len(RELAY_ZC_STATES) else f"unknown_{state}",
                 "tcp_path": RELAY_TCP_PATHS[path] if path < len(RELAY_TCP_PATHS) else f"unknown_{path}"}
    unknown: dict[str, int] = {}
    for i in range(count):
        value = int.from_bytes(data[8 + 8 * i:16 + 8 * i], "little")
        if i < len(RELAY_STATUS_FIELDS):
            out[RELAY_STATUS_FIELDS[i]] = value
        else:
            unknown[f"field_{i}"] = value
    out["unknown_fields"] = unknown
    return out


class EthLink(FrameLink):
    """UDP 上のフレームの送受信。チャンネルの振り分けは FrameLink。"""

    name = "eth"
    dap_packet_count = 4          # FPGA の受信 FIFO（2048）に、流している 1 つの後ろの 3 つ（各 約 560 バイト）が収まる

    def __init__(self, sock: socket.socket, address: tuple[str, int], bundle_bytes: int = 0, *,
                 buffer_bytes: Mapping[int, int]):
        super().__init__(buffer_bytes=buffer_bytes)
        self._sock = sock
        self._address = address
        self.lost_frames = 0                          # seq の飛びから数えた、落ちたデータグラムの数（下限。モジュールの説明）
        self._seq: int | None = None
        self._tcp_sock: socket.socket | None = None
        self._tcp_thread: threading.Thread | None = None
        self._relay_status_lock = threading.Lock()     # 問い合わせは 1 つずつ
        self._relay_status_token = 0                   # 今の問い合わせの番号（0 = 問い合わせていない）
        self._relay_status_reply: bytes | None = None
        self._relay_status_event = threading.Event()
        self._start()
        try:
            self.send(CH_CONTROL, b"")                # 送信先を FPGA に知らせる
            if bundle_bytes:
                self.request_bundle(bundle_bytes)
        except BaseException:                         # 受信のスレッドとソケットを残さない（LAN が落ちていて送れない等）
            self.close()
            raise

    @classmethod
    def open(cls, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, bundle_bytes: int = 0, *,
             buffer_bytes: Mapping[int, int]) -> "EthLink":
        """buffer_bytes: バイト列のチャンネル（SWO・UART0・STREAM）ごとに PC に溜める上限（FrameLink。全部を明示する）。"""
        # connect() しない: connect した UDP ソケットは送り元 IP をその時点で固定する。ボードの書き込み直後など LAN の
        # リンクが落ちている間に開くと別のアダプタの IP が固定され、FPGA はそこへ返すので応答が PC に届かない
        # （2026-10-17 に実機で確認）。sendto なら送るたびに経路と送り元が決まる
        address = (socket.gethostbyname(host), port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _RCVBUF)
            sock.settimeout(0.05)
            sock.bind(("", 0))
        except BaseException:
            sock.close()
            raise
        return cls(sock, address, bundle_bytes, buffer_bytes=buffer_bytes)

    def open_stream_tcp(self, port: int = TCP_PORT, timeout_s: float = 2.0, *, buffer_bytes: int) -> None:
        """PS 直読みのストリーム（ch 4 のバイト列）を、UDP でなく TCP で受ける。中継の TCP の口につなぐ。つながっている間、中継は
        ストリームのバイト列を TCP だけで送る（制御・DAP は UDP のまま）。UDP は、NIC が毎秒 1 万数千個を超えるパケットを捨てる
        PC があり（2026-10-31 実測）、欠けると流れが壊れるため。TCP は欠けを再送し、流量も調整する。つなげなければ ConnectionError。

        buffer_bytes: 受け取って、まだ読まれていない量の上限（呼び手が決める。既定は持たない）。上限まで溜まると受信のスレッドは
        recv を止め、カーネルの受信バッファ（_RCVBUF）が埋まると TCP の窓で中継の送りが止まる。中継は DDR のリングを読めなくなり、
        溢れれば PL が overrun を記録する（黙って捨てない）。PC のメモリは buffer_bytes + _RCVBUF まで（受け取りはリングへ直接
        recv_into する。間の写しは無い）。溜まり具合は stream_buffer。Session.open_eth は la.STREAM_BUFFER_BYTES（根拠はそこ）を渡す。
        開いた後に UDP で届くストリームのフレームは、TCP のバイトの並びのどこにも入らないので捨てて数える（StreamBuffer.dropped_frames）。"""
        if self._tcp_sock is not None:
            raise RuntimeError("TCP のストリームは開いている")
        if buffer_bytes <= 0:
            raise ValueError(f"ストリームを溜める上限が不正: {buffer_bytes}")
        host = self._address[0]
        try:
            sock = socket.create_connection((host, port), timeout=timeout_s)
        except OSError as e:
            raise ConnectionError(f"TCP {host}:{port} につなげない（中継が PS 直読みの TCP に対応していない？）: {e}") from e
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _RCVBUF)
        sock.settimeout(0.05)
        self._set_backpressure(CH_STREAM, buffer_bytes)
        self._tcp_sock = sock
        self._tcp_thread = threading.Thread(target=self._tcp_rx_task, name="fprobe-eth-tcp-rx", daemon=True)
        self._tcp_thread.start()

    @property
    def stream_tcp_open(self) -> bool:
        """PS 直読みのストリームを TCP で受けている（open_stream_tcp 済みで、切れていない）。"""
        return self._tcp_sock is not None and self._stream_error is None

    @property
    def stream_tcp_opened(self) -> bool:
        """open_stream_tcp 済み（切れた後も True）。ch 4 の受け口の道（select_source）を選べるのはこの間。"""
        return self._tcp_sock is not None

    def _tcp_rx_task(self) -> None:
        sock = self._tcp_sock

        def recv(view: memoryview) -> int:
            try:
                return sock.recv_into(view)
            except socket.timeout:
                return -1

        try:
            while not self._stop.is_set():
                # リングの末尾の空きへ直接受け取る（上限まで溜まっていれば、読まれるまで受け取らない）
                size = self._receive_into(CH_STREAM, recv, _TCP_RECV)
                if size < 0:
                    continue                                      # 時間切れ（close を見る）か close
                if not size:
                    raise ConnectionError("TCP が中継から閉じられた")
                if size < _TCP_BATCH:                             # 少ししか届いていなければ、少し眠ってから取る（下の _TCP_BATCH）
                    time.sleep(_TCP_BATCH_WAIT_S)
        except (OSError, ConnectionError) as e:
            if self._stop.is_set():
                return
            with self._cond:
                self._stream_error = e
                self._chan_cond[CH_STREAM].notify_all()

    def request_bundle(self, max_bytes: int) -> None:
        """PYNQ の中継に、byte チャンネルのフレームを max_bytes までの 1 つの UDP にまとめて送るよう頼む（0 = まとめない）。"""
        if not 0 <= max_bytes <= 0xFFFF:
            raise ValueError(f"まとめる上限が不正: {max_bytes}")
        self.send(CH_BUNDLE, max_bytes.to_bytes(2, "little"))

    def relay_status(self, timeout_s: float = 0.5) -> dict | None:
        """PYNQ の中継の状態（decode_relay_status の辞書）。返事が timeout_s のうちに来なければ None（中継でない・古い中継）。
        zc_state・tcp_path（ゼロコピーの道か複写の道か）と、このつながりの数え（relay.h の RELAY_STATUS_FIELDS）。"""
        with self._relay_status_lock:
            token = self._relay_status_token % 255 + 1             # 1〜255 を順に
            with self._cond:
                self._relay_status_event.clear()
                self._relay_status_reply = None
                self._relay_status_token = token
            self._sock.sendto(relay_status_request(token), self._address)
            if not self._relay_status_event.wait(timeout_s):
                return None
            return decode_relay_status(self._relay_status_reply)

    def _write_frame(self, ch: int, payload: bytes) -> None:
        self._sock.sendto(bytes([ch, 0]) + payload, self._address)

    def _close_transport(self) -> None:
        if self._tcp_thread is not None:
            self._tcp_thread.join()                       # _stop が立っているので、recv の timeout（0.05 秒）の後で終わる
        if self._tcp_sock is not None:
            self._tcp_sock.close()
        self._sock.close()

    def _lost_frames(self, ch: int) -> int:
        return self.lost_frames + self.bad_frames

    def _rx_loop(self) -> None:
        buffer = bytearray(_MAX_DATAGRAM)                 # 使い回す（recvfrom は毎回 bytes を作る）。payload は memoryview で渡し、
        view = memoryview(buffer)                         # 写すのは振り分けの先（リング・パケット）の 1 回だけ
        while not self._stop.is_set():
            try:
                size, sender = self._sock.recvfrom_into(buffer)
            except socket.timeout:
                continue
            except ConnectionResetError:              # Windows: 相手のポートが閉じていた（ICMP 到達不能）
                continue
            if sender != self._address:
                continue                                  # ボード以外からのデータグラム
            if size < 2:
                self._count_bad()
                continue
            ch, seq = buffer[0], buffer[1]
            if ch == CH_BUNDLE:
                self._dispatch_bundle(seq, view[:size])
                continue
            if ch == CH_RELAY_STATUS:                     # 中継の返事（PL のフレームでない。seq は数えない）
                with self._cond:
                    if size >= 2 and view[1] == self._relay_status_token and not self._relay_status_event.is_set():
                        self._relay_status_reply = bytes(view[:size])     # 今の問い合わせの返事だけ（遅れた返事は捨てる）
                        self._relay_status_event.set()
                continue
            self._count_seq(seq)
            self._dispatch(ch, view[2:size])

    def _count_seq(self, seq: int) -> None:
        if self._seq is not None and seq != (self._seq + 1) & 0xFF:
            with self._cond:
                self.lost_frames += (seq - self._seq - 1) & 0xFF
        self._seq = seq

    def _dispatch_bundle(self, seq: int, data: memoryview) -> None:
        """まとめたデータグラムの中のフレームを順に配る。中のフレームの seq は seq から連続する。
        形が崩れていたら、そこまでのフレームは配り、残りを捨てて 1 つ数える。"""
        pos, end = 2, len(data)
        while pos < end:
            if pos + 3 > end:
                self._count_bad()
                return
            ch, length = data[pos], data[pos + 1] | data[pos + 2] << 8
            if pos + 3 + length > end:
                self._count_bad()
                return
            self._count_seq(seq)
            self._dispatch(ch, data[pos + 3:pos + 3 + length])
            seq = (seq + 1) & 0xFF
            pos += 3 + length
