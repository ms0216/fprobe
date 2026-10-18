"""fprobe ホストライブラリ。

    from fprobe import Device
    with Device.open() as dev:
        print(dev.capabilities())
        dev.write32(SYSTEM_SCRATCH, 0x1234)
"""

from .caps import Capabilities
from .control import ControlChannel, ControlError, ProtocolError
from .device import Device

__all__ = ["Capabilities", "ControlChannel", "ControlError", "Device", "ProtocolError"]
