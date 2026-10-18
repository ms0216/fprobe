"""EthLink: 論理チャンネルを UDP で運ぶホストリンク（Ethernet の PHY を持つボード用。PC は標準のソケットだけで使う）。

仕様: docs/superpowers/specs/2026-10-17-m11-ethernet-link-design.md §2

    UDP（FPGA のポート 63920）の payload: ch(1) | seq(1) | payload(0..512)

- チャンネルの意味と振り分けは frame_link.FrameLink（SerialLink と共通）。CRC は Ethernet の FCS が受け持つ。
- seq: FPGA → PC のデータグラムの通し番号（全チャンネル共通、8 ビット）。飛びを lost_frames に数える
  （UDP は落ちうる。バイト列のチャンネルの欠けを黙って見逃さないため）。PC → FPGA の seq は 0。
- FPGA は最後に受けた正しいデータグラムの送り主へ送るので、開いたら最初に空の CONTROL を 1 つ送って結びつける
  （FPGA 側は payload の無いフレームを捨てる）。
"""

import socket

from . import generated as g
from .frame_link import (CH_CONTROL, CH_DAP, CH_LINE_CODING, CH_STREAM, CH_SWO, CH_UART0, MAX_PAYLOAD,
                         FrameLink)

__all__ = ["CH_CONTROL", "CH_DAP", "CH_LINE_CODING", "CH_STREAM", "CH_SWO", "CH_UART0", "DEFAULT_HOST",
           "DEFAULT_PORT", "LINK_ETH", "MAX_PAYLOAD", "EthLink"]

LINK_ETH = getattr(g, "LINK_ETH", 3)                 # ケイパビリティ host_link の値
DEFAULT_HOST = "192.168.77.2"                         # ボードの既定（boards/primer20k.py の eth）
DEFAULT_PORT = 63920
_RCVBUF = 8 << 20                                     # ストリーム（〜11 MB/s）を受けきるための受信バッファ


class EthLink(FrameLink):
    """UDP 上のフレームの送受信。チャンネルの振り分けは FrameLink。"""

    name = "eth"
    dap_packet_count = 4          # FPGA の受信 FIFO（2048）に、流している 1 つの後ろの 3 つ（各 約 560 バイト）が収まる

    def __init__(self, sock: socket.socket, address: tuple[str, int]):
        super().__init__()
        self._sock = sock
        self._address = address
        self.lost_frames = 0                          # seq の飛びから数えた、落ちたデータグラムの数
        self._seq: int | None = None
        self._start()
        self.send(CH_CONTROL, b"")                    # 送信先を FPGA に知らせる

    @classmethod
    def open(cls, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> "EthLink":
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
        return cls(sock, address)

    def _write_frame(self, ch: int, payload: bytes) -> None:
        self._sock.sendto(bytes([ch, 0]) + payload, self._address)

    def _close_transport(self) -> None:
        self._sock.close()

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, sender = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except ConnectionResetError:              # Windows: 相手のポートが閉じていた（ICMP 到達不能）
                continue
            if sender != self._address:
                continue                                  # ボード以外からのデータグラム
            if len(data) < 2:
                self._count_bad()
                continue
            ch, seq = data[0], data[1]
            if self._seq is not None and seq != (self._seq + 1) & 0xFF:
                with self._cond:
                    self.lost_frames += (seq - self._seq - 1) & 0xFF
            self._seq = seq
            self._dispatch(ch, data[2:])
