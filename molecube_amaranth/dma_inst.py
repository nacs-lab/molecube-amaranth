#

from amaranth import *
from amaranth.lib import enum
from amaranth.lib.data import Struct, Union, ArrayLayout, View
from amaranth.lib.memory import Memory
from amaranth.utils import ceil_log2

from transactron import TModule, Transaction, Method, def_method
from transactron.lib import PipelineBuilder, Connect

from amaranth_axi.utils import StructCat

from types import SimpleNamespace

from .dds import SET_ARG as DDS_SET_ARG, DDSReq
from .fifo import BufferedFifo, pipeline_regfifo
from .inst_cutter import INST_BUNDLE, INST_BUNDLE_SIZE
from .utils import assign_xvalue, top_d

# Instruction format:
#   [len: 2][opcode: 2][data: 12/28/44]
# Instruction length is fully determined by the first two bits to simplify decoding
#
# All instructions
#
# * 2 bytes (len = 0, 4 + 12 bits)
#
#     *     wait1: [len<0>: 2][opcode<0>: 2][cycle: 12]
#     *  ttl_set4: [len<0>: 2][opcode<1>: 2][bank4_1: 6][val1: 4][<0>: 2]
#     *  clockout: [len<0>: 2][opcode<3>: 2][period: 9][<0>: 3]
#
# * 4 bytes (len = 1, 4 + 28 bits)
#
#     *     wait2: [len<1>: 2][opcode<0>: 2][cycle: 28]
#     * ttl_set16: [len<1>: 2][opcode<1>: 2][bank8_1: 5][val1: 8][bank8_2: 5][val2: 8][<0>: 2]
#     * dds_set16: [len<1>: 2][opcode<2>: 2][bus_id: 1][dds_id: 4][fud: 1][addr: 6][data: 16]
#
# * 6 bytes (len = 2, 4 + 44 bits)
#
#     * wait_trig: [len<2>: 2][opcode<0>: 2][chn: 8][edge: 1][cycle: 35]
#     * ttl_set32: [len<2>: 2][opcode<1>: 2][bank16_1: 4][val1: 16][bank16_2: 4][val2: 16][<0>: 4]
#     * dds_set32: [len<2>: 2][opcode<2>: 2][bus_id: 1][dds_id: 4][fud: 1][addr: 6][data: 32]
#     *       dac: [len<2>: 2][opcode<3>: 2][id: 2][cycle: 9][clk_pha: 1][clk_pol: 1][data: 18][<0>: 13]


def assert_max_size(ty, maxsz):
    assert Shape.cast(ty).width <= maxsz

### Instruction header (length and opcode)

class OpCode(enum.Enum, shape=2):
    WAIT1 = 0
    TTL_SET4 = 1
    CLOCKOUT = 3

    WAIT2 = 0
    TTL_SET16 = 1
    DDS_SET1 = 2

    WAIT_TRIG = 0
    TTL_SET32 = 1
    DDS_SET2 = 2
    DAC = 3

class InstHead(Struct):
    len: 2
    opcode: OpCode


### 16 bit instructions

class Wait1Args(Struct):
    cycle: 12

class TTLSet4Args(Struct):
    bank4_1: 6
    val1: 4

class ClockOutArgs(Struct):
    period: 9

assert_max_size(Wait1Args, 16 * 1 - 4)
assert_max_size(TTLSet4Args, 16 * 1 - 4)
assert_max_size(ClockOutArgs, 16 * 1 - 4)

### 32 bit instructions

class Wait2Args(Struct):
    cycle: 28

class TTLSet16Args(Struct):
    bank8_1: 5
    val1: 8
    bank8_2: 5
    val2: 8

class DDSSet16Args(Struct):
    bus_id: 1
    dds_id: 4
    fud: 1
    addr: 6
    data: 16

assert_max_size(Wait2Args, 16 * 2 - 4)
assert_max_size(TTLSet16Args, 16 * 2 - 4)
assert_max_size(DDSSet16Args, 16 * 2 - 4)

### 48 bit instructions

class WaitTrigArgs(Struct):
    chn: 8
    edge: 1
    cycle: 35

