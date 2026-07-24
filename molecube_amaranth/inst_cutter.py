#

from amaranth import *

from amaranth_axi.adaptors import InAdaptor

from transactron import TModule, Transaction, Method, def_method

from .utils import assign_xvalue, xvalue
from .fifo import SyncFIFOBuffered

# Instruction format:
#   [len: 2][opcode: 2][data: 12/28/44]
# Instruction length is fully determined by the first two bits to simplify decoding

class InstCutter(Elaboratable):
    def __init__(self):
        self.write = Method(i=[('data', 64)])
        self.read = Method(o=[('inst', 48)])

    def elaborate(self, plat):
        m = TModule()

        # The minimum we will consume per cycle is one blocks (of 16 bytes)
        # and the maximum amount of input data per cycle is 64 bit or four blocks
        # so the maximum we could accumulate in the internal buffer
        # per cycle is 3 blocks.

        # To be able to guarantee forward progress, we also have to be able to
        # accept new input data when there's less than 3 blocks in the buffer
        # (since if we have less than that we can't guarantee that there's a complete
        #  instruction in the buffer) so we have to accept new instruction at least
        # for <= 2, and so the full buffer length needs to be at least 5 blocks.

        # For making the buffer full check simpler,
        # I'd like to make >= 4 blocks the condition for not accepting new input
        # which means that the maximum length we will accept input is 3 blocks
        # and the maximum buffer size we'll ever have is 6 blocks.

        buff_len = Signal(3)
        # Last bits are the valid ones,
        # the number of valid blocks is determined by buff_len
        buff = Signal(16 * 6, reset_less=True)

        m.submodules.fifo = fifo = SyncFIFOBuffered(width=48, depth=3)
        m.submodules.in_adaptor = in_adaptor = InAdaptor.from_signal(
            ready=fifo.r_en, valid=fifo.r_rdy, data=fifo.r_data)

        en = fifo.w_en
        inst = fifo.w_data

        def parse_undef():
            assign_xvalue(m, inst, domain='comb')

        def full_undef():
            parse_undef()
            assign_xvalue(m, buff_len)
            assign_xvalue(m, en, domain='comb')

        def assign_inst(inst, data):
            ldata = len(data)
            linst = len(inst)
            assert ldata <= linst
            m.d.comb += inst.eq(Cat(data, xvalue(m, linst - ldata)))

        def assign_len(l):
            assert l <= 6
            m.d.sync += buff_len.eq(l)

        def parsed_inst(data, linst):
            ldata = len(data) // 16
            if ldata >= linst:
                assign_len(ldata - linst)
                assign_inst(inst, data[:16 * linst])
            else:
                m.d.comb += en.eq(0)
                assign_len(ldata)
                assign_xvalue(m, inst, domain='comb')

        def parse(data):
            with m.Switch(data[:2]):
                with m.Case(0):
                    parsed_inst(data, 1)
                with m.Case(1):
                    parsed_inst(data, 2)
                with m.Case(2):
                    parsed_inst(data, 3)
                with m.Default():
                    full_undef()

        @def_method(m, self.write, ready=(~buff_len[2]) & fifo.w_rdy)
        def _(data):
            full_data = Cat(buff, data)
            m.d.sync += buff.eq(full_data[16 * 4:])
            m.d.comb += en.eq(1)

            with m.Switch(buff_len[:2]):
                with m.Case(0):
                    parse(full_data[16 * 6:])
                with m.Case(1):
                    parse(full_data[16 * 5:])
                with m.Case(2):
                    parse(full_data[16 * 4:])
                with m.Case(3):
                    parse(full_data[16 * 3:])
                with m.Default():
                    full_undef()

        with Transaction().body(m, ready=~self.write.run & fifo.w_rdy):
            m.d.comb += en.eq(1)
            with m.Switch(buff_len):
                with m.Case(0):
                    m.d.comb += en.eq(0)
                    assign_len(0)
                    assign_xvalue(m, inst, domain='comb')
                with m.Case(1):
                    parse(buff[16 * 5:])
                with m.Case(2):
                    parse(buff[16 * 4:])
                with m.Case(3):
                    parse(buff[16 * 3:])
                with m.Case(4):
                    parse(buff[16 * 2:])
                with m.Case(5):
                    parse(buff[16 * 1:])
                with m.Case(6):
                    parse(buff)
                with m.Default():
                    full_undef()

        with m.If(~fifo.w_rdy):
            parse_undef()

        @def_method(m, self.read)
        def _():
            return in_adaptor.input(m).DATA

        return m
