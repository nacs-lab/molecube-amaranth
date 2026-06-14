#

from amaranth import *
from amaranth.lib import io

from transactron import TModule
from transactron.testing import TestCaseWithSimulator, SimpleTestCircuit

from molecube_amaranth.inst_cutter import InstCutter

import pytest
import random

class TestCutter(TestCaseWithSimulator):
    def test_throughput(self):
        cutter = InstCutter()
        circ = SimpleTestCircuit(cutter)

        write_data = bytearray()
        insts = []

        def add_rand_inst(l):
            data_len = (l + 1) * 16 - 2
            data = random.randint(0, (1 << data_len) - 1)
            inst = (data << 2) | l
            inst_mask = (1 << (data_len + 2)) - 1
            insts.append((inst, inst_mask))
            write_data.extend(inst.to_bytes((l + 1) * 2, 'little'))

        for _ in range(200):
            add_rand_inst(random.randint(0, 2))

        extra_len = len(write_data) % 8
        if extra_len != 0:
            assert extra_len in (2, 4, 6)
            add_rand_inst((6 - extra_len) >> 1)

        nwrites = len(write_data) // 8
        assert len(write_data) == nwrites * 8

        async def producer(sim):
            for i in range(nwrites):
                data = int.from_bytes(write_data[i * 8:i * 8 + 8], 'little')
                await circ.write.call(sim, data=data)

        def test_inst(res):
            inst, mask = insts.pop(0)
            assert inst == res.inst & mask

        async def consumer(sim):
            test_inst(await circ.read.call(sim))
            while insts:
                test_inst(await circ.read.call_try(sim))

            for _ in range(10):
                assert await circ.read.call_try(sim) is None
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)

    def test_rand(self):
        cutter = InstCutter()
        circ = SimpleTestCircuit(cutter)

        write_data = bytearray()
        insts = []

        def add_rand_inst(l):
            data_len = (l + 1) * 16 - 2
            data = random.randint(0, (1 << data_len) - 1)
            inst = (data << 2) | l
            inst_mask = (1 << (data_len + 2)) - 1
            insts.append((inst, inst_mask))
            write_data.extend(inst.to_bytes((l + 1) * 2, 'little'))

        for _ in range(500):
            add_rand_inst(random.randint(0, 2))

        extra_len = len(write_data) % 8
        if extra_len != 0:
            assert extra_len in (2, 4, 6)
            add_rand_inst((6 - extra_len) >> 1)

        nwrites = len(write_data) // 8
        assert len(write_data) == nwrites * 8

        async def producer(sim):
            for i in range(nwrites):
                for _ in range(random.randint(0, 2)):
                    await sim.tick()
                data = int.from_bytes(write_data[i * 8:i * 8 + 8], 'little')
                await circ.write.call(sim, data=data)

        def test_inst(res):
            inst, mask = insts.pop(0)
            assert inst == res.inst & mask

        async def consumer(sim):
            while insts:
                for _ in range(random.randint(0, 2)):
                    await sim.tick()
                test_inst(await circ.read.call(sim))

            for _ in range(10):
                assert await circ.read.call_try(sim) is None
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
