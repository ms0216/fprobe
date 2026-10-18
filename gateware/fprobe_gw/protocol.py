"""CONTROL チャンネルのプロトコル定数（単一の定義元。VHDL とホストへ生成する）。

仕様: docs/superpowers/specs/2026-10-15-m3-control-plane-design.md §3
"""

__all__ = ["OP_EXCHANGE", "OP_READ", "OP_WRITE", "PROTOCOL_VERSION", "REQUEST_HEADER_BYTES", "RESPONSE_TRAILER_BYTES",
           "STATUS_BAD_LENGTH", "STATUS_BAD_OP", "STATUS_BUS_ERROR", "STATUS_OK"]

PROTOCOL_VERSION = 2          # 2: EXCHANGE を足した（1 の要求はそのまま使える）

OP_READ = 0x01
OP_WRITE = 0x02
OP_EXCHANGE = 0x03           # 同じ番地に語を書き、書くごとにその番地を読んで返す（番地は進めない）

STATUS_OK = 0
STATUS_BAD_OP = 1
STATUS_BAD_LENGTH = 2
STATUS_BUS_ERROR = 3

REQUEST_HEADER_BYTES = 8     # op, tag, count(2), addr(4)
RESPONSE_TRAILER_BYTES = 4   # 応答の末尾: tag, status, done(2)
