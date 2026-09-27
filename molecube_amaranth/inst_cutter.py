#

from amaranth import *

from transactron import TModule, Transaction, Method
from transactron.lib import Connect

from .fifo import RegFifo
from .utils import assign_xvalue, xvalue

# Instruction format:
#   [len: 2][opcode: 2][data: 12/28/44]
# Instruction length is fully determined by the first two bits to simplify decoding

# Blocks (16 bits) per input word and instructions per bundle
INST_BUNDLE_SIZE = 4

# Up to `INST_BUNDLE_SIZE` instructions per transfer, in order.
# `inst{i}` is only valid when `en{i}` is set.
# At most one instruction per bundle is "exclusive" (see `InstCutter`):
# `has_excl` tells whether there is one and `post{i}` marks the lanes after it,
# precomputed here so that the consumer does not need cross lane logic.
INST_BUNDLE = ([(f'inst{i}', 48) for i in range(INST_BUNDLE_SIZE)] +
               [(f'en{i}', 1) for i in range(INST_BUNDLE_SIZE)] +
               [(f'post{i}', 1) for i in range(INST_BUNDLE_SIZE)] +
               [('has_excl', 1)])

class InstCutter(Elaboratable):
    def __init__(self, *, exclusive=None):
        """
        exclusive: optional callable `inst -> Value`
            marking instructions of which at most one may be in a bundle.
        """
        self.write = Method(i=[('data', 64)])
        self.read = Method(o=INST_BUNDLE)
        self.exclusive = exclusive

    def elaborate(self, plat):
        m = TModule()

        nblocks = INST_BUNDLE_SIZE

        # The instruction lanes are indexed by the block position within
        # the input word: lane `j` is the instruction starting at block `j`
        # of the current word. This makes every lane a fixed slice of the
        # current and the next word so there are no data muxes at all.
        # The only sequential dependency between words is the number of
        # blocks of the last instruction of a word continuing into the next
        # one (the carry), which is a small function computed when a word
        # is accepted.

        m.submodules.data_conn = data_conn = Connect([('data', 64)])
        self.write.provide(data_conn.write)

        class Entry:
            def __init__(self, name):
                self.valid = Signal(name=f'{name}_valid')
                self.word = Signal(64, name=f'{name}_word', reset_less=True)
                # Block positions where an instruction starts
                self.starts = Signal(nblocks, name=f'{name}_starts', reset_less=True)
                # Whether the last instruction continues into the next word
                self.spans = Signal(name=f'{name}_spans', reset_less=True)

            def load(self, other):
                return [self.word.eq(other.word), self.starts.eq(other.starts),
                        self.spans.eq(other.spans)]

        # Three word queue: `cur` is being emitted (it needs `nxt` for an
        # instruction continuing into the next word), `nxt2` gives the input
        # a registered ready while keeping one word per cycle.
        cur = Entry('cur')
        nxt = Entry('nxt')
        nxt2 = Entry('nxt2')
        inp = Entry('inp')
        # Lanes of `cur` already emitted
        done = Signal(nblocks)

        ## Input side: instruction boundaries of the incoming word
        carry = Signal(range(3))

        in_trans = Transaction(name='cutter_in')
        with in_trans.body(m, ready=~nxt2.valid):
            data = data_conn.read(m).data
            m.d.top_comb += inp.word.eq(data)

            # Number of blocks of the instruction starting at each block
            # (an invalid length code is treated as a single block).
            nblk = [Signal(2, name=f'nblk{p}') for p in range(nblocks)]
            for p in range(nblocks):
                code = data[16 * p:16 * p + 2]
                m.d.top_comb += nblk[p].eq(Mux(code == 3, 1, code + 1))

            # start[p]: an instruction starts at block `p`,
            # either the first block after the carry or after a
            # previous instruction of this word.
            starts = []
            for p in range(nblocks):
                start = Signal(name=f'start{p}')
                terms = []
                if p < 3:
                    terms.append(carry == p)
                for q in range(p):
                    terms.append(starts[q] & (nblk[q] == p - q))
                m.d.top_comb += start.eq(Cat(*terms).any())
                starts.append(start)
            m.d.top_comb += inp.starts.eq(Cat(*starts))

            # Blocks of the last instruction beyond this word
            # (the cases are mutually exclusive)
            carry_out = Signal(range(3))
            carry_terms = C(0, 2)
            for p in range(nblocks):
                for n in range(1, 4):
                    if p + n > nblocks:
                        carry_terms = carry_terms | Mux(starts[p] & (nblk[p] == n),
                                                        p + n - nblocks, 0)
            m.d.top_comb += [carry_out.eq(carry_terms),
                             inp.spans.eq(carry_out != 0)]
            m.d.sync += carry.eq(carry_out)

        in_run = in_trans.run

        ## Output side: emit the lanes of `cur` up to the first exclusive
        ## instruction (at most one exclusive instruction per bundle).
        pair = Cat(cur.word, nxt.word)
        insts = [pair[16 * j:16 * j + 48] for j in range(nblocks)]

        remaining = Signal(nblocks)
        m.d.top_comb += remaining.eq(cur.starts & ~done)

        if self.exclusive is None:
            excl = C(0, nblocks)
        else:
            excl = Cat(*(remaining[j] & self.exclusive(insts[j])
                         for j in range(nblocks)))
        # A second exclusive instruction and everything after it
        # has to wait for the next bundle.
        blocked = Signal(nblocks)
        en = Signal(nblocks)
        for j in range(nblocks):
            earlier = excl[:j].any() if j > 0 else C(0)
            prev_blocked = blocked[j - 1] if j > 0 else C(0)
            m.d.top_comb += blocked[j].eq(prev_blocked | (excl[j] & earlier))
        m.d.top_comb += en.eq(remaining & ~blocked)

        # `cur` is fully emitted after this bundle
        finish = Signal()
        m.d.top_comb += finish.eq((remaining & ~en) == 0)

        # Emitted exclusive instruction and the lanes after it
        excl_en = Signal(nblocks)
        post = Signal(nblocks)
        m.d.top_comb += excl_en.eq(excl & en)
        for j in range(nblocks):
            m.d.top_comb += post[j].eq(en[j] & (excl_en[:j].any() if j > 0 else 0))

        m.submodules.out_fifo = out_fifo = RegFifo(INST_BUNDLE)

        out_trans = Transaction(name='cutter_out')
        with out_trans.body(m, ready=cur.valid & (~cur.spans | nxt.valid)):
            out_fifo.write(m, **{f'inst{j}': insts[j] for j in range(nblocks)},
                           **{f'en{j}': en[j] for j in range(nblocks)},
                           **{f'post{j}': post[j] for j in range(nblocks)},
                           has_excl=excl_en.any())
        out_run = out_trans.run

        ## Queue update
        advance = Signal()
        m.d.top_comb += advance.eq(out_run & finish)

        with m.If(advance):
            m.d.sync += done.eq(0)
        with m.Elif(out_run):
            m.d.sync += done.eq(done | en)

        # Shift the queue when `cur` is done, the input goes to the first
        # free slot (after the shift).
        with m.If(advance | ~cur.valid):
            m.d.sync += [cur.valid.eq(nxt.valid | (~nxt.valid & in_run))]
            with m.If(nxt.valid):
                m.d.sync += cur.load(nxt)
            with m.Else():
                m.d.sync += cur.load(inp)
            m.d.sync += nxt.valid.eq(nxt2.valid | (nxt.valid & in_run))
            with m.If(nxt2.valid):
                m.d.sync += nxt.load(nxt2)
            with m.Else():
                m.d.sync += nxt.load(inp)
            # The input is not accepted when `nxt2` is valid
            m.d.sync += nxt2.valid.eq(0)
        with m.Else():
            with m.If(in_run):
                with m.If(~nxt.valid):
                    m.d.sync += [nxt.valid.eq(1), *nxt.load(inp)]
                with m.Else():
                    m.d.sync += [nxt2.valid.eq(1), *nxt2.load(inp)]

        self.read.provide(out_fifo.read)

        return m
