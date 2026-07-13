#

from amaranth import *

from transactron import TModule, Transaction, Method, def_method

from .utils import assign_xvalue, oring_combiner

class ClockOutController(Elaboratable):
    def __init__(self, clockoutio, csr, *, div_width=8):
        self.clockoutio = clockoutio
        self.csr = csr
        self.div_width = div_width
        self.OFF = (1 << div_width) - 1
        self.set = Method(i=[('div', div_width)])

    def elaborate(self, plat):
        m = TModule()

        counter = Signal(self.div_width)
        divider = Signal(self.div_width, init=self.OFF)
        out = Signal(1)
        m.d.comb += [self.clockoutio.o.eq(out),
                     self.clockoutio.oe.eq(1)]

        with m.If(divider == self.OFF):
            assign_xvalue(m, counter)
            m.d.sync += out.eq(0)
        with m.Elif(counter == 0):
            m.d.sync += [counter.eq(divider),
                         out.eq(~out)]
        with m.Else():
            m.d.sync += counter.eq(counter - 1)

        # The only conflict we should have is potentially trying to
        # reset while running the sequence,
        # this can be supported by simplying or-ing the input to make sure
        # the divider is OFF.
        @def_method(m, self.set, combiner=oring_combiner, nonexclusive=True)
        def _(div):
            m.d.sync += [divider.eq(div),
                         self.csr.clockout_div.eq(div[-8:]),
                         counter.eq(div),
                         out.eq(0)]

        return m
