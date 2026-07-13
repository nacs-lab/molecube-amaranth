#

from amaranth import *
from amaranth.lib import io

from transactron import TModule
from transactron.testing import TestCaseWithSimulator, TestbenchIO as _TestbenchIO
from transactron.lib.adapters import AdapterTrans

from molecube_amaranth.csr import Registers
from molecube_amaranth.config import Config
from molecube_amaranth.clockout import ClockOutController

import pytest
import random

class ClockoutTester(Elaboratable):
    def __init__(self, clock_shift=0):
        self.port = io.SimulationPort("o", 1)
        self.csr = Registers(Config(CLOCK_SHIFT=clock_shift))
        self.buff = io.Buffer("o", self.port)
        self.controller = ClockOutController(self.buff, self.csr,
                                             div_width=8 + clock_shift)
        self.set = _TestbenchIO(AdapterTrans.create(self.controller.set))

    def elaborate(self, plat):
        m = TModule()

        m.submodules.csr = self.csr
        m.submodules.buff = self.buff
        m.submodules.controller = self.controller
        m.submodules.set = self.set

        return m

class TestClockOut(TestCaseWithSimulator):
    def test_idle(self):
        circ = ClockoutTester()

        async def f(sim):
            assert sim.get(circ.port.o) == 0
            for _ in range(1000):
                await sim.tick()
                assert sim.get(circ.port.o) == 0
                assert sim.get(circ.csr.clockout_div) == 255

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("half_cycle", range(1, 256))
    def test_clockout(self, half_cycle):
        circ = ClockoutTester()

        async def testclock(sim):
            await circ.set.call(sim, div=half_cycle - 1)

            for _ in range(4):
                for i in range(half_cycle):
                    assert sim.get(circ.port.o) == 0
                    await sim.tick()
                for i in range(half_cycle):
                    assert sim.get(circ.port.o) == 1
                    await sim.tick()

            assert sim.get(circ.csr.clockout_div) == half_cycle - 1

            # Check to make sure that the clock will start from 0 no matter where it was

            assert sim.get(circ.port.o) == 0
            await circ.set.call(sim, div=half_cycle - 1)

            for i in range(half_cycle):
                assert sim.get(circ.port.o) == 0
                await sim.tick()

            assert sim.get(circ.port.o) == 1

            await circ.set.call(sim, div=half_cycle - 1)
            for i in range(half_cycle):
                assert sim.get(circ.port.o) == 0
                await sim.tick()
            for i in range(half_cycle):
                assert sim.get(circ.port.o) == 1
                await sim.tick()

            await circ.set.call(sim, div=255)

            assert sim.get(circ.csr.clockout_div) == 255

            # Make sure the clock can be stopped
            for _ in range(1000):
                assert sim.get(circ.port.o) == 0
                await sim.tick()

        with self.run_simulation(circ, max_cycles=1000_000) as sim:
            sim.add_testbench(testclock)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_clockout_csr(self, clock_shift):
        circ = ClockoutTester(clock_shift)
        max_div = 1 << (clock_shift + 8) - 1

        async def f(sim):
            for _ in range(100):
                div = random.randint(1, max_div)
                await circ.set.call(sim, div=div - 1)
                assert sim.get(circ.csr.clockout_div) == (div - 1) >> clock_shift

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)
