#

from amaranth import *
from amaranth.lib import enum
from amaranth.lib.data import Field, FlexibleLayout, View, StructLayout

from transactron import TModule, Transaction
from transactron.lib import PipelineBuilder
from transactron.lib import BasicFifo

from .dds import SET_ARG as DDS_SET_ARG, DDSReq
from .utils import assign_xvalue, xvalue

class DDSOpCode(enum.Enum, shape=4):
    SET_FREQ = 0
    SET_AMP_PHASE = 1
    SET_TWO_BYTES = 2
    GET_TWO_BYTES = 3
    RESET = 4
    GET_FOUR_BYTES = 14
    SET_FOUR_BYTES = 15

TTL_ARG = FlexibleLayout(59, dict(
    value=Field(unsigned(32), 0),
    timer=Field(unsigned(24), 32),
    bank=Field(unsigned(3), 56),
))
DDS_ARG = FlexibleLayout(59, dict(
    data=Field(unsigned(32), 0),
    opcode=Field(DDSOpCode, 32),
    id=Field(unsigned(5), 32 + 4),
    addr=Field(unsigned(7), 32 + 9),
))
WAIT_ARG = FlexibleLayout(59, dict(
    trig_chn=Field(unsigned(8), 20),
    trig_type=Field(unsigned(4), 28),
    timer=Field(unsigned(24), 32),
))
LOOPBACK_ARG = FlexibleLayout(59, dict(
    data=Field(unsigned(32), 0),
))
CLOCKOUT_ARG = FlexibleLayout(59, dict(
    div=Field(unsigned(8), 0),
))
SPI_ARG = FlexibleLayout(59, dict(
    data=Field(unsigned(18), 0),
    clk_div=Field(unsigned(8), 32),
    save_result=Field(unsigned(1), 32 + 10),
    id=Field(unsigned(2), 32 + 11),
    clk_pha=Field(unsigned(1), 32 + 13),
    clk_pol=Field(unsigned(1), 32 + 14),
))

TTL_DECODE0 = StructLayout(dict(
    value=unsigned(32),
    bank=unsigned(3),
))
DDS_DECODE0 = StructLayout(dict(
    is_dds1=1,
    arg=DDS_SET_ARG,
))
WAIT_DECODE0 = StructLayout(dict(
    trig_chn=unsigned(8),
    trig_type=unsigned(4),
))
SPI_DECODE0 = StructLayout(dict(
    data=unsigned(18),
    clk_div=unsigned(8),
    save_result=unsigned(1),
    id=unsigned(2),
    clk_pha=unsigned(1),
    clk_pol=unsigned(1),
))

class InstOpCode(enum.Enum, shape=3):
    TTL = 0
    DDS = 1
    WAIT = 2
    CLEAR_UNDERFLOW = 3
    LOOPBACK = 4
    CLOCKOUT = 5
    SPI = 6

INST_STRUCT = FlexibleLayout(63, dict(
    ttl=Field(TTL_ARG, 0),
    dds=Field(DDS_ARG, 0),
    wait=Field(WAIT_ARG, 0),
    loopback=Field(LOOPBACK_ARG, 0),
    clockout=Field(CLOCKOUT_ARG, 0),
    spi=Field(SPI_ARG, 0),
    time_check=Field(unsigned(1), 59),
    opcode=Field(InstOpCode, 60),
))
DECODED_INST = StructLayout(dict(op=InstOpCode,
                                 ttl=TTL_DECODE0,
                                 dds=DDS_DECODE0,
                                 wait=WAIT_DECODE0,
                                 loopback=32,
                                 clockout=8,
                                 spi=SPI_DECODE0,
                                 time_check=1,
                                 timer=24))
TIME_STATUS_STRUCT = FlexibleLayout(32, dict(
    underflow=Field(unsigned(1), 0),
    trigger_timeout=Field(unsigned(1), 1),
    pulses_finished=Field(unsigned(1), 2),
    results=Field(unsigned(5), 4),
))

class RunState(enum.Enum):
    FETCH = 0
    EXECUTE = 1
    WAIT = 2
    TRIG_INIT = 3
    TRIG_ARMED = 4

