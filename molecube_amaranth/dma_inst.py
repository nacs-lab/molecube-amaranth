#

from amaranth import *
from amaranth.lib import enum
from amaranth.lib.data import Struct, Union, ArrayLayout, View
from amaranth.utils import ceil_log2

from transactron import TModule, Transaction, Method
from transactron.lib import PipelineBuilder, Connect

from amaranth_axi.utils import StructCat

from .dds import SET_ARG as DDS_SET_ARG, DDSReq
from .fifo import BufferedFifo
from .utils import assign_xvalue
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

class WaitDecode(Struct):
    cycle: 28
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

class DMAInstParser(Elaboratable):
    def __init__(self, csr, nttl):
        self.nttl = nttl
        TTLDecode = _TTLDecode(nttl)
        OutputAction = _OutputAction(nttl)
        self.TTLDecode = TTLDecode
        self.OutputAction = OutputAction
        self.csr = csr
        self.write = Method(i=[('inst', 48)])
        self.read = Method(o=[('is_trig', 1), ('wait', WaitAction),
                              ('action', OutputAction)])

    def elaborate(self, plat):
        m = TModule()

        ## TTL
        nttl = self.nttl
        nttl_width = ceil_log2(nttl)
        nttl_total = 1 << nttl_width
        ttl_mask = self.csr.dma_ttl_mask[:nttl]

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

        @decode_pipe.stage(m, o=[('head', InstHead), ('args', InstArgs)])
        def _(inst):
            return dict(head=InstHead(inst[:4]),
                        args=InstArgs(inst[4:]))

        @decode_pipe.stage(m, o=[('wait1', WaitDecode), ('wait2', WaitDecode)])
        def decode_wait_1(args):
            return dict(wait1=StructCat(WaitDecode, cycle=args.wait1.cycle,
                                        is0=args.wait1.cycle == 0),
                        wait2=StructCat(WaitDecode, cycle=args.wait2.cycle,
                                        is0=args.wait2.cycle == 0))

        @decode_pipe.stage(m, o=[('wait', WaitDecode)])
        def decode_wait_2(head, wait1, wait2):
            return WaitDecode(Mux(head.len[0], wait2, wait1))

        @decode_pipe.stage(m, o=[('ttl4', TTLDecode), ('ttl16', TTLDecode),
                                 ('ttl32', TTLDecode)])
        def decode_ttl_1(args):
            ttl4 = Signal(TTLDecode)
            ttl16 = Signal(TTLDecode)
            ttl32 = Signal(TTLDecode)

            bank4_4 = ttl_banks(ttl4.val, 4)
            mask4_4 = ttl_banks(ttl4.mask, 4)
            ttl4_bank4_1 = args.ttl_set4.bank4_1[:nttl_width - 2]
            m.d.top_comb += [bank4_4[ttl4_bank4_1].eq(args.ttl_set4.val1),
                             mask4_4[ttl4_bank4_1].eq(~C(0, 4))]

            bank16_8 = ttl_banks(ttl16.val, 8)
            mask16_8 = ttl_banks(ttl16.mask, 8)
            ttl16_bank8_1 = args.ttl_set16.bank8_1[:nttl_width - 3]
            m.d.top_comb += [bank16_8[ttl16_bank8_1].eq(args.ttl_set16.val1),
                             mask16_8[ttl16_bank8_1].eq(~C(0, 8))]

            bank32_16 = ttl_banks(ttl32.val, 16)
            mask32_16 = ttl_banks(ttl32.mask, 16)
            ttl32_bank16_1 = args.ttl_set32.bank16_1[:nttl_width - 4]
            m.d.top_comb += [bank32_16[ttl32_bank16_1].eq(args.ttl_set32.val1),
                             mask32_16[ttl32_bank16_1].eq(~C(0, 16))]

            return dict(ttl4=ttl4, ttl16=ttl16, ttl32=ttl32)

        @decode_pipe.stage(m, o=[('ttl16', TTLDecode), ('ttl32', TTLDecode)])
        def decode_ttl_2(args, ttl16, ttl32):
            _ttl16 = Signal(TTLDecode)
            _ttl32 = Signal(TTLDecode)
            m.d.top_comb += [_ttl16.eq(ttl16), _ttl32.eq(ttl32)]
            ttl16 = _ttl16
            ttl32 = _ttl32

            bank16_8 = ttl_banks(ttl16.val, 8)
            mask16_8 = ttl_banks(ttl16.mask, 8)
            ttl16_bank8_2 = args.ttl_set16.bank8_2[:nttl_width - 3]
            m.d.top_comb += [bank16_8[ttl16_bank8_2].eq(args.ttl_set16.val2),
                             mask16_8[ttl16_bank8_2].eq(~C(0, 8))]

            bank32_16 = ttl_banks(ttl32.val, 16)
            mask32_16 = ttl_banks(ttl32.mask, 16)
            ttl32_bank16_2 = args.ttl_set32.bank16_2[:nttl_width - 4]
            m.d.top_comb += [bank32_16[ttl32_bank16_2].eq(args.ttl_set32.val2),
                             mask32_16[ttl32_bank16_2].eq(~C(0, 16))]

            return dict(ttl16=ttl16, ttl32=ttl32)

        @decode_pipe.stage(m, o=[('ttl16', TTLDecode)])
        def decode_ttl_3(head, ttl4, ttl16):
            return TTLDecode(Mux(head.len[0], ttl16, ttl4))

        @decode_pipe.stage(m, o=[('ttl', TTLDecode)])
        def decode_ttl_4(head, ttl16, ttl32):
            return TTLDecode(Mux(head.len[1], ttl32, ttl16))

        @decode_pipe.stage(m, o=[('ttl', TTLDecode)])
        def decode_ttl_5(head, ttl):
            # The user should not specify any value outside of the mask that are on
            # so we don't need to mask the value, only the mask
            return StructCat(TTLDecode, val=ttl.val, mask=ttl.mask & ttl_mask)

        @decode_pipe.stage(m, o=[('dds', DDSDecode), ('dds_bus_id', 1)])
        def decode_dds(head, args):
            dds_set16 = args.dds_set16
            dds_set32 = args.dds_set32

            # We assume the bus_id bit are the same one for set 16 and set 32
            dds_bus_id = args.dds_set16.bus_id
            dds16 = dds_req.write1(m, id=dds_set16.dds_id, addr1=dds_set16.addr[1:],
                                   data1=dds_set16.data, fud=dds_set16.fud)
            dds32 = dds_req.write2(m, id=dds_set32.dds_id, addr1=dds_set32.addr[1:],
                                   data1=dds_set32.data[:16],
                                   addr2=Cat(C(1, 1), dds_set32.addr[2:]),
                                   data2=dds_set32.data[16:], fud=dds_set32.fud)
            dds16 = StructCat(DDSDecode, **dds16)
            dds32 = StructCat(DDSDecode, **dds32)
            return dict(dds_bus_id=dds_bus_id,
                        dds=DDSDecode(Mux(head.len[1], dds32, dds16)))

        @decode_pipe.stage(m, o=[('trivial', TrivialDecode)])
        def decode_trivial(args):
            return TrivialDecode(Signal.cast(args)[:TrivialDecode.as_shape().size])

        @decode_pipe.stage(m, o=[('opcode', DecodedOpCode)])
        def decode_opcode(head, dds_bus_id):
            opcode = Signal(DecodedOpCode)
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
            return opcode

        decode_pipe.fifo(depth=2)

        OutputAction = self.OutputAction

        m.submodules.decoded_fifo = decoded_fifo = BufferedFifo([('is_trig', 1),
                                                                 ('wait', WaitAction),
                                                                 ('action', OutputAction)],
                                                                17)

        @decode_pipe.stage(m)
        def _(opcode, trivial, wait, ttl, dds):
            output_cache = Signal(OutputAction)
            wait_action = Signal(WaitAction)

            # Only valid if the opcode is actually wait or wait_trig
            is_trig = Value.cast(opcode)[0]
            m.d.av_comb += wait_action.wait_trig.eq(trivial.wait_trig)
            with m.If(~is_trig):
                m.d.av_comb += wait_action.wait.eq(wait)

            with m.Switch(opcode):
                with m.Case(DecodedOpCode.WAIT, DecodedOpCode.WAIT_TRIG):
                    decoded_fifo.write(m, is_trig=is_trig, wait=wait_action,
                                       action=output_cache)
                    for output in ('clockout', 'ttl', 'dds0', 'dds1', 'dac'):
                        assign_xvalue(m, getattr(output_cache, output))
                        m.d.sync += getattr(output_cache, f'{output}_en').eq(0)
                with m.Case(DecodedOpCode.CLOCKOUT):
                    m.d.sync += [output_cache.clockout_en.eq(1),
                                 output_cache.clockout.eq(trivial.clock_out)]
                with m.Case(DecodedOpCode.TTL):
                    cache_ttl = Signal.cast(output_cache.ttl)
                    cmd_ttl = Signal.cast(ttl)
                    m.d.sync += [output_cache.ttl_en.eq(1),
                                 cache_ttl.eq(Mux(output_cache.ttl_en,
                                                  cache_ttl | cmd_ttl, cmd_ttl))]
                with m.Case(DecodedOpCode.DDS0):
                    m.d.sync += [output_cache.dds0_en.eq(1),
                                 output_cache.dds0.eq(dds)]
                with m.Case(DecodedOpCode.DDS1):
                    m.d.sync += [output_cache.dds1_en.eq(1),
                                 output_cache.dds1.eq(dds)]
                with m.Case(DecodedOpCode.DAC):
                    m.d.sync += [output_cache.dac_en.eq(1),
                                 output_cache.dac.eq(trivial.dac)]
                with m.Default():
                    assign_xvalue(m, output_cache)

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

        output_action = Signal(self.OutputAction)
        counter = Signal(28)
        output_en = Signal()
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

        with m.Switch(state):
            with m.Case(State.FETCH):
                assign_xvalue(m, counter)
                fetch_trans = Transaction()
                with fetch_trans.body(m):
                    req = inst_conn.read(m)
                    m.d.sync += [output_action.eq(req.action),
                                 output_en.eq(1)]
                    wait = req.wait.wait
                    with m.If(idling):
                        self.dmactrl.inst_started(m)
                    with m.If(req.is_trig):
                        wait_trig = req.wait.wait_trig
                        self.ioctrl.trigger.setup(m, chn=wait_trig.chn,
                                                  edge=wait_trig.edge,
                                                  cycle=wait_trig.cycle)
                        m.d.sync += state.eq(State.TRIG)
                    with m.Elif(~wait.is0):
                        m.d.sync += [counter.eq(wait.cycle - 1),
                                     state.eq(State.WAIT)]
                with m.If(~fetch_trans.run):
                    m.d.sync += [idling.eq(1),
                                 output_en.eq(0)]
                    assign_xvalue(m, output_action)
                    with Transaction().body(m, ready=~idling):
                        self.dmactrl.inst_stopped(m)

            with m.Case(State.WAIT):
                assign_xvalue(m, output_action)
                m.d.sync += [counter.eq(counter - 1),
                             output_en.eq(0),
                             self.long_wait.eq(counter[7:] != 0)] # > 128 cycles
                with m.If(counter == 0):
                    m.d.sync += state.eq(State.FETCH)

            with m.Case(State.TRIG):
                m.d.sync += output_en.eq(0)
                assign_xvalue(m, output_action)
                assign_xvalue(m, counter)
                with Transaction().body(m):
                    with m.If(self.ioctrl.trigger.wait(m).timeout):
                        self.dmactrl.trig_timeout(m)
                    m.d.sync += state.eq(State.FETCH)

        return m
