"""CONTROL チャンネル（バイトストリーム + last）の相手をする。要求は 1 メッセージずつ送り、応答を集める。

符号化・復号はホストライブラリ（fprobe.control）のものを使う。
"""

import random

from cocotb.triggers import ReadOnly, RisingEdge

from fprobe import generated as g
from fprobe.control import decode_response, encode_exchange, encode_read, encode_write


class ControlPort:
    def __init__(self, dut, *, request: str, response: str, rng: random.Random,
                 gap: float = 0.3, stall: float = 0.3):
        self.dut, self.rng, self.gap, self.stall = dut, rng, gap, stall
        self.req = {k: getattr(dut, f"{request}_{k}") for k in ("valid", "ready", "data", "last")}
        self.rsp = {k: getattr(dut, f"{response}_{k}") for k in ("valid", "ready", "data", "last")}
        self.req["valid"].value = 0
        self.req["data"].value = 0
        self.req["last"].value = 0
        self.rsp["ready"].value = 0
        self._tag = 0

    async def transact(self, message: bytes, *, timeout: int = 20000) -> bytes:
        """要求を送り（空きを挟む）、応答（out_last まで、出力を時々止める）を返す。"""
        clk = self.dut.clk
        received = bytearray()
        pending = list(message)
        for _ in range(timeout):
            # 送信側: 立ち上がり直後に値を変える
            if pending and self.rng.random() >= self.gap:
                self.req["valid"].value = 1
                self.req["data"].value = pending[0]
                self.req["last"].value = int(len(pending) == 1)
            else:
                self.req["valid"].value = 0
            self.rsp["ready"].value = int(self.rng.random() >= self.stall)
            await ReadOnly()
            sent = int(self.req["valid"].value) and int(self.req["ready"].value)
            got = int(self.rsp["valid"].value) and int(self.rsp["ready"].value)
            byte, last = int(self.rsp["data"].value), int(self.rsp["last"].value)
            await RisingEdge(clk)
            if sent:
                pending.pop(0)
            if got:
                received.append(byte)
                if last:
                    assert not pending, "要求を送り終える前に応答が終わった"
                    self.req["valid"].value = 0
                    self.rsp["ready"].value = 0
                    return bytes(received)
        raise AssertionError(f"応答が来ない（送信残り {len(pending)}、受信 {len(received)} バイト）")

    async def pipeline(self, messages: list[bytes], *, timeout: int = 50000) -> list[bytes]:
        """要求を応答を待たずに続けて送り（前の応答の受信中も）、応答をメッセージごとに返す。"""
        clk = self.dut.clk
        pending = [(byte, int(i == len(m) - 1)) for m in messages for i, byte in enumerate(m)]
        responses, current = [], bytearray()
        for _ in range(timeout):
            if pending and self.rng.random() >= self.gap:
                self.req["valid"].value = 1
                self.req["data"].value, self.req["last"].value = pending[0]
            else:
                self.req["valid"].value = 0
            self.rsp["ready"].value = int(self.rng.random() >= self.stall)
            await ReadOnly()
            sent = int(self.req["valid"].value) and int(self.req["ready"].value)
            got = int(self.rsp["valid"].value) and int(self.rsp["ready"].value)
            byte, last = int(self.rsp["data"].value), int(self.rsp["last"].value)
            await RisingEdge(clk)
            if sent:
                pending.pop(0)
            if got:
                current.append(byte)
                if last:
                    responses.append(bytes(current))
                    current = bytearray()
                    if len(responses) == len(messages):
                        self.req["valid"].value = 0
                        self.rsp["ready"].value = 0
                        return responses
        raise AssertionError(f"応答が揃わない（{len(responses)}/{len(messages)}）")

    def _next_tag(self) -> int:
        self._tag = (self._tag + 1) & 0xFF
        return self._tag

    async def read(self, address: int, count: int) -> tuple[int, int, list[int]]:
        """(status, done, words)。tag の一致も確かめる。"""
        tag = self._next_tag()
        rtag, status, done, words = decode_response(await self.transact(encode_read(tag, address, count)))
        assert rtag == tag and len(words) == done
        return status, done, words

    async def write(self, address: int, words: list[int]) -> tuple[int, int]:
        tag = self._next_tag()
        rtag, status, done, data = decode_response(await self.transact(encode_write(tag, address, words)))
        assert rtag == tag and data == []
        return status, done

    async def exchange(self, address: int, words: list[int]) -> tuple[int, int, list[int]]:
        """(status, done, 読んだ語)。EXCHANGE: 同じ番地に書き、書くごとに読む。"""
        tag = self._next_tag()
        rtag, status, done, data = decode_response(await self.transact(encode_exchange(tag, address, words)))
        assert rtag == tag and len(data) == done
        return status, done, data


__all__ = ["ControlPort", "g"]
