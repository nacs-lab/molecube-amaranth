#

from amaranth import *
from amaranth.lib.data import Struct
from amaranth.utils import exact_log2

from amaranth_axi.axitools import AXIMasterReadIFace

from transactron import TModule, Transaction, Method, def_method

from .fifo import BufferedFifo
from .inst_cutter import InstCutter
from .utils import oring_combiner, assign_xvalue, reg_chain

class CountKeeper(Elaboratable):
    def __init__(self, width):
        self.width = width
        self.add = Method(i=[('count', width)])
        self.done = Method(o=[('last', 1)])

    def elaborate(self, plat):
        m = TModule()

        cur_count = Signal(self.width, reset_less=True)
        next_count = Signal(self.width, reset_less=True)

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
    def __init__(self, axi, block_len, blocks_width, max_width=None,
                 const_user=0, const_cache=0, align_width=None):
        self.axi = axi
        self.addr_width = len(axi.ARADDR)
        self.data_width = len(axi.RDATA)
        self.block_len = block_len
        self.blocks_width = blocks_width
        if align_width is None:
            align_width = exact_log2(self.data_width // 8)
        else:
            assert (block_len * self.data_width // 8) % (1 << align_width) == 0
        self.align_width = align_width
        self.max_width = self.addr_width if max_width is None else max_width
        assert block_len <= 1 << len(axi.ARLEN)
        self.queue = Method(i=[('addr', self.addr_width), ('blocks', blocks_width)])
        self.get = Method(o=[('data', self.data_width), ('last', 1)])
        self.const_user = const_user
        self.const_cache = const_cache

    def elaborate(self, plat):
        m = TModule()

        def inc_addr(addr):
            inc = (self.data_width // 8) * self.block_len
            low_nbits = exact_log2(inc & -inc)
            assert low_nbits >= self.align_width
            mid_bits = addr[low_nbits - self.align_width:self.max_width - self.align_width]
            inc_mid_bits = Signal.like(mid_bits)
            m.d.top_comb += inc_mid_bits.eq(mid_bits + (inc >> low_nbits))
            res = Cat(addr[:low_nbits - self.align_width], inc_mid_bits, addr[self.max_width - self.align_width:])
            assert len(res) == len(addr)
            return res

        m.submodules.read_iface = read_iface = AXIMasterReadIFace(
            self.axi,
            buffered=True,
            const_id=0,
            const_size=exact_log2(self.data_width) - 3,
            const_len=self.block_len - 1,
            const_burst=1,
            const_cache=self.const_cache,
            const_user=self.const_user)

        m.submodules.count_keeper = count_keeper = CountKeeper(self.blocks_width)

        next_addr = Signal(self.addr_width - self.align_width, reset_less=True)

        request = Method(i=[('addr', self.addr_width - self.align_width)])
        @def_method(m, request, combiner=oring_combiner, nonexclusive=True)
        def _(addr):
            m.d.sync += next_addr.eq(inc_addr(addr))
            read_iface.request(m, addr=Cat(C(0, self.align_width), addr))

        req_blocks = Signal(self.blocks_width)
        with Transaction().body(m, ready=req_blocks != 0):
            m.d.sync += req_blocks.eq(req_blocks - 1)
            request(m, next_addr)

        @def_method(m, self.queue, ready=req_blocks == 0)
        def _(addr, blocks):
            m.d.sync += req_blocks.eq(blocks)
            count_keeper.add(m, blocks)
            request(m, addr[self.align_width:])

        @def_method(m, self.get)
        def _():
            rep = read_iface.reply(m)
            islast = Signal()

            with m.If(rep.last):
                m.d.av_comb += islast.eq(count_keeper.done(m).last)

            return dict(data=rep.data, last=islast)

        return m


class DMAController(Elaboratable):
    def __init__(self, axi, csr, fifos):
        self.axi = axi
        self.csr = csr
        self.fifos = fifos
        self.read_inst = Method(o=[('inst', 48)])
        self.inst_started = Method()
        self.inst_stopped = Method()
        self.trig_timeout = Method()

    def elaborate(self, plat):
        m = TModule()
        fifos = self.fifos

        transfer_count = Signal(8)
        running = Signal()
        underflow = Signal()
        trig_timeout = Signal()
        underflow_armed = Signal()
        cmd_empty = Signal(init=1)
        cmd_full = Signal(init=0)
        m.d.sync += [cmd_empty.eq(fifos.spi_cmd_fifo.empty & fifos.dds0_cmd_fifo.empty &
                                  fifos.dds1_cmd_fifo.empty),
                     cmd_full.eq(fifos.spi_cmd_fifo.full | fifos.dds0_cmd_fifo.full |
                                 fifos.dds1_cmd_fifo.full)]

        dma_status = self.csr.dma_status
        reg_chain(m, input=running, output=dma_status.running, levels=2,
                  reset_output=False, reset_mid=False)
        reg_chain(m, input=underflow, output=dma_status.underflow, levels=2,
                  reset_output=False, reset_mid=False)
        reg_chain(m, input=transfer_count, output=dma_status.transfer_count,
                  levels=2, reset_output=False, reset_mid=False)
        reg_chain(m, input=trig_timeout, output=dma_status.trig_timeout, levels=2,
                  reset_output=False, reset_mid=False)
        reg_chain(m, input=cmd_empty, output=dma_status.cmd_empty, levels=2,
                  reset_output=False, reset_mid=False)
        reg_chain(m, input=cmd_full, output=dma_status.cmd_full, levels=2,
                  reset_output=False, reset_mid=False)

        # Address has to be 128bytes aligned, each block cannot cross 1 MB boundary
        align_width = 7
        m.submodules.axi_stream = axi_stream = AXIReadStream(self.axi, 16, 10,
                                                             max_width=20,
                                                             align_width=align_width)

        with Transaction().body(m):
            cmd = fifos.dma_cmd_fifo.read(m)
            axi_stream.queue(m, addr=cmd.addr, blocks=cmd.blocks)
            with m.If(cmd.first):
                m.d.sync += [underflow_armed.eq(0),
                             underflow.eq(0),
                             trig_timeout.eq(0)]

        @def_method(m, self.inst_started, nonexclusive=True)
        def _():
            with m.If(underflow_armed):
                m.d.sync += underflow.eq(1)
            m.d.sync += running.eq(1)

        @def_method(m, self.inst_stopped, nonexclusive=True)
        def _():
            m.d.sync += [underflow_armed.eq(1),
                         running.eq(0)]

        @def_method(m, self.trig_timeout, nonexclusive=True)
        def _():
            m.d.sync += [trig_timeout.eq(1)]

        m.submodules.inst_cutter = inst_cutter = InstCutter()

        with Transaction().body(m):
            rep = axi_stream.get(m)
            inst_cutter.write(m, rep.data)
            with m.If(rep.last):
                m.d.sync += transfer_count.eq(transfer_count + 1)

        self.read_inst.provide(inst_cutter.read)

        return m
