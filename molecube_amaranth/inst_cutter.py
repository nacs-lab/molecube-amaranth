#

from amaranth import *

from transactron import TModule, Transaction, Method
from transactron.lib import PipelineBuilder, Connect

from .fifo import RegFifo
from .utils import assign_xvalue, xvalue

# Instruction format:
#   [len: 2][opcode: 2][data: 12/28/44]
# Instruction length is fully determined by the first two bits to simplify decoding

# Up to two instructions per transfer.
# `inst1` is only valid when `en1` is set.
INST_BUNDLE = [('inst0', 48), ('inst1', 48), ('en1', 1)]

class InstCutter(Elaboratable):
    def __init__(self, *, pair_ok=None):
        """
        pair_ok: optional callable `(m, inst0, inst1) -> Value`
            deciding whether two consecutive (complete) instructions may be
            emitted together in one bundle. Default: always.
        """
        self.write = Method(i=[('data', 64)])
        self.read = Method(o=INST_BUNDLE)
        self.pair_ok = pair_ok

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

        # We emit up to two instructions per cycle. Since at least one block is
        # consumed whenever input is accepted, the analysis above still holds.
        # With one block instructions the output (2 instructions/cycle)
        # matches the input rate (4 blocks per cycle accepted every other cycle),
        # for longer instructions the input is the bottleneck.

        buff_len = Signal(3)
        # Last bits are the valid ones,
        # the number of valid blocks is determined by buff_len
        buff = Signal(16 * 6, reset_less=True)

        m.submodules.data_conn = data_conn = Connect([('data', 64)])
        self.write.provide(data_conn.write)

        m.submodules.cut_pipe = cut_pipe = PipelineBuilder()

        @cut_pipe.stage(m, o=[('en0', 1)] + INST_BUNDLE)
        def _():
            in_trans = Transaction()
            with in_trans.body(m, ready=~buff_len[2]):
                data = data_conn.read(m)
            run = in_trans.run
            # Block `6 - buff_len` is the first valid block,
            # blocks 6 to 9 are only valid when `in_trans` runs.
            # The padding at the end makes sure the slices below are in range.
            full_data = Cat(buff, data, xvalue(m, 16 * 2))

            def blocks(first, n):
                return full_data[16 * first:16 * (first + n)]

            en0 = Signal(reset_less=True)
            en1 = Signal(reset_less=True)
            inst0 = Signal(48, reset_less=True)
            inst1 = Signal(48, reset_less=True)

            def emit(k, c0, c1, pair):
                # k: number of valid blocks in the buffer (excluding new input)
                # c0: length code of the first instruction (None if invalid)
                # c1: length code of the second instruction (None if invalid)
                # pair: signal, whether the two instructions may be paired
                n0 = 1 if c0 is None else c0 + 1
                n1 = None if c1 is None else c1 + 1

                def compute(with_input):
                    avail = k + (4 if with_input else 0)
                    e0 = avail >= n0
                    e1 = e0 and n1 is not None and avail >= n0 + n1
                    len_single = avail - n0 if e0 else avail
                    if not e1:
                        return e0, C(0), C(len_single, 3)
                    return e0, pair, Mux(pair, avail - n0 - n1, len_single)

                e0w, e1w, lw = compute(False)
                if k <= 3:
                    e0i, e1i, li = compute(True)
                    assert e0i
                    m.d.av_comb += [en0.eq(run | e0w),
                                    en1.eq(Mux(run, e1i, e1w))]
                    m.d.sync += buff_len.eq(Mux(run, li, lw))
                else:
                    # Cannot accept input with 4 or more blocks
                    m.d.av_comb += [en0.eq(e0w), en1.eq(e1w)]
                    m.d.sync += buff_len.eq(lw)

            with m.Switch(buff_len):
                for k in range(7):
                    with m.Case(k):
                        first = 6 - k
                        first_inst = blocks(first, 3)
                        m.d.av_comb += inst0.eq(first_inst)
                        with m.Switch(blocks(first, 1)[:2]):
                            for c0 in range(3):
                                with m.Case(c0):
                                    second = first + c0 + 1
                                    second_inst = blocks(second, 3)
                                    m.d.av_comb += inst1.eq(second_inst)
                                    if self.pair_ok is None:
                                        pair = C(1)
                                    else:
                                        pair = self.pair_ok(m, first_inst, second_inst)
                                    with m.Switch(blocks(second, 1)[:2]):
                                        for c1 in range(3):
                                            with m.Case(c1):
                                                emit(k, c0, c1, pair)
                                        with m.Default():
                                            # Invalid (or not yet valid) second instruction
                                            emit(k, c0, None, C(0))
                            with m.Default():
                                # Invalid (or not yet valid) first instruction
                                assign_xvalue(m, inst1, domain='av_comb')
                                emit(k, None, None, C(0))
                with m.Default():
                    assign_xvalue(m, inst0, domain='av_comb')
                    assign_xvalue(m, inst1, domain='av_comb')
                    assign_xvalue(m, en0, domain='av_comb')
                    assign_xvalue(m, en1, domain='av_comb')
                    assign_xvalue(m, buff_len)

            with m.If(run):
                m.d.sync += buff.eq(blocks(4, 6))

            return dict(en0=en0, inst0=inst0, inst1=inst1, en1=en1)

        # Registered ready towards the cutter stage and a plain register
        # output towards the parser (helps timing over a LUTRAM fifo)
        m.submodules.fifo = fifo = RegFifo(INST_BUNDLE)

        @cut_pipe.stage(m)
        def _(en0, inst0, inst1, en1):
            with m.If(en0):
                fifo.write(m, inst0=inst0, inst1=inst1, en1=en1)

        self.read.provide(fifo.read)

        return m
