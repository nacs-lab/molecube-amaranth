#

from amaranth import *
from amaranth.lib import io

from transactron import TModule
from transactron.testing import TestCaseWithSimulator, SimpleTestCircuit

from molecube_amaranth.spi import SPIController
from molecube_amaranth.io import SPIBuff
from molecube_amaranth.fifo import ResultFifo

from .utils import SPIChecker

import pytest
from types import SimpleNamespace
import random

def get_spi_test():
    m = TModule()

    port = SimpleNamespace()
    port.miso = io.SimulationPort("i", 1)
    port.mosi = io.SimulationPort("o", 1)
    port.sclk = io.SimulationPort("o", 1)
    port.cs = io.SimulationPort("o", 4)
    m.submodules.buff = buff = SPIBuff(port)

    fifo = ResultFifo(32, 256)
    m.submodules.fifo = fifo_circ = SimpleTestCircuit(fifo)

    controller = SPIController(buff, fifo)
    m.submodules.circ = circ = SimpleTestCircuit(controller)

    return m, circ, port, fifo_circ, controller

class TestSPI(TestCaseWithSimulator):
    def test_empty(self):
        m = TModule()

        fifo = ResultFifo(32, 256)
        m.submodules.fifo = fifo_circ = SimpleTestCircuit(fifo)

        controller = SPIController(None, fifo)
        m.submodules.circ = circ = SimpleTestCircuit(controller)

        async def f(sim):
            for _ in range(5):
                dummy_result = random.randint(0, 0xffff_ffff)
                await fifo_circ.write.call(sim, data=dummy_result)

                data = random.randint(0, 0xffff_ffff)
                id = random.randint(0, 3)
                cs = (1 << id)^0xf
                await circ.set.call(sim, data=data,
                                    div=0, nbits_minus_1=31,
                                    result=1, id=id,
                                    clk_pha=0, clk_pol=0)

                for n in range(65):
                    await sim.tick()
                # Wait two cycles to avoid writing to the result fifo simutaniously...
                await sim.tick()
                await sim.tick()

                dummy_result2 = random.randint(0, 0xffff_ffff)
                await fifo_circ.write.call(sim, data=dummy_result2)

                await sim.tick()

                assert (await fifo_circ.read.call(sim)).data == dummy_result
                assert (await fifo_circ.read.call(sim)).data == 0
                assert (await fifo_circ.read.call(sim)).data == dummy_result2

        with self.run_simulation(m) as sim:
            sim.add_testbench(f)

    def test_idle(self):
        m, circ, port, fifo_circ, controller = get_spi_test()

        async def f(sim):
            await SPIChecker.idle(sim, port, 100, ctrl=controller)

        with self.run_simulation(m) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("div", range(1, 10))
    @pytest.mark.parametrize("nbits", [1, 5, 8, 16, 18, 24, 32])
    @pytest.mark.parametrize("pha", range(2))
    @pytest.mark.parametrize("pol", range(2))
    @pytest.mark.parametrize("save_result", range(2))
    def test_spi(self, div, nbits, pha, pol, save_result):
        m, circ, port, fifo_circ, controller = get_spi_test()

        async def f(sim):
            for _ in range(5):
                dummy_result = random.randint(0, 0xffff_ffff)
                await fifo_circ.write.call(sim, data=dummy_result)

                data = random.randint(0, (1 << nbits) - 1)
                result_data = random.randint(0, (1 << nbits) - 1)
                id = random.randint(0, 3)
                await circ.set.call(sim, data=data << (32 - nbits),
                                    div=div - 1, nbits_minus_1=nbits - 1,
                                    result=save_result, id=id,
                                    clk_pha=pha, clk_pol=pol)
                await SPIChecker.spi(sim, port, id=id, div=div, nbits=nbits,
                                     pha=pha, pol=pol, data=data, result=result_data,
                                     ctrl=controller)
                # Wait two cycles to avoid writing to the result fifo simutaniously...
                await sim.tick()
                await sim.tick()

                dummy_result2 = random.randint(0, 0xffff_ffff)
                await fifo_circ.write.call(sim, data=dummy_result2)

                await sim.tick()

                assert (await fifo_circ.read.call(sim)).data == dummy_result
                if save_result:
                    assert (await fifo_circ.read.call(sim)).data == result_data
                assert (await fifo_circ.read.call(sim)).data == dummy_result2

        with self.run_simulation(m) as sim:
            sim.add_testbench(f)
