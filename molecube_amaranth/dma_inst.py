#

from amaranth import *
from amaranth.lib import enum
from amaranth.lib.data import Struct, Union, ArrayLayout, View
from amaranth.utils import ceil_log2

from transactron import TModule, Transaction, Method, def_method
from transactron.lib import PipelineBuilder, Connect

from amaranth_axi.utils import StructCat

from types import SimpleNamespace

from .dds import SET_ARG as DDS_SET_ARG, DDSReq
from .fifo import BufferedFifo, pipeline_regfifo
from .inst_cutter import INST_BUNDLE
from .utils import assign_xvalue, xvalue, top_d
from .trigger import TriggerController

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

class DMAInstDecoder(Elaboratable):
    """Decode a single instruction stream (one lane)."""
    def __init__(self, csr, nttl, flags_shape=1):
        """
        flags_shape: shape of the `flags` field passed through the decoder unchanged
        """
        self.csr = csr
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
                                 ('ttl16', TTLDecode), ('ttl32', TTLDecode)])
        def decode_2(head, args, wait1, wait2, ttl16, ttl32):
            ttl16, ttl32 = ttl_bank2(args, ttl16, ttl32)
            return dict(wait_raw=WaitRawDecode(Mux(head.len[0], wait2, wait1)),
                        ttl16=ttl16, ttl32=ttl32)

        @decode_pipe.stage(m, o=[('ttl', TTLDecode), ('wait', WaitDecode)])
        def decode_3(head, ttl4, ttl16, ttl32, wait_raw):
            return dict(ttl=ttl_select(head, ttl4, ttl16, ttl32),
                        wait=StructCat(WaitDecode, cycle_m1=(wait_raw.cycle - 1)[:28],
                                       is0=wait_raw.is0))

        pipeline_regfifo(decode_pipe)

        decode_pipe.add_external(self.read)

        return m


class DMAInstParser(Elaboratable):
    """Parse the instruction stream into output action groups.

    Up to two instructions (a bundle from the `InstCutter`) are consumed
    per cycle. Consecutive output actions are accumulated into a cache
    and each wait instruction emits the accumulated actions together with
    the wait. A bundle must not contain two waits (see `is_wait_inst`).
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

    def elaborate(self, plat):
        m = TModule()

        TTLDecode = self.TTLDecode
        OutputAction = self.OutputAction

        m.submodules.dec0 = dec0 = DMAInstDecoder(self.csr, self.nttl)
        m.submodules.dec1 = dec1 = DMAInstDecoder(self.csr, self.nttl)

        # Both lanes are always written so the two decode pipelines stay in lockstep.
        # Lane 0 carries the flag telling whether lane 1 holds a valid instruction.
        @def_method(m, self.write)
        def _(inst0, inst1, en1):
            dec0.write(m, inst=inst0, flags=en1)
            dec1.write(m, inst=inst1, flags=0)

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

        def apply_lane(d, valid, base):
            # Merge the action from a decoded lane into the (en, value) cache `base`
            res = {}
            for name in action_names:
                base_en, base_val = base[name]
                hit = Signal(name=f'{name}_hit')
                m.d.top_comb += hit.eq(valid & getattr(d, f'is_{name}'))
                new_val = Value.cast(action_payload(d, name))
                if name == 'ttl':
                    # TTL sets are accumulated
                    new_val = Mux(base_en, Value.cast(base_val) | new_val, new_val)
                val = Signal.like(base_val, name=f'{name}_val')
                m.d.top_comb += val.eq(Mux(hit, new_val, Value.cast(base_val)))
                res[name] = (base_en | hit, val)
            return res

        def empty_cache():
            return {name: (C(0), xvalue(m, Shape.cast(getattr(g, name).shape()).width))
                    for name in action_names}

        def output_action(cache):
            action = Signal(OutputAction)
            for name in action_names:
                en, val = cache[name]
                m.d.top_comb += [getattr(action, f'{name}_en').eq(en),
                                 getattr(action, name).eq(val)]
            return action

        @out_pipe.stage(m, o=[('en', 1), ('is_trig', 1), ('wait', WaitAction),
                              ('action', OutputAction)])
        def _():
            d0 = dec0.read(m)
            d1 = dec1.read(m)
            en1 = d0.flags

            wait0 = Signal()
            wait1 = Signal()
            m.d.top_comb += [wait0.eq(d0.is_wait),
                             wait1.eq(en1 & d1.is_wait)]

            cache = {name: (getattr(g, f'{name}_en'), getattr(g, name))
                     for name in action_names}
            # Cache after lane 0 (if it is an action)
            cache0 = apply_lane(d0, ~wait0, cache)
            # If lane 0 is a wait, it emits the cache and lane 1 starts a new group.
            # Otherwise lane 1 (if a wait) emits the cache including lane 0.
            emitted = {name: (Mux(wait0, cache[name][0], cache0[name][0]),
                              Mux(wait0, Value.cast(cache[name][1]),
                                  Value.cast(cache0[name][1])))
                       for name in action_names}
            empty = empty_cache()
            base1 = {name: (Mux(wait0, empty[name][0], cache0[name][0]),
                            Mux(wait0, empty[name][1], Value.cast(cache0[name][1])))
                     for name in action_names}
            cache1 = apply_lane(d1, en1, base1)
            for name in action_names:
                m.d.sync += [getattr(g, f'{name}_en').eq(Mux(wait1, 0, cache1[name][0])),
                             getattr(g, name).eq(Mux(wait1, empty[name][1],
                                                     cache1[name][1]))]

            # The wait of the emitting lane
            wait_opcode = Mux(wait0, Value.cast(d0.opcode), Value.cast(d1.opcode))
            is_trig = wait_opcode[0]
            wait_action = Signal(WaitAction)
            m.d.av_comb += wait_action.wait_trig.eq(Mux(wait0, d0.trivial.wait_trig,
                                                        d1.trivial.wait_trig))
            with m.If(~is_trig):
                m.d.av_comb += wait_action.wait.eq(Mux(wait0, d0.wait, d1.wait))

            return dict(en=wait0 | wait1, is_trig=is_trig, wait=wait_action,
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