class TTLSet32Args(Struct):
    bank16_1: 4
    val1: 16
    bank16_2: 4
    val2: 16

class DDSSet32Args(Struct):
    bus_id: 1
    dds_id: 4
    fud: 1
    addr: 6
    data: 32

class DACArgs(Struct):
    id: 2
    cycle: 9
    clk_pha: 1
    clk_pol: 1
    data: 18

assert_max_size(WaitTrigArgs, 16 * 3 - 4)
assert_max_size(TTLSet32Args, 16 * 3 - 4)
assert_max_size(DDSSet32Args, 16 * 3 - 4)

# All arguments

class InstArgs(Union):
    wait1: Wait1Args
    ttl_set4: TTLSet4Args
    clockout: ClockOutArgs

    wait2: Wait2Args
    ttl_set16: TTLSet16Args
    dds_set16: DDSSet16Args

    wait_trig: WaitTrigArgs
    ttl_set32: TTLSet32Args
    dds_set32: DDSSet32Args
    dac: DACArgs

assert_max_size(InstArgs, 16 * 3 - 4)

##
# Decoded instructions

class DecodedOpCode(enum.Enum, shape=3):
    WAIT = 0
    WAIT_TRIG = 1

    CLOCKOUT = 2
    TTL = 3
    DDS0 = 4
    DDS1 = 5
    DAC = 6

class WaitRawDecode(Struct):
    cycle: 28
    is0: 1

class WaitDecode(Struct):
    # The wait counter is loaded with `cycle - 1`, precomputed here
    # to keep the subtraction off the runner's fetch path.
    cycle_m1: 28
    is0: 1

WaitTrigDecode = WaitTrigArgs
ClockOutDecode = ClockOutArgs

DDSDecode = DDS_SET_ARG

def _TTLDecode(nttl):
    class TTLDecode(Struct):
        val: nttl
        mask: nttl

    return TTLDecode

DACDecode = DACArgs

class TrivialDecode(Union):
    wait_trig: WaitTrigDecode
    clock_out: ClockOutDecode
    dac: DACDecode

class WaitAction(Union):
    wait: WaitDecode
    wait_trig: WaitTrigDecode

def _OutputAction(nttl):
    class OutputAction(Struct):
        clockout_en: 1
        clockout: ClockOutDecode

        ttl_en: 1
        ttl: _TTLDecode(nttl).as_shape()

        dds0_en: 1
        dds0: DDSDecode

        dds1_en: 1
        dds1: DDSDecode

        dac_en: 1
        dac: DACDecode

    return OutputAction.as_shape()

def is_wait_inst(inst):
    """Whether a raw instruction is a wait (or wait_trig) instruction.

    The parser emits at most one wait group per cycle so the cutter
    puts at most one wait instruction in a bundle.
    """
    return InstHead(inst[:4]).opcode == OpCode.WAIT1

INST_CLASSES = ('wait', 'clockout', 'ttl', 'dds0', 'dds1', 'dac')

# DMA DDS write disabler request.
# Selects the pair of 16 bit registers `pair * 2` and `pair * 2 + 1`
# (i.e. `pair` is the DDS parallel address >> 2) of DDS `dds_id` on bus
# `bus_id`; if `we` is set, `value` is stored as the disable bits of the pair
# (bit 0 for the even register, bit 1 for the odd one). The `dds_mask` CSR
# then reflects the disable bits of the pair selected by the last request.
class DDSMaskReq(Struct):
    bus_id: 1
    dds_id: 4
    pair: 5
    we: 1
    value: 2

# DMA DDS writes to disabled registers are redirected to this 16 bit register
# (DDS parallel address 0x70) to keep the timing of the other side effects.
DDS_NOOP_IDX = 0x38

class LaneFlags(Struct):
    # Lane is valid
    en: 1
    # Lane comes after the wait of the bundle
    post: 1

class Lane0Flags(Struct):
    en: 1
    post: 1
    # The bundle has a wait (carried by the first lane only)
    has_wait: 1

