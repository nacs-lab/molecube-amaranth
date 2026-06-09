#

from amaranth import *
from amaranth.lib.data import ArrayLayout, View

from transactron import TModule, Transaction, Method, def_method
from transactron.lib import PipelineBuilder

from .utils import assign_xvalue, oring_combiner

class TTLOutController(Elaboratable):
    def __init__(self, ttloutio, csr, *, delay=0):
        self.ttloutio = ttloutio
        self.csr = csr
        assert delay in (0, 1)
        self.delay = delay
        self.set_bank_user = Method(i=[('bank', 3), ('value', 32)])
        self.set_bank_inst = Method(i=[('bank', 3), ('value', 32)])

    def elaborate(self, plat):
        m = TModule()

        nttls = len(self.ttloutio.o)

        ttl_hi_mask = Signal(nttls)
        ttl_lo_mask = Signal(nttls)
        m.d.sync += [ttl_hi_mask.eq(self.csr.ttl_hi_mask),
                     ttl_lo_mask.eq(self.csr.ttl_lo_mask)]

        csr_ttl_out = self.csr.ttl_out[:nttls]

        if self.delay == 0:
            ttl_out = csr_ttl_out
        else:
            assert self.delay == 1
            ttl_out = Signal(nttls)
            m.d.sync += csr_ttl_out.eq(ttl_out)

        ttl_banks = View(ArrayLayout(unsigned(32), 8), Cat(ttl_out, Signal(256 - nttls)))
        m.d.comb += [self.ttloutio.oe.eq(1),
                     self.ttloutio.o.eq((csr_ttl_out | ttl_hi_mask) & ~ttl_lo_mask)]

        m.submodules.set_pipe = set_pipe = PipelineBuilder()
        start_set_bank = set_pipe.create_external(i=[('bank', 3), ('value', 32)], o=[])
        @def_method(m, self.set_bank_user, singlecaller=True)
        def _(bank, value):
            start_set_bank(m, bank=bank, value=value)

        @set_pipe.stage(m)
        def _():
            pass

        @set_pipe.stage(m)
        def _(bank, value):
            m.d.sync += ttl_banks[bank].eq(value)

        @def_method(m, self.set_bank_inst, combiner=oring_combiner, nonexclusive=True)
        def _(bank, value):
            m.d.sync += ttl_banks[bank].eq(value)

        return m
