#

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amaranth.lib.fifo import SyncFIFOBuffered, FIFOInterface
from amaranth.lib.memory import Memory

from amaranth_axi.adaptors import InAdaptor, OutAdaptor

from transactron import TModule, Transaction, Method, def_method

from .utils import oring_combiner

def _incr(signal, modulo):
    if modulo == 2 ** len(signal):
        return signal + 1
    else:
        return Mux(signal == modulo - 1, 0, signal + 1)

# This is mostly a copy of the SyncFIFOBuffered in base amaranth
# However, the computation of the w_rdy signal is changed to be fully registered
# to reduce the cost on the writer side
class SyncFIFOBuffered(Elaboratable, FIFOInterface):
    def __init__(self, *, width, depth):
        super().__init__(width=width, depth=depth)

        self.level = Signal(range(depth + 1))

    def elaborate(self, platform):
        m = Module()
        assert self.depth > 1

        do_write = self.w_rdy & self.w_en
        do_read = self.r_rdy & self.r_en

        m.d.comb += [
            self.w_level.eq(self.level),
            self.r_level.eq(self.level),
        ]

        inner_depth = self.depth - 1
        inner_level = Signal(range(inner_depth + 1))
        inner_r_rdy = Signal()

        w_rdy = Signal(1, init=1)

        m.d.comb += [
            self.w_rdy.eq(w_rdy),
            inner_r_rdy.eq(inner_level != 0),
        ]
        if platform is None:
            m.d.sync += Assert(w_rdy == (inner_level != inner_depth))

        do_inner_read  = inner_r_rdy & (~self.r_rdy | self.r_en)

        storage = m.submodules.storage = Memory(shape=self.width, depth=inner_depth, init=[])
        w_port  = storage.write_port()
        r_port  = storage.read_port(domain="sync")
        produce = Signal(range(inner_depth))
        consume = Signal(range(inner_depth))

        m.d.comb += [
            w_port.addr.eq(produce),
            w_port.data.eq(self.w_data),
            w_port.en.eq(do_write),
        ]
        with m.If(do_write):
            m.d.sync += produce.eq(_incr(produce, inner_depth))

        m.d.comb += [
            r_port.addr.eq(consume),
            self.r_data.eq(r_port.data),
            r_port.en.eq(do_inner_read)
        ]
        with m.If(do_inner_read):
            m.d.sync += consume.eq(_incr(consume, inner_depth))

        w_rdy_topbit = inner_depth == 2 ** (len(inner_level) - 1)
        if w_rdy_topbit:
            m.d.comb += w_rdy.eq(~inner_level[-1])

        with m.If(do_write & ~do_inner_read):
            if not w_rdy_topbit:
                m.d.sync += w_rdy.eq(inner_level != inner_depth - 1)
            m.d.sync += inner_level.eq(inner_level + 1)
        with m.If(do_inner_read & ~do_write):
            if not w_rdy_topbit:
                m.d.sync += w_rdy.eq(1)
            m.d.sync += inner_level.eq(inner_level - 1)

        with m.If(do_inner_read):
            m.d.sync += self.r_rdy.eq(1)
        with m.Elif(self.r_en):
            m.d.sync += self.r_rdy.eq(0)

        m.d.comb += [
            self.level.eq(inner_level + self.r_rdy),
        ]

        return m


class CommandFifo(wiring.Component):
    full: Out(1)
    def __init__(self, data_width, depth):
        super().__init__()
        self.data_width = data_width
        self.depth = depth
        self._layout_in = [('data', self.data_width)]
        self._layout_out = [('data', self.data_width * 2)]
        self.write = Method(i=self._layout_in)
        self.read = Method(o=self._layout_out)

    def elaborate(self, plat):
        m = TModule()

        m.submodules.fifo = fifo = SyncFIFOBuffered(width=self.data_width * 2,
                                                    depth=self.depth - 2)
        m.submodules.in_adaptor = in_adaptor = InAdaptor.from_signal(
            ready=fifo.r_en, valid=fifo.r_rdy, data=fifo.r_data)
        m.submodules.out_adaptor = out_adaptor = OutAdaptor.from_signal(
            ready=fifo.w_rdy, valid=fifo.w_en, data=fifo.w_data)

        @def_method(m, self.read)
        def _():
            return in_adaptor.input(m).DATA

        has_half = Signal(1)
        half_data = Signal(self.data_width)

        @def_method(m, self.write)
        def _(data):
            m.d.sync += half_data.eq(data)
            with m.If(has_half):
                m.d.sync += has_half.eq(0)
                out_adaptor.output(m, Cat(half_data, data))
            with m.Else():
                m.d.sync += has_half.eq(1)

        m.d.comb += self.full.eq(~fifo.w_rdy)

        return m


class ResultFifo(Elaboratable):
    def __init__(self, data_width, depth):
        self.data_width = data_width
        self.depth = depth
        self._layout = [('data', self.data_width)]
        self.write = Method(i=self._layout)
        self.read = Method(o=self._layout)
        self.level = Signal(range(depth + 1))

    def elaborate(self, plat):
        m = TModule()

        m.submodules.fifo = fifo = SyncFIFOBuffered(width=self.data_width,
                                                    depth=self.depth - 2)
        m.submodules.in_adaptor = in_adaptor = InAdaptor.from_signal(
            ready=fifo.r_en, valid=fifo.r_rdy, data=fifo.r_data)
        m.submodules.out_adaptor = out_adaptor = OutAdaptor.from_signal(
            ready=fifo.w_rdy, valid=fifo.w_en, data=fifo.w_data)

        # Only include the out adaptor one if the actual fifo is not empty
        # Otherwise we can't guarantee that
        # the user can actually read all of those out yet.
        m.d.comb += self.level.eq(fifo.level + in_adaptor.LEVEL +
                                  (out_adaptor.LEVEL & fifo.r_rdy))

        @def_method(m, self.read)
        def _():
            read_trans = Transaction()
            with read_trans.body(m):
                res = in_adaptor.input(m).DATA
            return Mux(read_trans.run, res, 0)

        @def_method(m, self.write, combiner=oring_combiner, nonexclusive=True)
        def _(data):
            with Transaction().body(m):
                out_adaptor.output(m, data)

        return m


class Fifos(Elaboratable):
    def __init__(self, data_width):
        self.cmd_fifo = CommandFifo(data_width, 4099)
        self.result_fifo = ResultFifo(data_width, 515)

    def elaborate(self, plat):
        m = TModule()

        m.submodules.cmd_fifo = self.cmd_fifo
        m.submodules.result_fifo = self.result_fifo

        return m
