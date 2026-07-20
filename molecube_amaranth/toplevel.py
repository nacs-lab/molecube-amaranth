#

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amaranth.lib.cdc import ResetSynchronizer
from amaranth_zynq.ps7 import PsZynq

from molecube_amaranth.controllers import IOController
from molecube_amaranth.csr import Registers
from molecube_amaranth.fifo import Fifos
from molecube_amaranth.inst_runner import InstRunner
from molecube_amaranth.interface import ControlInterface
from molecube_amaranth.io import PulseIO

class TopLevel(Elaboratable):
    def __init__(self, config):
        self.config = config

    def elaborate(self, plat):
        m = Module()

        m.submodules.ps = ps = PsZynq()

        # Clock
        m.domains += ClockDomain('sync')
        clk = ps.get_clock_signal(0, self.config.CLOCK_HZ)
        m.d.comb += ClockSignal().eq(clk)
        m.d.comb += ps.MAXIGP0ACLK.eq(clk)

        # Reset
        reset = ps.get_reset_signal(0)
        reset_sync = ResetSynchronizer(reset, domain="sync")
        m.submodules.reset_sync = reset_sync

        m.submodules.regs = regs = Registers(self.config)
        fifos = None
        ioctrl = None
        m.submodules.controller = controller = ControlInterface(ps.MAXIGP0, regs, fifos,
                                                                ioctrl, prefix=0x7300_0000,
                                                                valid_width=9)

        return m