class DMAInstDecoder(Elaboratable):
    """Decode a single instruction stream (one lane)."""
    def __init__(self, csr, nttl, flags_shape=1, dds_mask_port=None):
        """
        flags_shape: shape of the `flags` field passed through the decoder unchanged
        dds_mask_port: optional asynchronous read port of the DDS write
            disable bits (see `DMAInstParser`); disabled DDS writes are
            redirected to `DDS_NOOP_IDX`.
        """
        self.csr = csr
        self.dds_mask_port = dds_mask_port
        TTLDecode = _TTLDecode(nttl)
        self.nttl = nttl
        self.TTLDecode = TTLDecode
        self.write = Method(i=[('inst', 48), ('flags', flags_shape)])
        self.read = Method(o=[('opcode', DecodedOpCode), ('trivial', TrivialDecode),
                              ('wait', WaitDecode), ('ttl', TTLDecode),
                              ('dds', DDSDecode), ('flags', flags_shape)] +
                           [(f'is_{name}', 1) for name in INST_CLASSES])

    def elaborate(self, plat):
        m = TModule()

        ## TTL
        nttl = self.nttl
        nttl_width = ceil_log2(nttl)
        nttl_total = 1 << nttl_width
        ttl_mask = self.csr.dma_ttl_mask

        def pad_ttl(s):
            return Cat(s, Signal(nttl_total - nttl))

        def ttl_banks(s, width):
            nele = nttl_total // width
            assert width * nele == nttl_total
            return View(ArrayLayout(unsigned(width), nele), pad_ttl(s))

        TTLDecode = self.TTLDecode

        ## DDS
        dds_req = DDSReq(self.csr)

        m.submodules.decode_pipe = decode_pipe = PipelineBuilder()

        decode_pipe.add_external(self.write)

        # Independent parts of the decoding are done in the same stages
        # to keep the latency down to the longest (TTL) dependency chain:
        # 1. everything except the second TTL bank and the final selections
        # 2. second TTL bank, wait select
        # 3. TTL select and mask, wait counter value
        # Doing both TTL banks in the first stage saves another stage and
        # ~1000 FFs in total but is slightly worse for timing.

        def ttl_set_bank(ttl, width, bank, val):
            # Set `bank` of `ttl` to `val` with the mask enabled
            banks = ttl_banks(ttl.val, width)
            masks = ttl_banks(ttl.mask, width)
            m.d.top_comb += [banks[bank].eq(val),
                             masks[bank].eq(~C(0, width))]

        def ttl_bank1(args, ttl4, ttl16, ttl32):
            ttl_set_bank(ttl4, 4, args.ttl_set4.bank4_1[:nttl_width - 2],
                         args.ttl_set4.val1)
            ttl_set_bank(ttl16, 8, args.ttl_set16.bank8_1[:nttl_width - 3],
                         args.ttl_set16.val1)
            ttl_set_bank(ttl32, 16, args.ttl_set32.bank16_1[:nttl_width - 4],
                         args.ttl_set32.val1)

        def ttl_bank2(args, ttl16_in, ttl32_in):
            ttl16 = Signal(TTLDecode)
            ttl32 = Signal(TTLDecode)
            m.d.top_comb += [ttl16.eq(ttl16_in), ttl32.eq(ttl32_in)]
            ttl_set_bank(ttl16, 8, args.ttl_set16.bank8_2[:nttl_width - 3],
                         args.ttl_set16.val2)
            ttl_set_bank(ttl32, 16, args.ttl_set32.bank16_2[:nttl_width - 4],
                         args.ttl_set32.val2)
            return ttl16, ttl32

        def ttl_select(head, ttl4, ttl16, ttl32):
            # 3:1 mux on the instruction length.
            # The global TTL mask is applied here where it fits in the same
            # LUT as the mux (applying it per bank costs a LUT per bit).
            # The user should not specify any value outside of the mask that are on
            # so we don't need to mask the value, only the mask
            ttl = Signal(TTLDecode)
            with m.Switch(head.len):
                with m.Case(0):
                    m.d.av_comb += ttl.eq(ttl4)
                with m.Case(1):
                    m.d.av_comb += ttl.eq(ttl16)
                with m.Default():
                    m.d.av_comb += ttl.eq(ttl32)
            return StructCat(TTLDecode, val=ttl.val, mask=ttl.mask & ttl_mask)

        @decode_pipe.stage(m, o=[('head', InstHead), ('args', InstArgs),
                                 ('wait1', WaitRawDecode), ('wait2', WaitRawDecode),
                                 ('ttl4', TTLDecode), ('ttl16', TTLDecode),
                                 ('ttl32', TTLDecode),
                                 ('dds', DDSDecode), ('trivial', TrivialDecode),
                                 ('opcode', DecodedOpCode)] +
                           [(f'is_{name}', 1) for name in INST_CLASSES])
        def decode_1(inst):
            head = InstHead(inst[:4])
            args = InstArgs(inst[4:])

            ## Wait
            wait1 = StructCat(WaitRawDecode, cycle=args.wait1.cycle,
                              is0=args.wait1.cycle == 0)
            wait2 = StructCat(WaitRawDecode, cycle=args.wait2.cycle,
                              is0=args.wait2.cycle == 0)

            ## TTL
            ttl4 = Signal(TTLDecode)
            ttl16 = Signal(TTLDecode)
            ttl32 = Signal(TTLDecode)
            ttl_bank1(args, ttl4, ttl16, ttl32)

            ## DDS
            dds_set16 = args.dds_set16
            dds_set32 = args.dds_set32

            # We assume the bus_id bit are the same one for set 16 and set 32
            dds_bus_id = args.dds_set16.bus_id
            dds16 = dds_req.write1(m, id=dds_set16.dds_id, addr1=dds_set16.addr,
                                   data1=dds_set16.data, fud=dds_set16.fud)
            dds32 = dds_req.write2(m, id=dds_set32.dds_id, addr1=dds_set32.addr,
                                   data1=dds_set32.data[:16],
                                   addr2=Cat(C(1, 1), dds_set32.addr[1:]),
                                   data2=dds_set32.data[16:], fud=dds_set32.fud)
            dds16 = StructCat(DDSDecode, **dds16)
            dds32 = StructCat(DDSDecode, **dds32)

            ## Trivial (wait_trig, clockout, dac)
            trivial = TrivialDecode(Signal.cast(args)[:TrivialDecode.as_shape().size])

            ## Opcode
            opcode = Signal(DecodedOpCode)
            # One-hot instruction class flags for the merge stage
            is_dds = head.opcode == OpCode.DDS_SET1
            is_clockout = head.opcode == OpCode.CLOCKOUT
            flags = dict(is_wait=head.opcode == OpCode.WAIT1,
                         is_ttl=head.opcode == OpCode.TTL_SET4,
                         is_dds0=is_dds & ~dds_bus_id,
                         is_dds1=is_dds & dds_bus_id,
                         is_clockout=is_clockout & ~head.len[1],
                         is_dac=is_clockout & head.len[1])
            with m.Switch(head.opcode):
                with m.Case(OpCode.WAIT1):
                    m.d.av_comb += opcode.eq(Mux(head.len[1], DecodedOpCode.WAIT_TRIG,
                                                 DecodedOpCode.WAIT))
                with m.Case(OpCode.TTL_SET4):
                    m.d.av_comb += opcode.eq(DecodedOpCode.TTL)
                with m.Case(OpCode.DDS_SET1):
                    m.d.av_comb += opcode.eq(DecodedOpCode.DDS0 | dds_bus_id)
                with m.Case(OpCode.CLOCKOUT):
                    m.d.av_comb += opcode.eq(Mux(head.len[1], DecodedOpCode.DAC,
                                                 DecodedOpCode.CLOCKOUT))

            return dict(head=head, args=args, wait1=wait1, wait2=wait2,
                        ttl4=ttl4, ttl16=ttl16, ttl32=ttl32,
                        dds=DDSDecode(Mux(head.len[1], dds32, dds16)),
                        trivial=trivial, opcode=opcode, **flags)

        @decode_pipe.stage(m, o=[('wait_raw', WaitRawDecode),
                                 ('ttl16', TTLDecode), ('ttl32', TTLDecode),
                                 ('dds_mask', 2)])
        def decode_2(head, args, wait1, wait2, ttl16, ttl32):
            ttl16, ttl32 = ttl_bank2(args, ttl16, ttl32)

            ## DDS write disable bits
            # The set16 and set32 instructions have the same bus_id, dds_id
            # and address fields. The two registers written by a set32
            # (`addr` and `addr | 1`) are in the same entry of the mask memory.
            # The memory read (a LUTRAM mux tree) is registered as is,
            # the bit for each register is selected in the next stage.
            dds_mask = Signal(2)
            port = self.dds_mask_port
            if port is not None:
                dds_arg = args.dds_set16
                m.d.top_comb += [port.addr.eq(Cat(dds_arg.addr[1:], dds_arg.dds_id,
                                                  dds_arg.bus_id)),
                                 dds_mask.eq(port.data)]

            return dict(wait_raw=WaitRawDecode(Mux(head.len[0], wait2, wait1)),
                        ttl16=ttl16, ttl32=ttl32, dds_mask=dds_mask)

        @decode_pipe.stage(m, o=[('ttl', TTLDecode), ('wait', WaitDecode),
                                 ('dds', DDSDecode)])
        def decode_3(head, ttl4, ttl16, ttl32, wait_raw, dds, dds_mask):
            # Redirect the disabled DDS writes to the no-op register.
            # `addr1` is the instruction address, `addr2` (only for set32)
            # is the odd register of the same pair.
            dds_mask1 = dds_mask.bit_select(dds.addr1[0], 1)
            dds_mask2 = dds_mask[1]
            dds_out = Signal(DDSDecode)
            m.d.top_comb += [dds_out.eq(dds),
                             dds_out.addr1.eq(Mux(dds_mask1, DDS_NOOP_IDX, dds.addr1)),
                             dds_out.addr2.eq(Mux(dds_mask2, DDS_NOOP_IDX, dds.addr2))]
            return dict(ttl=ttl_select(head, ttl4, ttl16, ttl32),
                        wait=StructCat(WaitDecode, cycle_m1=(wait_raw.cycle - 1)[:28],
                                       is0=wait_raw.is0),
                        dds=dds_out)

        # The ping-pong flavor keeps the (wide) data registers off
        # the read side, whose run condition is on a long path.
        pipeline_regfifo(decode_pipe, pingpong=True)

        decode_pipe.add_external(self.read)

        return m


