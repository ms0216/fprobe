"""ターゲット側のバッファの吸い出し（U43。STM32CubeMonitor の Snapshot 方式 CUBE-12、µC/Probe のターゲットで同期した
取得 UCP-03 に相当）。機器に依らない純粋な部品（メモリの読みは関数で受け取る）。

ターゲットのファームが制御ループ等の中で配列に値（構造体でもよい）を書き、書いた数を数える:

    struct Sample log_buf[256];  uint32_t log_count;
    log_buf[log_count % 256] = s;  log_count++;          // 要素を書いてから数を進める

PC は数を読み、前回から増えた分の要素だけを読んで DWARF の要素の型で解釈する。デバッグポートで周期的に読むより
速い変化を取りこぼさず、ターゲットの時刻（制御ループ）に同期した値が得られる。
- counter = True（既定）: head は書いた総数（単調に増え、型の幅で折り返す）。PC が読む前に一周されたら lost に数える。
  要素を読んだ後に数を読み直し、読んでいる間に上書きされたかもしれない要素は捨てて lost に数える。
- counter = False: head は次に書く位置（0〜長さ − 1）。一周されたことは分からない（読みの周期を十分速くする）。
"""

from dataclasses import dataclass
from typing import Callable

from .dwarf.typesys import CType
from .dwarf.values import decode

__all__ = ["TargetRing"]


@dataclass
class TargetRing:
    element: CType
    base: int                     # 配列の先頭の番地
    length: int                   # 要素の数
    head_address: int
    head_size: int                # head の大きさ（バイト）
    counter: bool = True
    seq: int | None = None        # 次に読む要素の通し番号（最初の poll で決める）
    lost: int = 0
    _head: int | None = None      # 前回読んだ head

    @property
    def stride(self) -> int:
        return self.element.size

    def _read_head(self, read: Callable[[int, int], bytes]) -> int:
        return int.from_bytes(read(self.head_address, self.head_size), "little")

    def poll(self, read: Callable[[int, int], bytes]) -> list[tuple[int, dict]]:
        """前回から増えた要素を (通し番号, 値) の並びで返す。最初の poll は位置を合わせるだけ（それより前は読まない）。"""
        head = self._read_head(read)
        if self._head is None:
            self._head, self.seq = head, 0
            return []
        if self.counter:
            new = (head - self._head) % (1 << (8 * self.head_size))
        else:
            if head >= self.length:
                raise ValueError(f"書く位置 {head} が配列の長さ {self.length} 以上（counter = True の数ではないか）")
            new = (head - self._head) % self.length
        self._head = head
        if new == 0:
            return []
        if new > self.length:                                   # 読む前に一周された
            self.lost += new - self.length
            self.seq += new - self.length
            new = self.length
        first = (head - new) % self.length if not self.counter else (head - new) % (1 << (8 * self.head_size))
        index = first % self.length
        data = self._read_elements(read, index, new)
        if self.counter:                                       # 読んでいる間に追いつかれた分は信用しない
            after = self._read_head(read)
            ahead = (after - first) % (1 << (8 * self.head_size)) - self.length
            if ahead > 0:
                skip = min(ahead, new)
                self.lost += skip
                data = data[skip * self.stride:]
                self.seq += skip
                new -= skip
        out = []
        for k in range(new):
            out.append((self.seq + k, decode(self.element, data[k * self.stride:(k + 1) * self.stride])))
        self.seq += new
        return out

    def _read_elements(self, read: Callable[[int, int], bytes], index: int, count: int) -> bytes:
        first = min(count, self.length - index)
        data = read(self.base + index * self.stride, first * self.stride)
        if count > first:
            data += read(self.base, (count - first) * self.stride)
        return data
