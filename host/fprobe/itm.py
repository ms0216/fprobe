"""ITM / DWT のパケットの解読（SWO のバイト列 → 型付きのパケット）の窓口。

実体は itm_decode（パケットの型・ItmDecoder）と itm_stream（Timestamper・PortText）。呼ぶ側はこの名前（fprobe.itm）から使う。
根拠: ARMv7-M Architecture Reference Manual, Appendix D4「Debug ITM and DWT Packet Protocol」。
仕様: docs/superpowers/specs/2026-10-17-f1-rtt-itm-semihosting-design.md の 1 節。

    dec = ItmDecoder()                  # 受信の最初から来るなら synced=True（既定）。途中からなら False
    packets = dec.feed(chunk)           # chunk はどこで切れていてもよい
    lines = PortText().feed(packets)    # ポートごとの printf の行
    timed = Timestamper().feed(packets) # ローカル・グローバルのタイムスタンプを付ける
"""

from .itm_decode import (DataTraceAddress, DataTracePc, DataTraceValue, DecodeError, EventCounter, ExceptionAction,
                         ExceptionTrace, Extension, GlobalTimestamp1, GlobalTimestamp2, Instrumentation, ItmDecoder,
                         LocalTimestamp, Overflow, Packet, PcSample, Resync, Sync, TimestampRelation)
from .itm_stream import PortText, TextLine, TimedPacket, Timestamper

__all__ = ["DataTraceAddress", "DataTracePc", "DataTraceValue", "DecodeError", "EventCounter", "ExceptionAction",
           "ExceptionTrace", "Extension", "GlobalTimestamp1", "GlobalTimestamp2", "Instrumentation", "ItmDecoder",
           "LocalTimestamp", "Overflow", "Packet", "PcSample", "PortText", "Resync", "Sync", "TextLine", "TimedPacket",
           "TimestampRelation", "Timestamper"]
