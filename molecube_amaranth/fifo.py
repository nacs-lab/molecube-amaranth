#

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amaranth.lib.memory import Memory
from amaranth.lib.data import View

from amaranth_axi.adaptors import InAdaptor, OutAdaptor

from transactron import TModule, Transaction, Method, def_method
from transactron.lib import PipelineBuilder

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


class BufferedFifo(wiring.Component):
    def __init__(self, layout, depth, *, write_stages=1,
                 read_buffered=False):
        super().__init__(dict(
            full=Out(1),
            empty=Out(1),
            level=Out(range(depth + 2 + read_buffered * 2)),
        ))
        self.depth = depth
        self.write = Method(i=layout)
        self.read = Method(o=layout)
        self.write_stages = write_stages
        self.read_buffered = read_buffered

    def elaborate(self, plat):
        m = TModule()

        layout = self.write.layout_in
        storage = m.submodules.storage = Memory(shape=layout,
                                                depth=self.depth, init=[])

        full = self.full
        empty = Signal(init=1)
        m.d.comb += self.empty.eq(empty)

        # w_addr == r_addr: empty
        # w_addr == r_addr - 1: full
        w_port = storage.write_port()
        w_addr = Signal.like(w_port.addr)
        m.d.comb += [w_port.addr.eq(w_addr),
                     w_port.en.eq(1)]

        r_port = storage.read_port()
        r_addr = r_port.addr

        do_write = Signal()
        do_read = Signal()

        if self.write_stages == 0:
            write_meth = self.write
        else:
            m.submodules.write_pipe = write_pipe = PipelineBuilder()
            write_pipe.add_external(self.write)
            for _ in range(self.write_stages - 1):
                @write_pipe.stage(m)
                def _():
                    pass
            write_meth = Method(i=layout)
            write_pipe.call_method(write_meth)

        @def_method(m, write_meth, ready=~full)
        def _(arg):
            m.d.comb += do_write.eq(1)
            m.d.sync += w_addr.eq(_incr(w_addr, self.depth)[0])
            m.d.top_comb += w_port.data.eq(arg)

        buff0 = r_port.data
        buff1 = Signal.like(buff0, reset_less=True)
        buff2 = Signal.like(buff0, reset_less=True)
        buff0_en = Signal()
        buff1_en = Signal()
        buff2_en = Signal()
        fifo_level = Signal(range(self.depth))
        may_read = Signal()

        next_buff0_en = Signal()
        next_buff1_en = Signal()
        next_buff2_en = Signal()
        next_fifo_level = Signal(range(self.depth))
        m.d.comb += next_fifo_level.eq(fifo_level)
        m.d.sync += [buff0_en.eq(next_buff0_en),
                     buff1_en.eq(next_buff1_en),
                     buff2_en.eq(next_buff2_en),
                     fifo_level.eq(next_fifo_level)]

        with m.If(do_write & ~do_read):
            m.d.comb += next_fifo_level.eq(fifo_level + 1)
            m.d.sync += [empty.eq(0),
                         full.eq(fifo_level >= self.depth - 2)]
        with m.If(~do_write & do_read):
            m.d.comb += next_fifo_level.eq(fifo_level - 1)
            m.d.sync += [empty.eq(fifo_level[1:] == 0),
                         full.eq(0)]

        if self.read_buffered:
            buff3 = Signal.like(buff0, reset_less=True)
            buff4 = Signal.like(buff0, reset_less=True)
            buff3_en = Signal()
            buff4_en = Signal()
            next_buff3_en = Signal()
            next_buff4_en = Signal()
            m.d.sync += [buff3_en.eq(next_buff3_en),
                         buff4_en.eq(next_buff4_en),
                         may_read.eq(~next_buff4_en | ~next_buff3_en |
                                     (~next_buff0_en & ~next_buff1_en) |
                                     (~next_buff0_en & ~next_buff2_en) |
                                     (~next_buff1_en & ~next_buff2_en)),
                         self.level.eq(next_fifo_level + next_buff0_en +
                                       next_buff1_en + next_buff2_en +
                                       next_buff3_en + next_buff4_en)]

            # Buffer transfers
            with m.If(~buff4_en):
                m.d.sync += buff4.eq(buff3)
            with m.If(~(buff3_en & buff4_en)):
                m.d.sync += buff3.eq(buff1)
            with m.If(~buff4_en & buff2_en):
                m.d.sync += buff3.eq(buff2)
            with m.If(~(buff2_en & buff4_en)):
                m.d.sync += buff2.eq(buff1)
            with m.If(buff0_en):
                m.d.sync += buff1.eq(buff0)
            m.d.comb += [do_read.eq(~empty & may_read),
                         next_buff0_en.eq(do_read)]
            with m.If(do_read):
                m.d.sync += r_addr.eq(_incr(r_addr, self.depth)[0])

            # Buffer flags
            m.d.comb += [next_buff1_en.eq(buff0_en | (buff1_en & buff2_en &
                                                      buff4_en)),
                         next_buff2_en.eq(buff3_en &
                                          ((buff1_en & buff2_en) |
                                           (buff2_en & buff4_en) |
                                           (buff4_en & buff1_en))),
                         next_buff3_en.eq(buff1_en | buff2_en |
                                          (buff3_en & buff4_en)),
                         next_buff4_en.eq(buff4_en | buff3_en)]

            @def_method(m, self.read, ready=buff4_en | buff3_en)
            def _():
                # The data read is always the would-be-buff4 data
                m.d.comb += next_buff4_en.eq(0)
                return View(layout, Mux(buff4_en, buff4, buff3))
        else:
            m.d.comb += [next_buff0_en.eq(buff0_en),
                         next_buff1_en.eq(buff1_en),
                         next_buff2_en.eq(buff2_en)]
            m.d.sync += [may_read.eq(~(next_buff0_en & next_buff1_en) &
                                     ~(next_buff1_en & next_buff2_en) &
                                     ~(next_buff2_en & next_buff0_en)),
                         self.level.eq(next_fifo_level + next_buff0_en +
                                       next_buff1_en + next_buff2_en)]

            with m.If(~buff2_en | ~buff1_en):
                m.d.sync += buff1.eq(buff0)
                m.d.comb += [next_buff1_en.eq(buff0_en),
                             next_buff0_en.eq(0)]

            with m.If(~buff2_en):
                m.d.sync += buff2.eq(buff1)
                m.d.comb += next_buff2_en.eq(buff1_en)
            with m.If(~empty & (self.read.run | may_read)):
                m.d.sync += r_addr.eq(_incr(r_addr, self.depth)[0])
                m.d.comb += [do_read.eq(1),
                             next_buff0_en.eq(1)]

            @def_method(m, self.read, ready=buff1_en | buff2_en)
            def _():
                # The data read is always the would-be-buff2 data
                m.d.comb += next_buff2_en.eq(0)
                return View(layout, Mux(buff2_en, buff2, buff1))

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
        self._fifo = BufferedFifo(self._layout_out, self.depth - 1)

        self.read = self._fifo.read
        self.write = Method(i=self._layout_in)

        for name in ('full', 'empty', 'level'):
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
                                                self.depth - 4,
                                                read_buffered=True)

        m.d.comb += self.level.eq(fifo.level)

        # Construct a fast and conservative result level for user API
        user_len = len(self.user_level)
        user_level = self.level[:user_len]
        for i in range(user_len, len(self.level), user_len):
            user_level = user_level | self.level[i:i + user_len]
        m.d.comb += self.user_level.eq(user_level)

        @def_method(m, self.read)
        def _():
            with Transaction().body(m):
                res = fifo.read(m).data
            # This assumes that fifo.read.ready
            # fully reflects whether the method can run
            return Mux(fifo.read.ready, res, 0)

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
                                                 ('blocks', 10), ('first', 1)], 8)

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
        self.cmd_fifo = CommandFifo(data_width, 4097)
        self.cmd2_fifo = CommandFifo(data_width, 17)
        self.spi_cmd_fifo = BufferedFifo(SPI_ARG, 8)
        self.dds0_cmd_fifo = BufferedFifo(DDS_SET_ARG, 32)
        self.dds1_cmd_fifo = BufferedFifo(DDS_SET_ARG, 32)
        self.result_fifo = ResultFifo(data_width, 36)
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
