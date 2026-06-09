#

from amaranth import *
from amaranth.lib.data import ArrayLayout, View

from transactron import TModule, Transaction, Method, def_method
from transactron.lib import PipelineBuilder

from .utils import assign_xvalue, oring_combiner, reg_chain

class TTLOutController(Elaboratable):
    def __init__(self, ttloutio, csr, *, delay=0):
        self.ttloutio = ttloutio
        self.csr = csr
        assert delay in (0, 1)
        self.delay = delay

        self.set_bank_inst = Method(i=[('bank', 3), ('value', 32)])

        for bank in range(8):
            setattr(self, f'set_bank_user{bank}', Method(i=[('value', 32)]))

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

        for bank in range(8):
            meth = getattr(self, f'set_bank_user{bank}')
            if bank * 32 >= nttls:
                @def_method(m, meth, singlecaller=True)
                def _(value):
                    pass
                continue
            ttl_bank_reg = ttl_banks[bank]

            ttl_bank_write_en = Signal()
            ttl_bank_write_data = Signal(32)

            with m.If(ttl_bank_write_en):
                m.d.sync += ttl_bank_reg.eq(ttl_bank_write_data)

            ttl_bank_write_en_in = Signal()
            ttl_bank_write_data_in = Signal(32)
            m.d.sync += ttl_bank_write_en_in.eq(0)
            assign_xvalue(m, ttl_bank_write_data_in)
            reg_chain(m, output=Cat(ttl_bank_write_en, ttl_bank_write_data),
                      input=Cat(ttl_bank_write_en_in, ttl_bank_write_data_in),
                      levels=2)
            @def_method(m, meth, singlecaller=True)
            def _(value):
                m.d.sync += [ttl_bank_write_en_in.eq(1),
                             ttl_bank_write_data_in.eq(value)]

        @def_method(m, self.set_bank_inst, combiner=oring_combiner, nonexclusive=True)
        def _(bank, value):
            m.d.sync += ttl_banks[bank].eq(value)

        return m
