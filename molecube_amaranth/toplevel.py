#

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amaranth.lib.cdc import ResetSynchronizer
from amaranth_zynq.ps7 import PsZynq

from molecube_amaranth.controllers import IOController
from molecube_amaranth.csr import Registers
from molecube_amaranth.fifo import Fifos
from molecube_amaranth.inst_runner import InstRunner, InstDispatcher
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
        m.submodules.fifos = fifos = Fifos(32)

        m.submodules.pulseio = pulseio = PulseIO.from_config(plat, self.config)
        m.submodules.ioctrl = ioctrl = IOController(pulseio, regs, fifos,
                                                    clock_shift=self.config.CLOCK_SHIFT)
        m.submodules.controller = controller = ControlInterface(ps.MAXIGP0, regs, fifos,
                                                                ioctrl, prefix=0x7300_0000,
                                                                valid_width=9)
        m.submodules.inst_runner = inst_runner = InstRunner(
            pulseio, regs, fifos, ioctrl, clock_shift=self.config.CLOCK_SHIFT)
        m.submodules.inst_dispatcher = inst_dispatcher = InstDispatcher(
            regs, fifos)

        return m
