#

from amaranth import *
from amaranth.lib.data import ArrayLayout, View
from amaranth.utils import ceil_log2

from transactron import TModule, Transaction, Method, def_method
from transactron.lib import PipelineBuilder

from .utils import assign_xvalue, oring_combiner

class TTLOutController(Elaboratable):
    def __init__(self, ttloutio, csr, *, delay=0):
        self.ttloutio = ttloutio
        self.csr = csr
        assert delay in (0, 1)
        self.delay = delay
        self.nttls = len(self.ttloutio.o)
        self.bank_width = ceil_log2(self.nttls) - 5

        self.set_bank_inst = Method(i=[('bank', self.bank_width), ('value', 32)])
        self.set_mask = Method(i=[('mask', self.nttls), ('value', self.nttls)])
        self.set_byte_user = Method(i=[('byte', 5), ('hi', 8), ('lo', 8)])

    def elaborate(self, plat):
        m = TModule()

        nttls = self.nttls
        nbanks = 2**self.bank_width
        full_nttls = nbanks * 32

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

        ttl_banks = View(ArrayLayout(unsigned(32), nbanks),
                         Cat(ttl_out, Signal(full_nttls - nttls)))
        m.d.comb += [self.ttloutio.oe.eq(1),
                     self.ttloutio.o.eq((csr_ttl_out | ttl_hi_mask) & ~ttl_lo_mask)]

        m.submodules.set_pipe = set_pipe = PipelineBuilder()
        set_en = Signal()
        set_byte = Signal(5, reset_less=True)
        set_hi = Signal(8, reset_less=True)
        set_lo = Signal(8, reset_less=True)
        m.d.sync += set_en.eq(0)
        assign_xvalue(m, Cat(set_byte, set_hi, set_lo))
        @def_method(m, self.set_byte_user, singlecaller=True)
        def _(byte, hi, lo):
            m.d.sync += [set_en.eq(1),
                         set_byte.eq(byte),
                         set_hi.eq(hi),
                         set_lo.eq(lo)]
        start_set = set_pipe.create_external(i=[('en', 1), ('byte', 5),
                                                ('hi', 8), ('lo', 8)], o=[])
        with Transaction().body(m):
            start_set(m, en=set_en, byte=set_byte, hi=set_hi, lo=set_lo)

        @set_pipe.stage(m)
        def _():
            pass

        @set_pipe.stage(m)
        def _():
            pass

        @set_pipe.stage(m, o=[('mask_hi', nttls), ('mask_lo', nttls)])
        def _(byte, hi, lo):
            mask_hi = Signal(nttls)
            mask_lo = Signal(nttls)
            lo_bytes = View(ArrayLayout(unsigned(8), nbanks * 4),
                            Cat(mask_lo, Signal(full_nttls - nttls)))
            hi_bytes = View(ArrayLayout(unsigned(8), nbanks * 4),
                            Cat(mask_hi, Signal(full_nttls - nttls)))
            m.d.top_comb += [hi_bytes[byte].eq(hi),
                             lo_bytes[byte].eq(lo)]
            return dict(mask_hi=mask_hi, mask_lo=mask_lo)

        @set_pipe.stage(m)
        def _(en, mask_hi, mask_lo):
            with m.If(en):
                m.d.sync += ttl_out.eq((ttl_out | mask_hi) & ~mask_lo)

        @def_method(m, self.set_bank_inst, combiner=oring_combiner, nonexclusive=True)
        def _(bank, value):
            m.d.sync += ttl_banks[bank].eq(value)

        @def_method(m, self.set_mask, singlecaller=True)
        def _(mask, value):
            m.d.sync += ttl_out.eq((ttl_out & ~mask) | value)

        return m
