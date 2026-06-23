#

from amaranth import *
from amaranth.lib import enum

from transactron import TModule, Transaction, Method, def_method

from .utils import assign_xvalue

class TrigState(enum.Enum):
    IDLE = 0
    INIT = 1
    ARMED = 2
    FIRE = 3

class TriggerController(Elaboratable):
    def __init__(self, ttlin, timer_width):
        self.ttlin = ttlin
        self.timer_width = timer_width
        self.setup = Method(i=[('chn', 8), ('edge', 1), ('cycle', timer_width)])
        self.wait = Method(o=[('timeout', 1)])

    def elaborate(self, plat):
        m = TModule()

        ttlin = Signal.like(self.ttlin.i)
        m.d.sync += ttlin.eq(self.ttlin.i)

        trig_chn = Signal(range(len(ttlin)))
        trig_edge = Signal()
        trig_ttl = Signal()
        m.d.sync += trig_ttl.eq(ttlin.bit_select(trig_chn, 1) ^ trig_edge)

        wait_cycle = Signal(self.timer_width)
        wait_end = Signal()
        m.d.sync += [wait_end.eq(wait_cycle[1:] == 0),
                     wait_cycle.eq(wait_cycle - 1)]

        state = Signal(TrigState)
        trig_starting = Signal()
        m.d.sync += trig_starting.eq(0)

        timedout = Signal(init=1)
        trig_firing = Signal()
        assign_xvalue(m, timedout)
        m.d.sync += trig_firing.eq(0)

        with m.Switch(state):
            with m.Case(TrigState.IDLE):
                with m.If(trig_starting):
                    m.d.sync += state.eq(TrigState.INIT)
            with m.Case(TrigState.INIT):
                with m.If(wait_end):
                    m.d.sync += [state.eq(TrigState.FIRE),
                                 trig_firing.eq(1),
                                 timedout.eq(1)]
                with m.Elif(trig_ttl):
                    m.d.sync += state.eq(TrigState.ARMED)
            with m.Case(TrigState.ARMED):
                with m.If(wait_end | ~trig_ttl):
                    m.d.sync += [state.eq(TrigState.FIRE),
                                 trig_firing.eq(1),
                                 timedout.eq(trig_ttl)]
            with m.Case(TrigState.FIRE):
                m.d.sync += state.eq(TrigState.IDLE)

        @def_method(m, self.setup)
        def _(chn, edge, cycle):
            m.d.sync += [trig_chn.eq(chn),
                         trig_edge.eq(edge),
                         wait_cycle.eq(cycle),
                         trig_starting.eq(1)]

        @def_method(m, self.wait, ready=trig_firing, nonexclusive=True)
        def _():
            return timedout

        return m
