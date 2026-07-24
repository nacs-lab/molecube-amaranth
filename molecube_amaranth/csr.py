#

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amaranth.lib.data import Struct

from transactron import TModule, Method, def_method

def cast_to_width(s, width, allow_trunc=False):
    s = Value.cast(s)
    l = len(s)
    if l == width:
        return s
    elif l > width:
        if not allow_trunc:
            raise TypeError("Signal truncation not allowed")
        return s[:width]
    else:
        return Cat(s, Signal(width - l))

class Counter(wiring.Component):
    def __init__(self, width):
        super().__init__({'value': Out(width)})
        self.count = Method()
        self.clear = Method()

    def elaborate(self, plat):
        m = TModule()

        counting = Signal(1)
        clearing = Signal(1)

        with m.If(clearing):
            m.d.sync += [counting.eq(0),
                         clearing.eq(0),
                         self.value.eq(0)]
        with m.Elif(counting):
            m.d.sync += [counting.eq(0),
                         self.value.eq(self.value + 1)]

        @def_method(m, self.count, nonexclusive=True)
        def _():
            m.d.sync += counting.eq(1)

        @def_method(m, self.clear, nonexclusive=True)
        def _():
            m.d.sync += clearing.eq(1)

        return m

class DMAStatus(Struct):
    transfer_count: 8
    running: 1
    underflow: 1
    trig_timeout: 1
    cmd_empty: 1
    cmd_full: 1

class DMACtrl(Struct):
    enabled: 1

class Registers(Elaboratable):
    REG_WIDTH = 32
    TTL_WIDTH = 256
    CLKDIV_WIDTH = 8
    def __init__(self, config):
        self.ttl_hi_mask = Signal(self.TTL_WIDTH)
        self.ttl_lo_mask = Signal(self.TTL_WIDTH)
        self.ttl_out = Signal(self.TTL_WIDTH)
        self.dma_ttl_mask = Signal(self.TTL_WIDTH)

        self.ttl_in = Signal(self.TTL_WIDTH)

        self.timing_status = Signal(self.REG_WIDTH)
        self.timing_ctrl = Signal(self.REG_WIDTH)
        self.clockout_div = Signal(self.CLKDIV_WIDTH, init=255)
        self.loopback = Signal(self.REG_WIDTH)
        self.dds0_reg = Signal(self.REG_WIDTH, reset_less=True)
        self.dds1_reg = Signal(self.REG_WIDTH, reset_less=True)
        self.dma_status = Signal(DMAStatus, init={'cmd_empty': 1})
        self.dma_ctrl = Signal(DMACtrl)

        # Semistatic
        self.dma_ttl_mask.attrs["molecube.vivado.false_path_from"] = "TRUE"
        self.dma_ttl_mask.attrs["molecube.vivado.false_path_to"] = "TRUE"

        def dds_cycle(cycle_2):
            return (cycle_2 >> (1 - config.CLOCK_SHIFT)) - 1

        self.dds_write_adsu = Signal(3, init=dds_cycle(config.DDS_WRITE_ADSU_2)) # Address/Data SetUp cycles - 1
        self.dds_write_wrlow = Signal(3, init=dds_cycle(config.DDS_WRITE_WRLOW_2)) # WRite enable LOW (assert) cycles - 1
        self.dds_write_adhd = Signal(3, init=dds_cycle(config.DDS_WRITE_ADHD_2)) # Address/Data HolD cycles - 1
        self.dds_write_fuddl = Signal(3, init=dds_cycle(config.DDS_WRITE_FUDDL_2)) # FUD DeLay cycles - 1
        self.dds_write_fudhd = Signal(3, init=dds_cycle(config.DDS_WRITE_FUDHD_2)) # FUD HolD cycle - 1


        self.dds_read_asu = Signal(5, init=dds_cycle(config.DDS_READ_ASU_2)) # Address SetUp cycle - 1
        self.dds_read_rdl = Signal(5, init=dds_cycle(config.DDS_READ_RDL_2)) # Read re-init DeLay cycle - 1
        self.dds_read_rdhoz = Signal(5, init=dds_cycle(config.DDS_READ_RDHOZ_2)) # ReaD enable High to Output high-Z cycle - 1

        self.dds_reset_rshd = Signal(5, init=dds_cycle(config.DDS_RESET_RSHD_2)) # ReSet HolD cycle - 1

        for name in ('dds_write_adsu', 'dds_write_wrlow', 'dds_write_adhd',
                     'dds_write_fuddl', 'dds_write_fudhd', 'dds_read_asu',
                     'dds_read_rdl', 'dds_read_rdhoz', 'dds_reset_rshd'):
            r = getattr(self, name)
            r0 = Signal(name=f'{name}_iszero')
            setattr(self, f'{name}_iszero', r0)
            r.attrs["molecube.vivado.false_path_from"] = "TRUE"
            r0.attrs["molecube.vivado.false_path_from"] = "TRUE"

        self.dbg_result_count = Signal(self.REG_WIDTH)

        self.all_counters = dict(
            dbg_inst_word_count=Counter(self.REG_WIDTH),
            dbg_inst_count=Counter(self.REG_WIDTH),
            dbg_ttl_count=Counter(self.REG_WIDTH),
            dbg_dds_count=Counter(self.REG_WIDTH),
            dbg_wait_count=Counter(self.REG_WIDTH),
            dbg_clear_count=Counter(self.REG_WIDTH),
            dbg_loopback_count=Counter(self.REG_WIDTH),
            dbg_clock_count=Counter(self.REG_WIDTH),
            dbg_spi_count=Counter(self.REG_WIDTH),
            dbg_underflow_cycle=Counter(self.REG_WIDTH),
            dbg_inst_cycle=Counter(self.REG_WIDTH),
            # dbg_ttl_cycle=Counter(self.REG_WIDTH),
            # dbg_wait_cycle=Counter(self.REG_WIDTH),
            # dbg_result_overflow_count=Counter(self.REG_WIDTH),
            dbg_result_generated=Counter(self.REG_WIDTH),
            dbg_result_consumed=Counter(self.REG_WIDTH),
        )

        for (k, v) in self.all_counters.items():
            setattr(self, k, v)

    @property
    def dds_timing1(self):
        return Cat(cast_to_width(self.dds_write_adsu, 6),
                   cast_to_width(self.dds_write_wrlow, 6),
                   cast_to_width(self.dds_write_adhd, 6),
                   cast_to_width(self.dds_write_fuddl, 6),
                   cast_to_width(self.dds_write_fudhd, 6))

    @property
    def dds_timing2(self):
        return Cat(cast_to_width(self.dds_read_asu, 6),
                   cast_to_width(self.dds_read_rdl, 6),
                   cast_to_width(self.dds_read_rdhoz, 6),
                   cast_to_width(self.dds_reset_rshd, 6))

    def elaborate(self, m):
        m = TModule()

        for (k, v) in self.all_counters.items():
            setattr(m.submodules, k, v)

        for name in ('dds_write_adsu', 'dds_write_wrlow', 'dds_write_adhd',
                     'dds_write_fuddl', 'dds_write_fudhd', 'dds_read_asu',
                     'dds_read_rdl', 'dds_read_rdhoz', 'dds_reset_rshd'):
            r = getattr(self, name)
            r0 = getattr(self, f'{name}_iszero')
            m.d.sync += r0.eq(r == 0)

        return m
