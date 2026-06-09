#

from amaranth import *
from amaranth.lib.data import Field, FlexibleLayout, View, StructLayout

from .clockout import ClockOutController
from .spi import SPIController
from .dds import DDSController
from .ttlout import TTLOutController

class IOController(Elaboratable):
    def __init__(self, pulseio, csr, fifos, *, clock_shift):
        self.clockout = ClockOutController(pulseio.clockout, div_width=8 + clock_shift)
        self.spi = SPIController(pulseio.spi, fifos.result_fifo, div_width=8 + clock_shift)
        self.dds0 = DDSController(pulseio.dds0, fifos.result_fifo, csr, bus_id=0)
        self.dds1 = DDSController(pulseio.dds1, fifos.result_fifo, csr, bus_id=1)
        self.ttlout = TTLOutController(pulseio.ttlout, csr,
                                       delay=1 if clock_shift == 0 else 0)

    def elaborate(self, plat):
        m = Module()
        m.submodules.clockout = self.clockout
        m.submodules.spi = self.spi
        m.submodules.dds0 = self.dds0
        m.submodules.dds1 = self.dds1
        m.submodules.ttlout = self.ttlout
        return m