class DMAInstParser(Elaboratable):
    """Parse the instruction stream into output action groups.

    Up to `INST_BUNDLE_SIZE` instructions (a bundle from the `InstCutter`) are
    consumed per cycle. Consecutive output actions are accumulated into a
    cache and each wait instruction emits the accumulated actions together
    with the wait. A bundle contains at most one wait (see `is_wait_inst`):
    the lanes before it go into the emitted group and the lanes after it
    start the next group.
    """
    def __init__(self, csr, nttl):
        self.nttl = nttl
        TTLDecode = _TTLDecode(nttl)
        OutputAction = _OutputAction(nttl)
        self.TTLDecode = TTLDecode
        self.OutputAction = OutputAction
        self.csr = csr
        self.write = Method(i=INST_BUNDLE)
        self.read = Method(o=[('is_trig', 1), ('wait', WaitAction),
                              ('action', OutputAction)])
        # DDS write disabler configuration (semi-static)
        self.set_dds_mask = Method(i=DDSMaskReq.as_shape())

    def elaborate(self, plat):
        m = TModule()

        TTLDecode = self.TTLDecode
        OutputAction = self.OutputAction
        nlanes = INST_BUNDLE_SIZE

        ## DDS write disabler
        # One disable bit per 16 bit register of each DDS, indexed by
        # (bus_id, dds_id, idx). Stored as pairs of bits (even/odd idx)
        # so that the two registers of a set32 are in the same entry and each
        # lane only needs one read port. Semi-static, configured through
        # the CSR request from the control interface; zero (nothing disabled)
        # at power up (not cleared by reset).
        m.submodules.dds_mask = dds_mask = Memory(shape=unsigned(2), depth=2 * 16 * 32,
                                                  init=[])
        def dds_mask_addr(req):
            return Cat(req.pair, req.dds_id, req.bus_id)

        # The request is registered first to keep the caller's path short
        req = Signal(DDSMaskReq, reset_less=True)
        req_valid = Signal()
        m.d.sync += req_valid.eq(0)

        @def_method(m, self.set_dds_mask)
        def _(arg):
            m.d.sync += [req.eq(arg), req_valid.eq(1)]

        mask_wr = dds_mask.write_port()
        m.d.comb += [mask_wr.addr.eq(dds_mask_addr(req)),
                     mask_wr.data.eq(req.value),
                     mask_wr.en.eq(req_valid & req.we)]
        # Read back of the register selected by the last request
        mask_rd = dds_mask.read_port(domain="comb")
        mask_rd_req = Signal(DDSMaskReq, reset_less=True)
        with m.If(req_valid):
            m.d.sync += mask_rd_req.eq(req)
        m.d.comb += mask_rd.addr.eq(dds_mask_addr(mask_rd_req))
        m.d.sync += self.csr.dds_mask.eq(mask_rd.data)

        decs = []
        for i in range(nlanes):
            # Only the first lane carries the bundle level flag
            dec = DMAInstDecoder(self.csr, self.nttl,
                                 flags_shape=Lane0Flags if i == 0 else LaneFlags,
                                 dds_mask_port=dds_mask.read_port(domain="comb"))
            m.submodules[f'dec{i}'] = dec
            decs.append(dec)

        # All lanes are always written so the decode pipelines stay in lockstep,
        # each lane carries its own flags (precomputed by the cutter so the
        # merge below needs no cross lane logic).
        @def_method(m, self.write)
        def _(arg):
            for i, dec in enumerate(decs):
                flags = dict(en=getattr(arg, f'en{i}'), post=getattr(arg, f'post{i}'))
                if i == 0:
                    flags = StructCat(Lane0Flags, has_wait=arg.has_excl, **flags)
                else:
                    flags = StructCat(LaneFlags, **flags)
                dec.write(m, inst=getattr(arg, f'inst{i}'), flags=flags)

        m.submodules.decoded_fifo = decoded_fifo = BufferedFifo([('is_trig', 1),
                                                                 ('wait', WaitAction),
                                                                 ('action', OutputAction)],
                                                                256)

        m.submodules.out_pipe = out_pipe = PipelineBuilder()

        # Accumulated output actions since the last wait
        g = SimpleNamespace()
        g.clockout_en = Signal()
        g.clockout = Signal(ClockOutDecode, reset_less=True)
        g.ttl_en = Signal()
        g.ttl = Signal(TTLDecode, reset_less=True)
        g.dds0_en = Signal()
        g.dds0 = Signal(DDSDecode, reset_less=True)
        g.dds1_en = Signal()
        g.dds1 = Signal(DDSDecode, reset_less=True)
        g.dac_en = Signal()
        g.dac = Signal(DACDecode, reset_less=True)

        action_names = ('clockout', 'ttl', 'dds0', 'dds1', 'dac')

        def action_payload(d, name):
            if name == 'clockout':
                return d.trivial.clock_out
            elif name == 'ttl':
                return d.ttl
            elif name == 'dac':
                return d.trivial.dac
            return d.dds

        def merge_lanes(name, base_en, base_val, lanes):
            """Merge the action `name` of the decoded `lanes` (list of
            (valid, decoded)) into the (en, value) cache `base`.

            TTL sets accumulate by or-ing the values and masks. For the
            other actions at most one of the sources is active within a
            wait group (more than one is undefined behavior for that
            channel) so they are simply or-ed together as well, keeping
            the merge a flat and-or without any priority logic."""
            hits = [Signal(name=f'{name}_hit{i}') for i in range(len(lanes))]
            for hit, (valid, d) in zip(hits, lanes):
                m.d.top_comb += hit.eq(valid & getattr(d, f'is_{name}'))
            en = base_en | Cat(*hits).any()
            res = Mux(base_en, Value.cast(base_val), 0)
            for hit, (_, d) in zip(hits, lanes):
                res = res | Mux(hit, Value.cast(action_payload(d, name)), 0)
            val = Signal.like(base_val, name=f'{name}_val')
            m.d.top_comb += val.eq(res)
            return en, val

        def merge_all(base, lanes):
            return {name: merge_lanes(name, base[name][0], base[name][1], lanes)
                    for name in action_names}

        def empty_cache():
            return {name: (C(0), C(0, Shape.cast(getattr(g, name).shape()).width))
                    for name in action_names}

        def output_action(cache):
            action = Signal(OutputAction)
            for name in action_names:
                en, val = cache[name]
                m.d.top_comb += [getattr(action, f'{name}_en').eq(en),
                                 getattr(action, name).eq(val)]
            return action

        # Register the decoded lanes before merging them: the four decoders
        # are spread out on the chip and the merge collects from all of them,
        # so this splits the long routes into two stages.
        lane_layouts = [(f'lane{i}', dec.read.layout_out) for i, dec in enumerate(decs)]

        @out_pipe.stage(m, o=lane_layouts)
        def _():
            return {f'lane{i}': dec.read(m) for i, dec in enumerate(decs)}

        @out_pipe.stage(m, i=lane_layouts, o=[('en', 1), ('is_trig', 1),
                                              ('wait', WaitAction),
                                              ('action', OutputAction)])
        def _(arg):
            ds = [getattr(arg, f'lane{i}') for i in range(nlanes)]
            ens = [d.flags.en for d in ds]
            waits = Signal(nlanes)
            m.d.top_comb += waits.eq(Cat(*(en & d.is_wait for en, d in zip(ens, ds))))
            any_wait = ds[0].flags.has_wait
            # Lanes before and after the wait (at most one wait per bundle),
            # a wait lane itself has no action so it can count as before.
            before = [en & ~d.flags.post for en, d in zip(ens, ds)]
            after = [en & d.flags.post for en, d in zip(ens, ds)]

            cache = {name: (getattr(g, f'{name}_en'), getattr(g, name))
                     for name in action_names}
            # Emitted group: the cache and the actions before the wait
            # (all lanes if there is no wait, in which case it is not emitted)
            emitted = merge_all(cache, [(b, d) for b, d in zip(before, ds)])
            # Next cache: the actions after the wait, or the cache and all
            # the actions if there is no wait. Since the actions are or-ed
            # this is a single flat merge with the sources gated by the
            # (registered) wait flag.
            kept = {name: (cache[name][0] & ~any_wait,
                           Mux(any_wait, 0, Value.cast(cache[name][1])))
                    for name in action_names}
            restart = merge_all(kept, [(a | (b & ~any_wait), d)
                                       for a, b, d in zip(after, before, ds)])
            for name in action_names:
                m.d.sync += [getattr(g, f'{name}_en').eq(restart[name][0]),
                             getattr(g, name).eq(restart[name][1])]

            # The wait of the (only) wait lane
            is_trig = Signal()
            wait_action = Signal(WaitAction)
            sel_opcode0 = C(0)
            sel_wait_trig = C(0, Shape.cast(WaitTrigDecode).width)
            sel_wait = C(0, Shape.cast(WaitDecode).width)
            for i, d in enumerate(ds):
                sel_opcode0 = sel_opcode0 | Mux(waits[i], Value.cast(d.opcode)[0], 0)
                sel_wait_trig = sel_wait_trig | Mux(waits[i],
                                                    Value.cast(d.trivial.wait_trig), 0)
                sel_wait = sel_wait | Mux(waits[i], Value.cast(d.wait), 0)
            m.d.top_comb += is_trig.eq(sel_opcode0)
            m.d.av_comb += wait_action.wait_trig.eq(sel_wait_trig)
            with m.If(~is_trig):
                m.d.av_comb += wait_action.wait.eq(sel_wait)

            return dict(en=any_wait, is_trig=is_trig, wait=wait_action,
                        action=output_action(emitted))

        @out_pipe.stage(m)
        def _(en, is_trig, wait, action):
            with m.If(en):
                decoded_fifo.write(m, is_trig=is_trig, wait=wait, action=action)

        self.read.provide(decoded_fifo.read)

        return m


