"""fprobe-mcp を stdio で起動し、AI エージェントと同じ経路で一連のデバッグ作業ができることを確かめる（M10）。

接続 → RAM の読み書き → レジスタ・ステップ → ロジアナを仕掛けてメモリを読み、その SWD 通信を捕捉 →
番地一致でロジアナをトリガ（クロストリガ）→ 再開・切断。
Flash には触れない（書き込みは test_dap_hw で確認済み）。
"""

import asyncio
import json
import sys

import pytest

pytest.importorskip("mcp")
from mcp import Client, StdioServerParameters  # noqa: E402

from tests.hil.program import program  # noqa: E402
from fprobe_gw.boards.primer20k import PRIMER20K  # noqa: E402
from fprobe_gw.usb.fprobe_device import STRINGS, STR_SERIAL  # noqa: E402

pytestmark = pytest.mark.hil

# 既定のポート（7820）は画面の開発で別のデーモン（偽の機器等）が使っていることがある: 試験専用のポートで自分のデーモンを起こす
SERVER = StdioServerParameters(command=sys.executable,
                               args=["-m", "fprobe.mcp_server", "--serial", STRINGS[STR_SERIAL - 1], "--listen", "7891"])
SRAM = 0x20004000
PROBE_DRIVES_SWDIO = 8


@pytest.fixture(scope="module")
def programmed():
    program("fprobe")


async def _session(steps):
    async with Client(SERVER) as client:
        async def call(name, **arguments):
            result = await client.call_tool(name, arguments)
            assert not result.is_error, (name, result.content)
            # 戻り値が dict なら本文が JSON、それ以外は structured_content = {"result": 値}
            structured = result.structured_content
            if structured is None:
                if not result.content:
                    return None                                      # 戻り値なしの道具
                text = result.content[0].text
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text                                      # 文字列の戻り値（16 進のバイト列等）
            return structured["result"] if structured.keys() == {"result"} else structured
        return await steps(client, call)


def test_debug_session_through_mcp(programmed):
    async def steps(client, call):
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"target_connect", "target_memory_read", "la_capture", "uart_write", "xtrig_route", "events_read", "xtrig_set_sequence"} <= names

        info = await call("probe_info")
        assert info["capabilities"]["la_channels"] == 16

        status = await call("target_connect", target="stm32f401xe", frequency_hz=4_000_000)
        assert status["state"] == "HALTED"

        await call("events_start")
        pattern = bytes(range(64)).hex()
        await call("target_memory_write", address=SRAM, data_hex=pattern)
        assert await call("target_memory_read", address=SRAM, length=64) == pattern
        assert (await call("events_stop"))["lost"] == 0
        events = await call("events_read", last=1000)
        assert any(e["kind"] == "ap_write" and e["address"] == f"0x{SRAM:08X}" and e["value"] == "0x03020100"
                   for e in events["items"])

        regs = await call("target_registers_read")
        assert regs["pc"] == status["pc"]
        stepped = await call("target_step")
        assert stepped["state"] == "HALTED" and stepped["pc"] != status["pc"]

        await call("la_arm", rate_hz=25e6, pretrigger=512, fall=1 << PROBE_DRIVES_SWDIO)
        assert await call("target_memory_read", address=0xE000ED00, length=4) == "41c20f41"
        cap = await call("la_collect")
        swclk = cap["channels"][str(PRIMER20K.debug_connector["swclk"])]
        assert swclk["edges"] >= 80 and str(PROBE_DRIVES_SWDIO) in cap["channels"]

        # クロストリガ: デバッガがこの番地に書いたらロジアナをトリガ（ch15 は常に 0 なので値一致だけでは成立しない）
        await call("xtrig_set_match", address=SRAM + 4)
        await call("xtrig_route", to_la=["dap_match"])
        await call("la_arm", rate_hz=25e6, pretrigger=512, mask=1 << 15, value=1 << 15)
        await call("target_memory_write", address=SRAM + 4, data_hex="78563412")
        cap = await call("la_collect", timeout_s=2)
        assert cap["trigger_index"] == 512
        assert (await call("xtrig_status"))["match"]["count"] >= 1
        await call("xtrig_route")

        # 多段トリガ: ホストの指令 2 回で 2 段を進めて発火
        await call("xtrig_set_sequence", stages=[{"when": ["host"]}, {"when": ["host"], "within_us": 1e6}])
        await call("xtrig_fire")
        status = await call("xtrig_sequence_status")
        assert (status["stage"], status["fired"]) == (1, False)
        await call("xtrig_fire")
        assert (await call("xtrig_sequence_status"))["fired"] is True

        assert (await call("target_resume"))["state"] == "RUNNING"
        await call("target_disconnect")

    asyncio.run(_session(steps))
