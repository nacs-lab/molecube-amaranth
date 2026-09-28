#

from amaranth import *

from transactron.testing import TestCaseWithSimulator, SimpleTestCircuit
from transactron.testing.testbenchio import CallTrigger

from molecube_amaranth.csr import Counter


class TestCounter(TestCaseWithSimulator):
    def test_count(self):
        counter = Counter(32)
        circ = SimpleTestCircuit(counter)

        async def f(sim):
            assert sim.get(counter.value) == 0
            for i in range(1000):
                await circ.count.call(sim)
                assert sim.get(counter.value) == i
                await sim.tick()
                assert sim.get(counter.value) == i + 1

            await circ.clear.call(sim)
            assert sim.get(counter.value) == 1000
            await sim.tick()
            assert sim.get(counter.value) == 0

            for i in range(1000):
                await circ.count.call(sim)
                assert sim.get(counter.value) == i

            await sim.tick()
            assert sim.get(counter.value) == 1000

            await CallTrigger(sim).call(circ.count).call(circ.clear).until_done()
            assert sim.get(counter.value) == 1000
            await sim.tick()
            assert sim.get(counter.value) == 0
            await sim.tick()
            assert sim.get(counter.value) == 0


        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)
