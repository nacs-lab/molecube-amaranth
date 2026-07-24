#

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amaranth.lib.fifo import SyncFIFOBuffered, FIFOInterface
from amaranth.lib.memory import Memory
from amaranth.lib.data import View

from amaranth_axi.adaptors import InAdaptor, OutAdaptor

from transactron import TModule, Transaction, Method, def_method

from .dds import SET_ARG as DDS_SET_ARG
from .inst_runner import SPI_DECODE0 as SPI_ARG
from .utils import oring_combiner

def _incr(signal, modulo):
    n = len(signal)
    plus1 = signal + 1
    assert len(plus1) == n + 1
    if modulo == 2 ** n:
        return plus1[:n], plus1[n],
    else:
        wrap = signal == modulo - 1
        return Mux(wrap, 0, signal + 1), wrap

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
            m.d.sync += produce.eq(_incr(produce, inner_depth)[0])

        m.d.comb += [
            r_port.addr.eq(consume),
            self.r_data.eq(r_port.data),
            r_port.en.eq(do_inner_read)
        ]
        with m.If(do_inner_read):
            m.d.sync += consume.eq(_incr(consume, inner_depth)[0])

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


class BufferedFifo(wiring.Component):
    def __init__(self, layout, depth):
        super().__init__(dict(
            full=Out(1),
            empty=Out(1),
            fifo_level=Out(range(depth)),
            input_level=Out(1),
            output_level=Out(1),
        ))
        self.depth = depth
        self.write = Method(i=layout)
        self.read = Method(o=layout)

    def elaborate(self, plat):
        m = TModule()

        layout = self.write.layout_in

        m.submodules.fifo = fifo = SyncFIFOBuffered(width=layout.size,
                                                    depth=self.depth)
        m.submodules.in_adaptor = in_adaptor = InAdaptor.from_signal(
            ready=fifo.r_en, valid=fifo.r_rdy, data=View(layout, fifo.r_data))
        m.submodules.out_adaptor = out_adaptor = OutAdaptor.from_signal(
            ready=fifo.w_rdy, valid=fifo.w_en, data=View(layout, fifo.w_data))

        @def_method(m, self.read)
        def _():
            return in_adaptor.input(m)

        @def_method(m, self.write)
        def _(arg):
            out_adaptor.output(m, arg)

        m.d.comb += [self.full.eq(~fifo.w_rdy),
                     self.empty.eq(~fifo.r_rdy),
                     self.fifo_level.eq(fifo.level),
                     self.input_level.eq(in_adaptor.LEVEL),
                     self.output_level.eq(out_adaptor.LEVEL)]

        return m


class UpsizeFifo(Elaboratable):
    def __init__(self, *, width_in, width_out, depth):
        assert width_out % width_in == 0
        assert width_out >= width_in

        self.width_in = width_in
        self.width_out = width_out
        self.n = width_out // width_in
        self.depth = depth

        self._layout_in = [('data', self.width_in)]
        self._layout_out = [('data', self.width_out)]
        self._fifo = BufferedFifo(self._layout_out, self.depth - 2)

        self.read = self._fifo.read
        self.write = Method(i=self._layout_in)

        for name in ('full', 'empty', 'fifo_level', 'input_level', 'output_level'):
            setattr(self, name, getattr(self._fifo, name))

    def elaborate(self, plat):
        m = TModule()

        m.submodules.fifo = fifo = self._fifo

        part_count = Signal(range(self.n))
        partial_data = Signal(self.width_in * (self.n - 1), reset_less=True)

        @def_method(m, self.write)
        def _(data):
            next_count, full = _incr(part_count, self.n)
            new_data = Cat(partial_data, data)
            m.d.sync += [partial_data.eq(new_data[self.width_in:]),
                         part_count.eq(next_count)]
            with m.If(full):
                fifo.write(m, new_data)

        return m


class CommandFifo(UpsizeFifo):
    def __init__(self, data_width, depth):
        UpsizeFifo.__init__(self, width_in=data_width, width_out=data_width * 2,
                            depth=depth)


