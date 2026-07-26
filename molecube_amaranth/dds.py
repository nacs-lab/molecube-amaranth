#

from amaranth import *
from amaranth.lib import enum
from amaranth.lib.data import StructLayout
from amaranth.lib.memory import Memory

from transactron import TModule, Transaction, Method, def_method

from .utils import xvalue, assign_xvalue, oring_combiner, reg_chain


class FSMState(enum.Enum):
    IDLE = 0

    WR_ADSETUP1 = 1 # Write address/data setup 1
    WR_ENABLE1 = 2 # Write enable asserted 1
    WR_ADHOLD1 = 3 # Write address/data hold after write deassert 1
    WR_ADSETUP2 = 4 # Write address/data setup 2
    WR_ENABLE2 = 5 # Write enable asserted 2
    WR_FUDWAIT = 6 # Write wait before IO update
    WR_FINALHOLD = 7 # Write IO update hold

    RESET = 8 # Resetting

    RD_ASETUP1 = 9 # Read address setup 1
    RD_DELAY1 = 10 # Read delay before second read
    RD_ASETUP2 = 11 # Read address setup 2
    RD_FINISH = 12 # Done reading

SET_ARG = StructLayout(dict(
    state=FSMState,
    id=4,
    hold_cnt=5,
    hold_end=1,
    read=1,
    reset=1,
    fud=1,
    addr1=6,
    addr2=6,
    data1=16,
    data2=16,
))

class DDSReq:
    def __init__(self, csr):
        self.csr = csr

    def write1(self, m, *, id, addr1, data1, fud=1):
        return dict(state=FSMState.WR_ADSETUP2,
                    id=id,
                    hold_cnt=Cat(self.csr.dds_write_adsu, C(0, 2)),
                    hold_end=self.csr.dds_write_adsu_iszero,
                    read=0, reset=0, fud=fud,
                    addr1=addr1, data1=data1, addr2=xvalue(m, 6), data2=xvalue(m, 16))

    def write2(self, m, *, id, addr1, data1, addr2, data2, fud=1):
        return dict(state=FSMState.WR_ADSETUP1,
                    id=id,
                    hold_cnt=Cat(self.csr.dds_write_adsu, C(0, 2)),
                    hold_end=self.csr.dds_write_adsu_iszero,
                    read=0, reset=0, fud=fud,
                    addr1=addr1, data1=data1, addr2=addr2, data2=data2)

    def set_freq(self, m, *, id, freq):
        return self.write2(m, id=id, addr1=0x2d >> 1, data1=freq[:16],
                           addr2=0x2f >> 1, data2=freq[16:])

    def set_amp_phase(self, m, *, id, amp, phase):
        return self.write2(m, id=id, addr1=0x33 >> 1, data1=C(0, 16) | amp,
                           addr2=0x31 >> 1, data2=phase)

    def set_two_bytes(self, m, *, id, addr, data):
        return self.write1(m, id=id, addr1=addr >> 1, data1=data)

    def set_four_bytes(self, m, *, id, addr, data):
        addr_2 = (addr >> 1) | 1
        return self.write2(m, id=id, addr1=addr >> 1, data1=data[:16],
                           addr2=addr_2, data2=data[16:])

    def reset(self, m, *, id, addr1=0, data1=0):
        return dict(state=FSMState.RESET,
                    id=id,
                    hold_cnt=self.csr.dds_reset_rshd,
                    hold_end=self.csr.dds_reset_rshd_iszero,
                    read=0, reset=1, fud=xvalue(m, 1),
                    addr1=addr1 >> 1, data1=data1,
                    addr2=xvalue(m, 6), data2=xvalue(m, 16))

    def get_two_bytes(self, m, *, id, addr, data1=0, data2=0):
        return dict(state=FSMState.RD_ASETUP2,
                    id=id,
                    hold_cnt=self.csr.dds_read_asu,
                    hold_end=self.csr.dds_read_asu_iszero,
                    read=1, reset=0, fud=xvalue(m, 1),
                    addr1=addr >> 1, data1=data1,
                    addr2=xvalue(m, 6), data2=data2)

    def get_four_bytes(self, m, *, id, addr, data1=0):
        addr_2 = (addr >> 1) | 1
        return dict(state=FSMState.RD_ASETUP1,
                    id=id,
                    hold_cnt=self.csr.dds_read_asu,
                    hold_end=self.csr.dds_read_asu_iszero,
                    read=1, reset=0, fud=xvalue(m, 1),
                    addr1=addr_2, data1=data1,
                    addr2=addr >> 1, data2=xvalue(m, 16))