class DMAInstRunner(Elaboratable):
    def __init__(self, pulseio, csr, ioctrl, dmactrl):
        self.pulseio = pulseio
        self.csr = csr
        self.ioctrl = ioctrl
        self.dmactrl = dmactrl

        nttl = len(pulseio.ttlout.o)
        TTLDecode = _TTLDecode(nttl)
        OutputAction = _OutputAction(nttl)
        self.TTLDecode = TTLDecode
        self.OutputAction = OutputAction
        self.write = Method(i=[('is_trig', 1), ('wait', WaitAction),
                               ('action', OutputAction)])
        self.long_wait = Signal()


    def elaborate(self, plat):
        m = TModule()

        m.submodules.inst_conn = inst_conn = Connect([('is_trig', 1),
                                                      ('wait', WaitAction),
                                                      ('action', self.OutputAction)])
        self.write.provide(inst_conn.write)

        class State(enum.Enum):
            FETCH = 0
            WAIT = 1
            TRIG = 2
        state = Signal(State)
        idling = Signal(init=1)
        m.d.sync += idling.eq(0)

        output_action = Signal(self.OutputAction, reset_less=True)
        counter = Signal(28, reset_less=True)
        output_en = Signal()
        m.d.sync += output_en.eq(0)
        with Transaction().body(m, ready=output_en):
            with m.If(output_action.clockout_en):
                self.ioctrl.clockout.set(m, output_action.clockout.period)

            with m.If(output_action.ttl_en):
                self.ioctrl.ttlout.set_mask(m, mask=output_action.ttl.mask,
                                            value=output_action.ttl.val)

            with m.If(output_action.dds0_en):
                self.ioctrl.dds0.set(m, output_action.dds0)

            with m.If(output_action.dds1_en):
                self.ioctrl.dds1.set(m, output_action.dds1)

            with m.If(output_action.dac_en):
                dac = output_action.dac
                self.ioctrl.spi.set(m, data=dac.data << (32 - 18),
                                    div=dac.cycle, nbits_minus_1=17,
                                    result=0, id=dac.id, clk_pha=dac.clk_pha,
                                    clk_pol=dac.clk_pol)

        trig_action = Signal(WaitTrigDecode, reset_less=True)
        trig_en = Signal()
        m.d.sync += trig_en.eq(0)
        with Transaction().body(m, ready=trig_en):
            self.ioctrl.trigger.setup(m, chn=trig_action.chn,
                                      edge=trig_action.edge,
                                      cycle=trig_action.cycle)

        with m.Switch(state):
            with m.Case(State.FETCH):
                assign_xvalue(m, counter)
                fetch_trans = Transaction()
                with fetch_trans.body(m):
                    req = inst_conn.read(m)
                    m.d.sync += output_en.eq(1)
                    top_d(m).sync += output_action.eq(req.action)
                    wait = req.wait.wait
                    with m.If(idling):
                        self.dmactrl.inst_started(m)
                    with m.If(req.is_trig):
                        m.d.sync += [state.eq(State.TRIG),
                                     trig_en.eq(1)]
                        top_d(m).sync += trig_action.eq(req.wait.wait_trig)
                    with m.Elif(~wait.is0):
                        m.d.sync += [counter.eq(wait.cycle_m1),
                                     state.eq(State.WAIT)]
                with m.If(~fetch_trans.run):
                    m.d.sync += idling.eq(1)
                    with Transaction().body(m, ready=~idling):
                        self.dmactrl.inst_stopped(m)

            with m.Case(State.WAIT):
                m.d.sync += [counter.eq(counter - 1),
                             self.long_wait.eq(counter[7:] != 0)] # > 128 cycles
                with m.If(counter == 0):
                    m.d.sync += state.eq(State.FETCH)

            with m.Case(State.TRIG):
                assign_xvalue(m, counter)
                with Transaction().body(m):
                    with m.If(self.ioctrl.trigger.wait(m).timeout):
                        self.dmactrl.trig_timeout(m)
                    m.d.sync += state.eq(State.FETCH)

        return m
