"""fprobe.raw_dap.ack_of: pyOCD の転送の誤りを ACK に。分からないものを NO_ACK と名付けない（?55）。"""

from pyocd.probe.pydapaccess.dap_access_api import DAPAccessIntf

from fprobe.raw_dap import Ack, ack_of


def test_only_a_reported_no_ack_is_no_ack():
    assert ack_of(DAPAccessIntf.TransferError("SWD/JTAG communication failure (No ACK); check")) == Ack.NO_ACK
    assert ack_of(DAPAccessIntf.TransferError("SWD/JTAG communication failure (Unexpected ACK '5')")) == Ack.OTHER
    assert ack_of(DAPAccessIntf.TransferError()) == Ack.OTHER
    assert ack_of(DAPAccessIntf.TransferFaultError("x")) == Ack.FAULT
    assert ack_of(DAPAccessIntf.TransferTimeoutError("x")) == Ack.WAIT
