"""CSR 定義とプロトコル定数から、ホストライブラリ用の Python モジュール（host/fprobe/generated.py）を生成する。"""

from .. import protocol
from .model import CsrMap

__all__ = ["render_host_module"]


def _value(name: str, value: int | str) -> str:
    if isinstance(value, str):
        return repr(value)
    if name.endswith(("_VID", "_PID")):
        return f"0x{value:04X}"
    return f"0x{value:X}" if value >= 0x100 else str(value)


def render_host_module(csr_map: CsrMap, sections: dict[str, dict[str, int | str]]) -> str:
    """sections: 見出し → 定数（CSR とプロトコルの後に並べる。USB 識別子・ケイパビリティ等）。"""
    lines = [
        '"""自動生成ファイル。直接編集しないこと（ゲートウェア側で python -m fprobe_gw gen を実行して再生成）。',
        "",
        "生成元: gateware/fprobe_gw/protocol.py, csr/fprobe_map.py, caps.py, usb/fprobe_device.py",
        '"""',
        "",
        "# CONTROL プロトコル",
        f"PROTOCOL_VERSION = {protocol.PROTOCOL_VERSION}",
        f"OP_READ = 0x{protocol.OP_READ:02X}",
        f"OP_WRITE = 0x{protocol.OP_WRITE:02X}",
        f"OP_EXCHANGE = 0x{protocol.OP_EXCHANGE:02X}",
        f"STATUS_OK = {protocol.STATUS_OK}",
        f"STATUS_BAD_OP = {protocol.STATUS_BAD_OP}",
        f"STATUS_BAD_LENGTH = {protocol.STATUS_BAD_LENGTH}",
        f"STATUS_BUS_ERROR = {protocol.STATUS_BUS_ERROR}",
        f"REQUEST_HEADER_BYTES = {protocol.REQUEST_HEADER_BYTES}",
        f"RESPONSE_TRAILER_BYTES = {protocol.RESPONSE_TRAILER_BYTES}",
        "",
        "# CSR（バイトアドレス）。<BLOCK>_BASE / <BLOCK>_SIZE、<BLOCK>_<REG>、",
        "# 複数フィールドのレジスタは <BLOCK>_<REG>_<FIELD> = (lsb, width)、CONST は <BLOCK>_<REG>_VALUE",
    ]
    for block in csr_map.blocks:
        prefix = block.name.upper()
        lines += ["", f"# {block.name}: {block.doc}",
                  f"{prefix}_BASE = 0x{block.base:08X}", f"{prefix}_SIZE = 0x{block.size:X}"]
        for reg in block.registers:
            name = f"{prefix}_{reg.name.upper()}"
            lines.append(f"{name} = 0x{block.base + reg.offset:08X}")
            for f in reg.fields:
                if not reg.single:
                    lines.append(f"{name}_{f.name.upper()} = ({f.lsb}, {f.width})")
            if reg.single and reg.fields[0].access == "CONST":
                lines.append(f"{name}_VALUE = 0x{reg.fields[0].value:08X}")
    for title, constants in sections.items():
        lines += ["", f"# {title}", *[f"{name} = {_value(name, value)}" for name, value in constants.items()]]
    lines.append("")
    return "\n".join(lines)