class ResultFifo(Elaboratable):
    def __init__(self, data_width, depth):
        self.data_width = data_width
        self.depth = depth
        self._layout = [('data', self.data_width)]
        self.write = Method(i=self._layout)
        self.read = Method(o=self._layout)
        self.level = Signal(range(depth + 1))
        # The legacy API only expose 5 bits, we just need to make sure this number
        # is not zero if the fifo is not empty and that it's not more than the actual count
        self.user_level = Signal(5)

    def elaborate(self, plat):
        m = TModule()

        m.submodules.fifo = fifo = BufferedFifo([('data', self.data_width)],
                                                 self.depth - 2)

        # Only include the out adaptor one if the actual fifo is not empty
        # Otherwise we can't guarantee that
        # the user can actually read all of those out yet.
        m.d.comb += self.level.eq((fifo.fifo_level + fifo.input_level) +
                                  (fifo.output_level & ~fifo.empty))

        # Construct a fast and conservative result level for user API
        est_level = Signal.like(fifo.fifo_level)
        m.d.comb += est_level.eq((fifo.fifo_level + fifo.input_level) |
                                 (fifo.output_level & ~fifo.empty))

        user_len = len(self.user_level)
        user_level = est_level[:user_len]
        for i in range(user_len, len(est_level), user_len):
            user_level = user_level | est_level[i:i + user_len]
        m.d.comb += self.user_level.eq(user_level)

        @def_method(m, self.read)
        def _():
            read_trans = Transaction()
            with read_trans.body(m):
                res = fifo.read(m).data
            return Mux(read_trans.run, res, 0)

        @def_method(m, self.write, combiner=oring_combiner, nonexclusive=True)
        def _(data):
            with Transaction().body(m):
                fifo.write(m, data)

        return m


class DMACmdFifo(Elaboratable):
    # Start address has to be page (4k) aligned
    def __init__(self, *, addr_width=32, align_width=12):
        self.write = Method(i=[('addr', addr_width), ('blocks', 10), ('first', 1)])
        self.read = Method(o=[('addr', addr_width), ('blocks', 10), ('first', 1)])
        self.addr_width = addr_width
        self.align_width = align_width

    def elaborate(self, plat):
        m = TModule()

        m.submodules.fifo = fifo = BufferedFifo([('addr',
                                                 self.addr_width - self.align_width),
                                                 ('blocks', 10), ('first', 1)], 9)

        @def_method(m, self.write)
        def _(addr, blocks, first):
            fifo.write(m, addr=addr[self.align_width:], blocks=blocks, first=first)

        @def_method(m, self.read)
        def _():
            cmd = fifo.read(m)
            return dict(addr=Cat(C(0, self.align_width), cmd.addr),
                        blocks=cmd.blocks, first=cmd.first)

        return m


class Fifos(Elaboratable):
    def __init__(self, data_width, *, dma_addr_width=32, dma_align_width=12):
        self.cmd_fifo = CommandFifo(data_width, 4099)
        self.cmd2_fifo = CommandFifo(data_width, 19)
        self.spi_cmd_fifo = BufferedFifo(SPI_ARG, 7)
        self.dds0_cmd_fifo = BufferedFifo(DDS_SET_ARG, 35)
        self.dds1_cmd_fifo = BufferedFifo(DDS_SET_ARG, 35)
        self.result_fifo = ResultFifo(data_width, 515)
        self.dma_cmd_fifo = DMACmdFifo(addr_width=dma_addr_width,
                                       align_width=dma_align_width)

    def elaborate(self, plat):
        m = TModule()

        m.submodules.cmd_fifo = self.cmd_fifo
        m.submodules.cmd2_fifo = self.cmd2_fifo
        m.submodules.spi_cmd_fifo = self.spi_cmd_fifo
        m.submodules.dds0_cmd_fifo = self.dds0_cmd_fifo
        m.submodules.dds1_cmd_fifo = self.dds1_cmd_fifo
        m.submodules.result_fifo = self.result_fifo
        m.submodules.dma_cmd_fifo = self.dma_cmd_fifo

        return m