class DDSController(Elaboratable):
    def __init__(self, ddsio, result_fifo, csr, *, bus_id=0):
        self.ddsio = ddsio
        self.result_fifo = result_fifo
        self.csr = csr
        self.bus_id = bus_id

        self.set = Method(i=SET_ARG)
        self.read_dds_cache = Method(i=[('id', 4), ('addr', 6)])
        self.busy = Signal()

    def elaborate(self, plat):
        m = TModule()

        ddsio = self.ddsio

        if self.bus_id == 0:
            dds_reg_out = self.csr.dds0_reg
        else:
            assert self.bus_id == 1
            dds_reg_out = self.csr.dds1_reg

        _, dds_reg_out = reg_chain(m, output=dds_reg_out, levels=2,
                                   reset_input=False, reset_mid=False)

        dds_reset = Signal(1)
        dds_rd = Signal(1)
        dds_wr = Signal(1)
        dds_fud = Signal(1)
        dds_cs = Signal(11)
        dds_id = Signal(4)
        dds_need_fud = Signal(1)

        dds_addr = Signal(6)
        dds_data_oe = Signal(1, init=1) # output by default
        dds_data_out = Signal(16)
        dds_data_in = Signal(16)

        m.d.comb += [ddsio.addr.o.eq(Cat(1, dds_addr)),
                     ddsio.data.oe.eq(dds_data_oe),
                     ddsio.data.o.eq(dds_data_out),
                     dds_data_in.eq(ddsio.data.i),
                     ddsio.wrb.o.eq(~dds_wr),
                     ddsio.rdb.o.eq(~dds_rd),
                     ddsio.reset.o.eq(dds_reset),
                     ddsio.fud.o.eq(dds_fud),
                     ddsio.cs.o.eq(~dds_cs)]

        fsm_state = Signal(FSMState)
        hold_cnt = Signal(5, reset_less=True)
        hold_end = Signal(reset_less=True)

        dds_next_addr = Signal(6)
        dds_next_data = Signal(16)

        m.submodules.regs_cache = regs_cache = Memory(shape=unsigned(16),
                                                      depth=11 << 6, init=[])

        wr_cache = regs_cache.write_port()

        wr_cache_en = Signal()
        wr_cache_addr = Signal(6 + 4, reset_less=True)
        wr_cache_data = Signal(16, reset_less=True)

        reg_chain(m, input=wr_cache_en,
                  output=wr_cache.en, levels=2)
        reg_chain(m, input=Cat(wr_cache_addr, wr_cache_data),
                  output=Cat(wr_cache.addr, wr_cache.data), levels=2,
                  reset_mid=False)

        m.d.sync += wr_cache_en.eq(0)
        assign_xvalue(m, wr_cache_addr)
        assign_xvalue(m, wr_cache_data)
        def do_cache(data):
            m.d.sync += [wr_cache_en.eq(1),
                         wr_cache_addr.eq(Cat(dds_addr, dds_id)),
                         wr_cache_data.eq(data)]

        rd_cache = regs_cache.read_port()
        m.d.comb += rd_cache.en.eq(1)

        rd_cache_en = Signal()
        rd_cache_valid = Signal()
        m.d.sync += [rd_cache_valid.eq(rd_cache_en),
                     rd_cache_en.eq(0)]
        with m.If(rd_cache_valid):
            m.d.sync += dds_reg_out.eq(rd_cache.data)
        assign_xvalue(m, rd_cache.addr)

        @def_method(m, self.read_dds_cache, singlecaller=True)
        def _(id, addr):
            m.d.sync += [rd_cache_en.eq(1),
                         rd_cache.addr.eq(Cat(addr, id))]

        ## DDS parallel write sequence:
        # 1. setup address and data
        #    address needs to be valid before write enable is asserted while data doesn't
        #    however, it's also fine for data to be valid then.
        #    Given that the timing requirement for address and data to be held valid
        #    after write enable is deasserted is the same
        #    (both 0 ns, i.e both data and address needs to be valid
        #    when the deassert happens but not after), it should be fine (and preferred)
        #    to simply setup both the address and the data at the same time.
        # 2. assert write enable
        # 3. deassert write enable
        # 4. address and data can be updated for the next write
        # 5. After the last write, IO_UPDATE signal needs to be toggled on and off
        #    to make the update actually happen.
        #
        # There are 5 time intervals that we could configure here
        # * address+data setup to write enable assertion
        # * write assertion length
        # * write deassition to address+data change
        # * write deassition to IO_UPDATE assertion
        # * IO_UPDATE assertion length

        ## DDS parallel read sequence:
        # 1. setup address and assert read enable
        # 2. read the data and deassert read enable
        # 3. set the data pin to output mode again
        #
        # For multiple read we may or may not need to deassert and reassert read enable

        final_result = Signal(32, reset_less=True)
        write_result = Signal(1)
        assign_xvalue(m, final_result)

        with m.If(~hold_end):
            m.d.sync += [hold_cnt.eq(hold_cnt - 1),
                         hold_end.eq(hold_cnt[1:] == 0)]
            with m.Switch(fsm_state):
                with m.Case(FSMState.IDLE):
                    assign_xvalue(m, hold_cnt)
                    assign_xvalue(m, hold_end)
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)

                with m.Case(FSMState.WR_ADSETUP2):
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                with m.Case(FSMState.WR_ENABLE2):
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                with m.Case(FSMState.WR_FUDWAIT):
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)
                with m.Case(FSMState.WR_FINALHOLD):
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)

                with m.Case(FSMState.RESET):
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)

                with m.Case(FSMState.RD_DELAY1):
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_need_fud)
                with m.Case(FSMState.RD_ASETUP2):
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_need_fud)
                with m.Case(FSMState.RD_FINISH):
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)
        with m.Else():
            with m.Switch(fsm_state):
                with m.Case(FSMState.WR_ADSETUP1):
                    # Assert write enable
                    m.d.sync += [fsm_state.eq(FSMState.WR_ENABLE1),
                                 hold_cnt.eq(self.csr.dds_write_wrlow),
                                 hold_end.eq(self.csr.dds_write_wrlow_iszero),
                                 dds_wr.eq(1)]
                    do_cache(dds_data_out)
                with m.Case(FSMState.WR_ENABLE1):
                    # Deassert write enable
                    m.d.sync += [fsm_state.eq(FSMState.WR_ADHOLD1),
                                 hold_cnt.eq(self.csr.dds_write_adhd),
                                 hold_end.eq(self.csr.dds_write_adhd_iszero),
                                 dds_wr.eq(0)]
                with m.Case(FSMState.WR_ADHOLD1):
                    # Setup next address/data
                    m.d.sync += [fsm_state.eq(FSMState.WR_ADSETUP2),
                                 hold_cnt.eq(self.csr.dds_write_adsu),
                                 hold_end.eq(self.csr.dds_write_adsu_iszero),
                                 dds_addr.eq(dds_next_addr),
                                 dds_data_out.eq(dds_next_data)]
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                with m.Case(FSMState.WR_ADSETUP2):
                    # Assert write enable
                    m.d.sync += [fsm_state.eq(FSMState.WR_ENABLE2),
                                 hold_cnt.eq(self.csr.dds_write_wrlow),
                                 hold_end.eq(self.csr.dds_write_wrlow_iszero),
                                 dds_wr.eq(1)]
                    do_cache(dds_data_out)
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                with m.Case(FSMState.WR_ENABLE2):
                    # Deassert write enable
                    m.d.sync += [fsm_state.eq(Mux(dds_need_fud, FSMState.WR_FUDWAIT,
                                                  FSMState.WR_FINALHOLD)),
                                 hold_cnt.eq(Mux(dds_need_fud, self.csr.dds_write_fuddl,
                                                 self.csr.dds_write_adhd)),
                                 hold_end.eq(Mux(dds_need_fud,
                                                 self.csr.dds_write_fuddl_iszero,
                                                 self.csr.dds_write_adhd_iszero)),
                                 dds_wr.eq(0)]
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)
                with m.Case(FSMState.WR_FUDWAIT):
                    # Assert IO update
                    m.d.sync += [fsm_state.eq(FSMState.WR_FINALHOLD),
                                 hold_cnt.eq(self.csr.dds_write_fudhd),
                                 hold_end.eq(self.csr.dds_write_fudhd_iszero),
                                 dds_fud.eq(1)]
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)
                with m.Case(FSMState.WR_FINALHOLD):
                    # Deassert IO update
                    m.d.sync += [fsm_state.eq(FSMState.IDLE),
                                 dds_cs.eq(0),
                                 dds_fud.eq(0),
                                 dds_addr.eq(0),
                                 dds_data_out.eq(0),
                                 self.busy.eq(0)]
                    assign_xvalue(m, hold_cnt)
                    assign_xvalue(m, hold_end)
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)

                with m.Case(FSMState.RESET):
                    # Done reset
                    m.d.sync += [fsm_state.eq(FSMState.IDLE),
                                 dds_cs.eq(0),
                                 dds_reset.eq(0),
                                 self.busy.eq(0)]
                    assign_xvalue(m, hold_cnt)
                    assign_xvalue(m, hold_end)
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)

                with m.Case(FSMState.RD_ASETUP1):
                    # Setup address and read enable
                    m.d.sync += [fsm_state.eq(FSMState.RD_DELAY1),
                                 hold_cnt.eq(self.csr.dds_read_rdl),
                                 hold_end.eq(self.csr.dds_read_rdl_iszero),
                                 dds_rd.eq(0),
                                 dds_next_data.eq(dds_data_in),
                                 dds_addr.eq(dds_next_addr)]
                    do_cache(dds_data_in)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_need_fud)
                with m.Case(FSMState.RD_DELAY1):
                    m.d.sync += [fsm_state.eq(FSMState.RD_ASETUP2),
                                 hold_cnt.eq(self.csr.dds_read_asu),
                                 hold_end.eq(self.csr.dds_read_asu_iszero),
                                 dds_rd.eq(1)]
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_need_fud)
                with m.Case(FSMState.RD_ASETUP2):
                    m.d.sync += [fsm_state.eq(FSMState.RD_FINISH),
                                 hold_cnt.eq(self.csr.dds_read_rdhoz),
                                 hold_end.eq(self.csr.dds_read_rdhoz_iszero),
                                 dds_rd.eq(0),
                                 dds_addr.eq(0),
                                 write_result.eq(1),
                                 final_result.eq(Cat(dds_data_in, dds_next_data))]
                    do_cache(dds_data_in)
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)
                with m.Case(FSMState.RD_FINISH):
                    m.d.sync += [fsm_state.eq(FSMState.IDLE),
                                 dds_cs.eq(0),
                                 dds_data_oe.eq(1),
                                 self.busy.eq(0)]
                    assign_xvalue(m, hold_cnt)
                    assign_xvalue(m, hold_end)
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)
                with m.Default():
                    assign_xvalue(m, hold_cnt)
                    assign_xvalue(m, hold_end)
                    assign_xvalue(m, dds_next_data)
                    assign_xvalue(m, dds_next_addr)
                    assign_xvalue(m, dds_id)
                    assign_xvalue(m, dds_need_fud)

        @def_method(m, self.set, combiner=oring_combiner, nonexclusive=True)
        def _(arg):
            m.d.sync += [fsm_state.eq(arg.state),
                         self.busy.eq(1),
                         hold_cnt.eq(arg.hold_cnt),
                         hold_end.eq(arg.hold_end),
                         dds_rd.eq(arg.read),
                         dds_id.eq(arg.id),
                         dds_cs.eq(1 << arg.id),
                         dds_reset.eq(arg.reset),
                         dds_need_fud.eq(arg.fud),
                         dds_addr.eq(arg.addr1),
                         dds_data_out.eq(arg.data1),
                         dds_next_addr.eq(arg.addr2),
                         dds_next_data.eq(arg.data2),
                         dds_data_oe.eq(~arg.read)]

        with Transaction().body(m, ready=write_result):
            self.result_fifo.write(m, final_result)
            m.d.sync += write_result.eq(0)

        return m
