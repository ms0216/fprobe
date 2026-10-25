"""fprobe-mcp の入口（pyproject の scripts）と、Probe の旧い import 先。

本体の Probe は fprobe.probe、MCP の薄いクライアントは fprobe.mcp_client にある。ここはその 2 つを結ぶだけ
（`python -m fprobe.mcp_server` と、旧い import `from fprobe.mcp_server import Probe` のため）。

    fprobe-mcp [--serial <USB シリアル>]
    fprobe-mcp --port <COM ポート> [--baud <ボーレート>]     # SerialLink（UART のホストリンク）のとき
    fprobe-mcp --eth <IP アドレス>                          # EthLink（UDP のホストリンク）のとき
"""

from .mcp_client import main
from .probe import SOURCE_LIMIT, Probe
from .session import Session

__all__ = ["Probe", "SOURCE_LIMIT", "Session", "main"]

if __name__ == "__main__":
    main()
