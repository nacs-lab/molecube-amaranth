#

from amaranth import *
from amaranth.utils import exact_log2

from amaranth_axi.axitools import AXIMasterReadIFace

from transactron import TModule, Transaction, Method, def_method

from .utils import oring_combiner, assign_xvalue

class CountKeeper(Elaboratable):
    def __init__(self, width):
        self.width = width
        self.add = Method(i=[('count', width)])
        self.done = Method(o=[('last', 1)])

    def elaborate(self, plat):
        m = TModule()

        cur_count = Signal(self.width)
        next_count = Signal(self.width)

        cur_valid = Signal()
        next_valid = Signal()

        pop_count = Method()
        @def_method(m, pop_count, single_caller=True)
        def _():
            # Assume cur_valid
            m.d.sync += [next_valid.eq(0),
                         cur_valid.eq(next_valid),
                         cur_count.eq(next_count)]
            assign_xvalue(m, next_count)

        @def_method(m, self.add, ready=~next_valid)
        def _(count):
            m.d.sync += cur_valid.eq(1)
            with m.If(pop_count.run):
                # Assume cur_valid
                m.d.sync += cur_count.eq(count)
                assign_xvalue(m, next_count)
            with m.Elif(cur_valid):
                m.d.sync += [next_count.eq(count),
                             next_valid.eq(1)]
            with m.Else():
                m.d.sync += cur_count.eq(count)
                assign_xvalue(m, next_count)

        @def_method(m, self.done, nonexclusive=True)
        def _():
            last_count = Signal()
            m.d.top_comb += last_count.eq(cur_count == 0)

            with m.If(last_count):
                pop_count(m)
            with m.Else():
                m.d.sync += cur_count.eq(cur_count - 1)

            return last_count

        return m

class AXIReadStream(Elaboratable):
    def __init__(self, axi, block_len, blocks_width, max_width=None):
        self.axi = axi
        self.addr_width = len(axi.ARADDR)
        self.data_width = len(axi.RDATA)
        self.block_len = block_len
        self.blocks_width = blocks_width
        self.max_width = self.addr_width if max_width is None else max_width
        assert block_len <= 1 << len(axi.ARLEN)
        self.queue = Method(i=[('addr', self.addr_width), ('blocks', blocks_width)])
        self.get = Method(o=[('data', self.data_width), ('last', 1)])

    def elaborate(self, plat):
        m = TModule()

        def inc_addr(addr):
            inc = (self.data_width // 8) * self.block_len
            low_nbits = exact_log2(inc & -inc)
            mid_bits = addr[low_nbits:self.max_width]
            inc_mid_bits = Signal.like(mid_bits)
            m.d.top_comb += inc_mid_bits.eq(addr[low_nbits:self.max_width] + (inc >> low_nbits))
            res = Cat(addr[:low_nbits], inc_mid_bits, addr[self.max_width:])
            assert len(res) == len(addr)
            return res

        m.submodules.read_iface = read_iface = AXIMasterReadIFace(
            self.axi,
            buffered=True,
            const_id=0,
            const_size=exact_log2(self.data_width) - 3,
            const_len=self.block_len - 1,
            const_burst=1)

        m.submodules.count_keeper = count_keeper = CountKeeper(self.blocks_width)

        next_addr = Signal(self.addr_width)

        request = Method(i=[('addr', self.addr_width)])
        @def_method(m, request, combiner=oring_combiner, nonexclusive=True)
        def _(addr):
            m.d.sync += next_addr.eq(inc_addr(addr))
            read_iface.request(m, addr=addr)

        req_blocks = Signal(self.blocks_width)
        with Transaction().body(m, ready=req_blocks != 0):
            m.d.sync += req_blocks.eq(req_blocks - 1)
            request(m, next_addr)

        @def_method(m, self.queue, ready=req_blocks == 0)
        def _(addr, blocks):
            m.d.sync += req_blocks.eq(blocks)
            count_keeper.add(m, blocks)
            request(m, addr)

        @def_method(m, self.get)
        def _():
            rep = read_iface.reply(m)
            islast = Signal()

            with m.If(rep.last):
                m.d.av_comb += islast.eq(count_keeper.done(m).last)

            return dict(data=rep.data, last=islast)

        return m
