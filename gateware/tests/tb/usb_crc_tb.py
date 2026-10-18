"""usb_crc_pkg を Python 参照モデルと照合する。"""

import random

import cocotb
from cocotb.triggers import Timer

from tests.models import usb_packets as up


@cocotb.test()
async def crc16_next_matches_model(dut):
    rng = random.Random(16)
    for _ in range(300):
        crc, byte = rng.randrange(1 << 16), rng.randrange(256)
        dut.crc_in.value = crc
        dut.byte_in.value = byte
        await Timer(1, unit="ns")
        assert int(dut.crc_out.value) == up.crc16_register(bytes([byte]), crc)


@cocotb.test()
async def crc16_chain_reaches_residual(dut):
    packet = up.data_packet(up.DATA0, bytes(range(37)))
    crc = 0xFFFF
    for byte in packet[1:]:
        dut.crc_in.value = crc
        dut.byte_in.value = byte
        await Timer(1, unit="ns")
        crc = int(dut.crc_out.value)
    assert crc == up.CRC16_RESIDUAL


@cocotb.test()
async def crc5_accepts_valid_and_rejects_corrupt_tokens(dut):
    rng = random.Random(5)
    for _ in range(200):
        addr, endp = rng.randrange(128), rng.randrange(16)
        packet = up.token(up.IN, addr, endp)
        field = packet[1] | (packet[2] << 8)
        dut.token_field.value = field
        await Timer(1, unit="ns")
        assert int(dut.crc5_ok.value) == 1
        dut.token_field.value = field ^ (1 << rng.randrange(16))   # 1 ビット誤り
        await Timer(1, unit="ns")
        assert int(dut.crc5_ok.value) == 0