class InstRunner(Elaboratable):
    def __init__(self, pulseio, csr, fifos, ioctrl, *, clock_shift=1):
        self.pulseio = pulseio
        self.csr = csr
        self.fifos = fifos
        self.clock_shift = clock_shift
        self.ioctrl = ioctrl

    def elaborate(self, plat):
        m = TModule()

        ioctrl = self.ioctrl
        bank_width = ioctrl.ttlout.bank_width

        # Run state
        state = Signal(RunState, init=RunState.FETCH)
        check_timing = Signal(1)
        wait_cycle = Signal(24 + self.clock_shift)
        trig_lower_edge = Signal(1)
        trig_chn = Signal(8)
        trig_ttl = self.pulseio.ttlin.i.bit_select(trig_chn, 1)

        # Status
        underflow = Signal(1)
        trigger_timeout = Signal(1)
        pulses_finished = Signal(1, init=1)
        # delay underflow/trigger_timeout/pulses_finished flag by one cycle to match
        # output timing
        timing_status = View(TIME_STATUS_STRUCT, Signal(32, init=0x4))
        m.d.sync += [self.csr.timing_status.eq(timing_status.as_value()),
                     self.csr.dbg_result_count.eq(self.fifos.result_fifo.level),
                     timing_status.underflow.eq(underflow),
                     timing_status.trigger_timeout.eq(trigger_timeout),
                     timing_status.pulses_finished.eq(pulses_finished),
                     timing_status.results.eq(self.fifos.result_fifo.user_level)]

        # Control
        pulse_hold = self.csr.timing_ctrl[7]
        pulse_init = self.csr.timing_ctrl[8]

        force_release = Signal(1)
        with m.If(self.fifos.cmd_fifo.full):
            m.d.sync += force_release.eq(1)

        def shift_cycle_m1(div):
            return Cat(~C(0, self.clock_shift), div)

        m.submodules.decode_pipe = decode_pipe = PipelineBuilder()

        decode_pipe.call_method(self.fifos.cmd_fifo.read)
        @decode_pipe.stage(m)
        def _():
            pass

        @decode_pipe.stage(m, o=DECODED_INST)
        def _(data):
            inst = View(INST_STRUCT, data[:63])
            op = inst.opcode

            ttlarg = inst.ttl
            ttl = Signal(TTL_DECODE0)
            m.d.top_comb += [ttl.value.eq(ttlarg.value),
                             ttl.bank[:bank_width].eq(ttlarg.bank[:bank_width])]
            assign_xvalue(m, ttl.bank[bank_width:], domain='top_comb')

            ddsarg = inst.dds
            is_dds1 = ddsarg.id >= 11
            dds_id = Mux(is_dds1, ddsarg.id - 11, ddsarg.id)

            dds_set_arg = Signal(DDS_SET_ARG)
            def _set_dds_arg(d):
                for (k, v) in d.items():
                    m.d.av_comb += getattr(dds_set_arg, k).eq(v)
            with m.Switch(ddsarg.opcode):
                dds_req = DDSReq(self.csr)
                with m.Case(DDSOpCode.SET_FREQ):
                    _set_dds_arg(dds_req.set_freq(m, id=dds_id, freq=ddsarg.data))
                with m.Case(DDSOpCode.SET_AMP_PHASE):
                    _set_dds_arg(dds_req.set_amp_phase(m, id=dds_id,
                                                       amp=ddsarg.data[:16],
                                                       phase=ddsarg.data[16:]))
                with m.Case(DDSOpCode.SET_TWO_BYTES):
                    _set_dds_arg(dds_req.set_two_bytes(m, id=dds_id,
                                                       addr=ddsarg.addr,
                                                       data=ddsarg.data[:16]))
                with m.Case(DDSOpCode.GET_TWO_BYTES):
                    _set_dds_arg(dds_req.get_two_bytes(m, id=dds_id, addr=ddsarg.addr,
                                                       data1=ddsarg.data[:16],
                                                       data2=ddsarg.data[16:]))
                with m.Case(DDSOpCode.RESET):
                    _set_dds_arg(dds_req.reset(m, id=dds_id, addr1=ddsarg.addr,
                                               data1=ddsarg.data[:16]))
                with m.Case(DDSOpCode.SET_FOUR_BYTES):
                    _set_dds_arg(dds_req.set_four_bytes(m, id=dds_id,
                                                        addr=ddsarg.addr,
                                                        data=ddsarg.data))
                with m.Case(DDSOpCode.GET_FOUR_BYTES):
                    _set_dds_arg(dds_req.get_four_bytes(m, id=dds_id, addr=ddsarg.addr,
                                                        data1=ddsarg.data[:16]))
                with m.Default():
                    assign_xvalue(m, dds_set_arg, domain='av_comb')


            dds = Signal(DDS_DECODE0)
            m.d.top_comb += [dds.is_dds1.eq(is_dds1),
                             dds.arg.eq(dds_set_arg)]

            waitarg = inst.wait
            wait = Signal(WAIT_DECODE0)
            m.d.top_comb += [wait.trig_chn.eq(waitarg.trig_chn),
                             wait.trig_type.eq(waitarg.trig_type)]

            loopback = inst.loopback.data

            clockout_div = inst.clockout.div

            spiarg = inst.spi
            spi = Signal(SPI_DECODE0)
            m.d.top_comb += [spi.data.eq(spiarg.data),
                             spi.clk_div.eq(spiarg.clk_div),
                             spi.save_result.eq(spiarg.save_result),
                             spi.id.eq(spiarg.id),
                             spi.clk_pha.eq(spiarg.clk_pha),
                             spi.clk_pol.eq(spiarg.clk_pol)]

            timer = Signal(24)
            with m.Switch(inst.opcode):
                with m.Case(InstOpCode.TTL):
                    m.d.av_comb += timer.eq(ttlarg.timer)
                with m.Case(InstOpCode.DDS):
                    m.d.av_comb += timer.eq(50)
                with m.Case(InstOpCode.WAIT):
                    m.d.av_comb += timer.eq(waitarg.timer)
                with m.Case(InstOpCode.CLEAR_UNDERFLOW):
                    m.d.av_comb += timer.eq(5)
                with m.Case(InstOpCode.LOOPBACK):
                    m.d.av_comb += timer.eq(5)
                with m.Case(InstOpCode.CLOCKOUT):
                    m.d.av_comb += timer.eq(5)
                with m.Case(InstOpCode.SPI):
                    m.d.av_comb += timer.eq(45)
                with m.Default():
                    assign_xvalue(m, timer, domain='av_comb')

            return dict(op=op, ttl=ttl, dds=dds, wait=wait, loopback=loopback,
                        clockout=clockout_div, spi=spi,
                        time_check=inst.time_check, timer=timer)

        decode_pipe.fifo(depth=2)

        @decode_pipe.stage(m, ready=force_release | ~pulse_hold)
        def _():
            self.csr.dbg_inst_count.count(m)

        decode_pipe.fifo(depth=2)

        read_decoded = decode_pipe.create_external(o=DECODED_INST, i=[])

        def should_wait(cycle):
            if self.clock_shift > 0:
                return 1
            # cycle >= 1
            return (cycle >> 1) != 0

        def start_wait(cycle):
            cycle = cycle << self.clock_shift
            m.d.sync += wait_cycle.eq(cycle)
            with m.If(should_wait(cycle)):
                m.d.sync += state.eq(RunState.WAIT)

        # We usually don't put 0 or 1 in `wait_cycle` during waiting
        # and we'll branch out on `wait_cycle == 2` so when we check for wait ending
        # we'll never deal with 0 or 1, we can therefore skip checking the second bit
        # in the number when checking for `wait_cycle == 2`
        # For clock_shift == 1, we may put 0 in there.
        # However, in this case, we actually do want it to behave like wait_cycle == 2
        # to match the clock_shift == 1 behavior so the check still works.
        wait_end = ((wait_cycle >> 2) == 0) & (wait_cycle[0] == 0)

        exe_inst = Signal(DECODED_INST)
        exe_trig_enable = Signal(1)
        assign_xvalue(m, exe_trig_enable)
        assign_xvalue(m, exe_inst)

        with Transaction().body(m, ready=~pulses_finished):
            self.csr.dbg_inst_cycle.count(m)

        with m.Switch(state):
            with m.Case(RunState.FETCH):
                fetch_inst = Transaction()
                with fetch_inst.body(m):
                    new_inst = read_decoded(m)
                    m.d.sync += exe_inst.eq(new_inst)
                    m.d.sync += [check_timing.eq(new_inst.time_check),
                                 pulses_finished.eq(0)]
                    trig_type = new_inst.wait.trig_type
                    m.d.sync += [exe_trig_enable.eq(trig_type != 0),
                                 trig_lower_edge.eq(trig_type & 1),
                                 trig_chn.eq(new_inst.wait.trig_chn),
                                 wait_cycle.eq(new_inst.timer << self.clock_shift)]

                    if self.clock_shift == 0:
                        with m.If(new_inst.op == InstOpCode.TTL):
                            self.csr.dbg_ttl_count.count(m)
                            ioctrl.ttlout.set_bank_inst(m, bank=new_inst.ttl.bank[:bank_width],
                                                        value=new_inst.ttl.value)
                        with m.If(new_inst.timer >> 1): # timer > 1
                            m.d.sync += state.eq(RunState.EXECUTE)
                        with m.Elif(new_inst.op == InstOpCode.WAIT):
                            self.csr.dbg_wait_count.count(m)
                    else:
                        m.d.sync += state.eq(RunState.EXECUTE)

                with m.If(~fetch_inst.run):
                    m.d.sync += pulses_finished.eq(1)
                    with m.If(check_timing):
                        with Transaction().body(m):
                            self.csr.dbg_underflow_cycle.count(m)
                        m.d.sync += underflow.eq(1)

            with m.Case(RunState.EXECUTE):
                m.d.sync += [wait_cycle.eq(wait_cycle - 1),
                             state.eq(RunState.WAIT)]

                with m.Switch(exe_inst.op):
                    with m.Case(InstOpCode.TTL):
                        if self.clock_shift != 0:
                            with Transaction().body(m):
                                self.csr.dbg_ttl_count.count(m)
                                ioctrl.ttlout.set_bank_inst(m, bank=exe_inst.ttl.bank[:bank_width],
                                                            value=exe_inst.ttl.value)
                    with m.Case(InstOpCode.DDS):
                        with Transaction().body(m):
                            self.csr.dbg_dds_count.count(m)
                            with m.If(exe_inst.dds.is_dds1):
                                ioctrl.dds1.set(m, exe_inst.dds.arg)
                            with m.Else():
                                ioctrl.dds0.set(m, exe_inst.dds.arg)
                    with m.Case(InstOpCode.WAIT):
                        with Transaction().body(m):
                            self.csr.dbg_wait_count.count(m)
                        m.d.sync += state.eq(Mux(exe_trig_enable,
                                                 RunState.TRIG_INIT, RunState.WAIT))
                    with m.Case(InstOpCode.CLEAR_UNDERFLOW):
                        with Transaction().body(m):
                            self.csr.dbg_underflow_cycle.clear(m)
                            self.csr.dbg_clear_count.count(m)
                        m.d.sync += [underflow.eq(0),
                                     trigger_timeout.eq(0)]
                    with m.Case(InstOpCode.LOOPBACK):
                        with Transaction().body(m):
                            self.csr.dbg_loopback_count.count(m)
                            self.fifos.result_fifo.write(m, exe_inst.loopback)
                    with m.Case(InstOpCode.CLOCKOUT):
                        with Transaction().body(m):
                            self.csr.dbg_clock_count.count(m)
                            ioctrl.clockout.set(m, shift_cycle_m1(exe_inst.clockout))
                        m.d.sync += self.csr.clockout_div.eq(exe_inst.clockout)
                    with m.Case(InstOpCode.SPI):
                        with Transaction().body(m):
                            self.csr.dbg_spi_count.count(m)
                            ioctrl.spi.set(m, data=exe_inst.spi.data << (32 - 18),
                                           div=shift_cycle_m1(exe_inst.spi.clk_div),
                                           nbits_minus_1=17,
                                           result=exe_inst.spi.save_result,
                                           id=exe_inst.spi.id,
                                           clk_pha=exe_inst.spi.clk_pha,
                                           clk_pol=exe_inst.spi.clk_pol)
                with m.If(wait_end):
                    m.d.sync += state.eq(RunState.FETCH)

            with m.Case(RunState.WAIT):
                m.d.sync += wait_cycle.eq(wait_cycle - 1)
                with m.If(wait_end):
                    m.d.sync += state.eq(RunState.FETCH)

            with m.Case(RunState.TRIG_INIT):
                m.d.sync += wait_cycle.eq(wait_cycle - 1)
                with m.If(trig_ttl == trig_lower_edge):
                    m.d.sync += state.eq(RunState.TRIG_ARMED)
                with m.Elif(wait_end):
                    m.d.sync += [state.eq(RunState.FETCH),
                                 trigger_timeout.eq(1)]

            with m.Case(RunState.TRIG_ARMED):
                m.d.sync += wait_cycle.eq(wait_cycle - 1)
                with m.If(trig_ttl != trig_lower_edge):
                    m.d.sync += state.eq(RunState.FETCH)
                with m.Elif(wait_end):
                    m.d.sync += [state.eq(RunState.FETCH),
                                 trigger_timeout.eq(1)]

        with Transaction().body(m, ready=pulse_init):
            ioctrl.clockout.set(m, ioctrl.clockout.OFF)
            self.csr.dbg_inst_word_count.clear(m)
            self.csr.dbg_inst_count.clear(m)
            self.csr.dbg_ttl_count.clear(m)
            self.csr.dbg_dds_count.clear(m)
            self.csr.dbg_wait_count.clear(m)
            self.csr.dbg_clear_count.clear(m)
            self.csr.dbg_loopback_count.clear(m)
            self.csr.dbg_clock_count.clear(m)
            self.csr.dbg_spi_count.clear(m)
            self.csr.dbg_underflow_cycle.clear(m)
            self.csr.dbg_inst_cycle.clear(m)
            self.csr.dbg_result_generated.clear(m)
            self.csr.dbg_result_consumed.clear(m)
            # self.csr.dbg_ttl_cycle.clear(m)
            # self.csr.dbg_wait_cycle.clear(m)
            m.d.sync += [state.eq(RunState.FETCH),
                         wait_cycle.eq(0),
                         check_timing.eq(0),
                         underflow.eq(0),
                         trigger_timeout.eq(0),
                         pulses_finished.eq(1),
                         force_release.eq(0),
                         self.csr.clockout_div.eq(255)]

        return m
