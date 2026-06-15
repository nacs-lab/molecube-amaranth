#

from amaranth import *
from amaranth.lib import io

from transactron import TModule
from transactron.testing import TestCaseWithSimulator, SimpleTestCircuit

from molecube_amaranth.fifo import CommandFifo, ResultFifo, BufferedFifo

import pytest
import random

class TestFifos(TestCaseWithSimulator):
    def test_command(self):
        fifo = CommandFifo(32, 1024)
        circ = SimpleTestCircuit(fifo)

        async def f(sim):
            for _ in range(100):
                data1 = random.randint(0, 0xffff_ffff)
                data2 = random.randint(0, 0xffff_ffff)
                await circ.write.call(sim, data=data1)
                await circ.write.call(sim, data=data2)
                data_out = (await circ.read.call(sim)).data
                assert data_out == data1 | (data2 << 32)

            data_ins = []
            for _ in range(1024):
                data1 = random.randint(0, 0xffff_ffff)
                data2 = random.randint(0, 0xffff_ffff)
                await circ.write.call(sim, data=data1)
                await circ.write.call(sim, data=data2)
                data_ins.append(data1 | (data2 << 32))

            for i in range(1024):
                data_out = (await circ.read.call(sim)).data
                assert data_out == data_ins[i]

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("depth", range(127, 133))
    def test_result(self, depth):
        fifo = ResultFifo(32, depth)
        circ = SimpleTestCircuit(fifo)

        async def f(sim):
            for _ in range(100):
                data = random.randint(0, 0xffff_ffff)
                await circ.write.call(sim, data=data)
                await sim.tick()
                await sim.tick()
                data_out = (await circ.read.call(sim)).data
                assert data_out == data

            data_ins = []
            for i in range(fifo.depth):
                data = random.randint(0, 0xffff_ffff)
                await circ.write.call(sim, data=data)
                data_ins.append(data)

            for i in range(fifo.depth):
                data_out = (await circ.read.call(sim)).data
                assert data_out == data_ins[i]

            for i in range(128):
                data_out = (await circ.read.call(sim)).data
                assert data_out == 0

            data_ins = []
            for i in range(fifo.depth):
                data = random.randint(0, 0xffff_ffff)
                await circ.write.call(sim, data=data)
                data_ins.append(data)

            for i in range(1024):
                data = random.randint(0, 0xffff_ffff)
                await circ.write.call(sim, data=data)
                data_ins.append(0)

            for i in range(1024 + fifo.depth):
                data_out = (await circ.read.call(sim)).data
                assert data_out == data_ins[i]

            for i in range(fifo.depth):
                data_out = (await circ.read.call(sim)).data
                assert data_out == 0

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    def test_fifo(self):
        fifo = BufferedFifo([('data', 12), ('data2', 3)], 16)
        circ = SimpleTestCircuit(fifo)

        datas = [dict(data=random.randint(0, (1 << 12) - 1),
                      data2=random.randint(0, (1 << 3) - 1)) for _ in range(100)]

        async def producer(sim):
            for data in datas:
                for _ in range(random.randint(0, 2)):
                    await sim.tick()
                await circ.write.call(sim, **data)

        async def consumer(sim):
            for data in datas:
                for _ in range(random.randint(0, 2)):
                    await sim.tick()
                res = await circ.read.call(sim)
                assert res.data == data['data']
                assert res.data2 == data['data2']

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
