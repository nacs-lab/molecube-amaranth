#

from amaranth import *
from amaranth.lib import io

from transactron import TModule, Method, def_method
from transactron.testing import TestCaseWithSimulator, TestbenchIO as _TestbenchIO, SimpleTestCircuit
from transactron.lib.adapters import AdapterTrans

from molecube_amaranth.config import Config
from molecube_amaranth.csr import Registers
from molecube_amaranth.dds import DDSController, DDSReq
from molecube_amaranth.io import get_dds_ports, DDSBuff
from molecube_amaranth.fifo import ResultFifo

from .utils import DDSChecker

import pytest
import random

class DDSControllerTester(Elaboratable):
    def __init__(self, bus_id=0):
        self.bus_id = bus_id
        self.port = port = get_dds_ports(None, 0)
        self._buff = DDSBuff(port)

        self.cache = [[0 for addr in range(2**6)] for dds_id in range(11)]

        config = Config()

        self.csr = Registers(config)

        fifo = ResultFifo(32, 256)
        self.fifo = SimpleTestCircuit(fifo)

        self.controller = DDSController(self._buff, fifo, self.csr, bus_id=bus_id)

        self._set_freq = Method(i=[('id', 4), ('freq', 32)])
        self._set_amp_phase = Method(i=[('id', 4), ('amp', 12), ('phase', 16)])
        self._set_two_bytes = Method(i=[('id', 4), ('addr', 7), ('data', 16)])
        self._set_four_bytes = Method(i=[('id', 4), ('addr', 7), ('data', 32)])
        self._reset = Method(i=[('id', 4)])
        self._get_two_bytes = Method(i=[('id', 4), ('addr', 7)])
        self._get_four_bytes = Method(i=[('id', 4), ('addr', 7)])

        self.set_freq = _TestbenchIO(AdapterTrans.create(self._set_freq))
        self.set_amp_phase = _TestbenchIO(AdapterTrans.create(self._set_amp_phase))
        self.set_two_bytes = _TestbenchIO(AdapterTrans.create(self._set_two_bytes))
        self.set_four_bytes = _TestbenchIO(AdapterTrans.create(self._set_four_bytes))

        self.reset = _TestbenchIO(AdapterTrans.create(self._reset))
        self.get_two_bytes = _TestbenchIO(AdapterTrans.create(self._get_two_bytes))
        self.get_four_bytes = _TestbenchIO(AdapterTrans.create(self._get_four_bytes))

        self.read_dds_cache = _TestbenchIO(AdapterTrans.create(self.controller.read_dds_cache))

    def elaborate(self, _):
        m = TModule()

        m.submodules.buff = self._buff
        m.submodules.csr = self.csr
        m.submodules.fifo = self.fifo
        m.submodules.controller = self.controller

        m.submodules.set_freq = self.set_freq
        m.submodules.set_amp_phase = self.set_amp_phase
        m.submodules.set_two_bytes = self.set_two_bytes
        m.submodules.set_four_bytes = self.set_four_bytes

        m.submodules.reset = self.reset
        m.submodules.get_two_bytes = self.get_two_bytes
        m.submodules.get_four_bytes = self.get_four_bytes

        m.submodules.read_dds_cache = self.read_dds_cache

        dds_req = DDSReq(self.csr)

        @def_method(m, self._set_freq)
        def _(id, freq):
            self.controller.set(m, dds_req.set_freq(m, id=id, freq=freq))

        @def_method(m, self._set_amp_phase)
        def _(id, amp, phase):
            self.controller.set(m, dds_req.set_amp_phase(m, id=id, amp=amp, phase=phase))

        @def_method(m, self._set_two_bytes)
        def _(id, addr, data):
            self.controller.set(m, dds_req.set_two_bytes(m, id=id, addr=addr, data=data))

        @def_method(m, self._set_four_bytes)
        def _(id, addr, data):
            self.controller.set(m, dds_req.set_four_bytes(m, id=id, addr=addr, data=data))

        @def_method(m, self._reset)
        def _(id):
            self.controller.set(m, dds_req.reset(m, id=id))

        @def_method(m, self._get_two_bytes)
        def _(id, addr):
            self.controller.set(m, dds_req.get_two_bytes(m, id=id, addr=addr))

        @def_method(m, self._get_four_bytes)
        def _(id, addr):
            self.controller.set(m, dds_req.get_four_bytes(m, id=id, addr=addr))

        return m

    def set_cache(self, id, addr, val):
        self.cache[id][addr] = val

    def get_cache(self, id, addr):
        return self.cache[id][addr]

    async def check_write1(self, sim, id, addr1, data1):
        self.set_cache(id, addr1 >> 1, data1)
        await DDSChecker.set1(sim, self.csr, self.port, id=id, addr1=addr1, data1=data1)
        await DDSChecker.idle(sim, self.port)

    async def check_write2(self, sim, id, addr1, data1, addr2, data2):
        self.set_cache(id, addr1 >> 1, data1)
        self.set_cache(id, addr2 >> 1, data2)
        await DDSChecker.set2(sim, self.csr, self.port, id=id, addr1=addr1, data1=data1,
                              addr2=addr2, data2=data2)
        await DDSChecker.idle(sim, self.port)

    async def check_read1(self, sim, id, addr, data):
        self.set_cache(id, addr >> 1, data)
        await DDSChecker.get1(sim, self.csr, self.port, id=id, addr=addr, data=data)
        await DDSChecker.idle(sim, self.port)

    async def check_read2(self, sim, id, addr, data):
        self.set_cache(id, addr >> 1, data & 0xffff)
        self.set_cache(id, (addr >> 1) + 1, data >> 16)
        await DDSChecker.get2(sim, self.csr, self.port, id=id, addr=addr, data=data)
        await DDSChecker.idle(sim, self.port)

    async def read_cache(self, sim, id, addr):
        await self.read_dds_cache.call(sim, id=id, addr=addr)
        for _ in range(5):
            await sim.tick()
        return sim.get((self.csr.dds0_reg, self.csr.dds1_reg)[self.bus_id])

    async def check_cache(self, sim, targets):
        for id, addr in targets:
            v = await self.read_cache(sim, id, addr)
            assert self.get_cache(id, addr) == v

class TestDDS(TestCaseWithSimulator):
    def test_idle(self):
        circ = DDSControllerTester()

        async def f(sim):
            await DDSChecker.idle(sim, circ.port, 100)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("adsu", [0, 5])
    @pytest.mark.parametrize("wrlow", [0, 5])
    @pytest.mark.parametrize("adhd", [0, 5])
    @pytest.mark.parametrize("fuddl", [0, 5])
    @pytest.mark.parametrize("fudhd", [0, 5])
    def test_set_freq(self, adsu, wrlow, adhd, fuddl, fudhd):
        circ = DDSControllerTester()

        async def f(sim):
            sim.set(circ.csr.dds_write_adsu, adsu)
            sim.set(circ.csr.dds_write_wrlow, wrlow)
            sim.set(circ.csr.dds_write_adhd, adhd)
            sim.set(circ.csr.dds_write_fuddl, fuddl)
            sim.set(circ.csr.dds_write_fudhd, fudhd)
            for _ in range(100):
                id = random.randint(0, 10)
                freq = random.randint(0, 0xffff_ffff)

                await circ.set_freq.call(sim, id=id, freq=freq)

                await circ.check_write2(sim, id, 0x2d, freq & 0xffff, 0x2f, freq >> 16)
            await circ.check_cache(sim, ((id, addr) for id in range(11) for addr in (0x2d >> 1, 0x2f >> 1)))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("adsu", [0, 5])
    @pytest.mark.parametrize("wrlow", [0, 5])
    @pytest.mark.parametrize("adhd", [0, 5])
    @pytest.mark.parametrize("fuddl", [0, 5])
    @pytest.mark.parametrize("fudhd", [0, 5])
    def test_set_amp_phase(self, adsu, wrlow, adhd, fuddl, fudhd):
        circ = DDSControllerTester()

        async def f(sim):
            sim.set(circ.csr.dds_write_adsu, adsu)
            sim.set(circ.csr.dds_write_wrlow, wrlow)
            sim.set(circ.csr.dds_write_adhd, adhd)
            sim.set(circ.csr.dds_write_fuddl, fuddl)
            sim.set(circ.csr.dds_write_fudhd, fudhd)
            for _ in range(100):
                id = random.randint(0, 10)
                amp = random.randint(0, 0xfff)
                phase = random.randint(0, 0xffff)

                await circ.set_amp_phase.call(sim, id=id, amp=amp, phase=phase)

                await circ.check_write2(sim, id, 0x33, amp, 0x31, phase)
            await circ.check_cache(sim, ((id, addr) for id in range(11) for addr in (0x33 >> 1, 0x31 >> 1)))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("adsu", [0, 5])
    @pytest.mark.parametrize("wrlow", [0, 5])
    @pytest.mark.parametrize("adhd", [0, 5])
    @pytest.mark.parametrize("fuddl", [0, 5])
    @pytest.mark.parametrize("fudhd", [0, 5])
    def test_set_two_bytes(self, adsu, wrlow, adhd, fuddl, fudhd):
        circ = DDSControllerTester()

        async def f(sim):
            sim.set(circ.csr.dds_write_adsu, adsu)
            sim.set(circ.csr.dds_write_wrlow, wrlow)
            sim.set(circ.csr.dds_write_adhd, adhd)
            sim.set(circ.csr.dds_write_fuddl, fuddl)
            sim.set(circ.csr.dds_write_fudhd, fudhd)
            targets = set()
            for _ in range(100):
                id = random.randint(0, 10)
                addr = random.randrange(1, 0x80, 2)
                data = random.randint(0, 0xffff)
                targets.add((id, addr >> 1))

                await circ.set_two_bytes.call(sim, id=id, addr=addr, data=data)

                await circ.check_write1(sim, id, addr, data)
            await circ.check_cache(sim, sorted(targets))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("adsu", [0, 5])
    @pytest.mark.parametrize("wrlow", [0, 5])
    @pytest.mark.parametrize("adhd", [0, 5])
    @pytest.mark.parametrize("fuddl", [0, 5])
    @pytest.mark.parametrize("fudhd", [0, 5])
    def test_set_four_bytes(self, adsu, wrlow, adhd, fuddl, fudhd):
        circ = DDSControllerTester()

        async def f(sim):
            sim.set(circ.csr.dds_write_adsu, adsu)
            sim.set(circ.csr.dds_write_wrlow, wrlow)
            sim.set(circ.csr.dds_write_adhd, adhd)
            sim.set(circ.csr.dds_write_fuddl, fuddl)
            sim.set(circ.csr.dds_write_fudhd, fudhd)
            targets = set()
            for _ in range(100):
                id = random.randint(0, 10)
                addr = random.randrange(1, 0x7e, 4)
                data = random.randint(0, 0xffff_ffff)
                targets.add((id, addr >> 1))
                targets.add((id, (addr >> 1) + 1))

                await circ.set_four_bytes.call(sim, id=id, addr=addr, data=data)

                await circ.check_write2(sim, id, addr, data & 0xffff,
                                        addr + 2, data >> 16)
            await circ.check_cache(sim, sorted(targets))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("rshd", [0, 32])
    def test_reset(self, rshd):
        circ = DDSControllerTester()

        async def f(sim):
            sim.set(circ.csr.dds_reset_rshd, rshd)
            for _ in range(100):
                id = random.randint(0, 10)

                await circ.reset.call(sim, id=id)

                await DDSChecker.reset(sim, circ.csr, circ.port, id=id)
                await DDSChecker.idle(sim, circ.port)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("asu", [0, 1, 5])
    @pytest.mark.parametrize("rdhoz", [0, 1, 5])
    def test_get_two_bytes(self, asu, rdhoz):
        circ = DDSControllerTester()

        async def f(sim):
            sim.set(circ.csr.dds_read_asu, asu)
            sim.set(circ.csr.dds_read_rdhoz, rdhoz)
            targets = set()
            for _ in range(10):
                id = random.randint(0, 10)
                addr = random.randrange(1, 0x80, 2)
                data = random.randint(0, 0xffff)

                dummy_result = random.randint(0, 0xffff_ffff)
                await circ.fifo.write.call(sim, data=dummy_result)

                await circ.get_two_bytes.call(sim, id=id, addr=addr)

                await circ.check_read1(sim, id, addr, data)
                targets.add((id, addr >> 1))

                dummy_result2 = random.randint(0, 0xffff_ffff)
                await circ.fifo.write.call(sim, data=dummy_result2)

                await sim.tick()

                assert (await circ.fifo.read.call(sim)).data == dummy_result
                assert (await circ.fifo.read.call(sim)).data == data
                assert (await circ.fifo.read.call(sim)).data == dummy_result2
            await circ.check_cache(sim, sorted(targets))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("asu", [0, 1, 5])
    @pytest.mark.parametrize("rdl", [0, 1, 5])
    @pytest.mark.parametrize("rdhoz", [0, 1, 5])
    def test_get_four_bytes(self, asu, rdl, rdhoz):
        circ = DDSControllerTester()

        async def f(sim):
            sim.set(circ.csr.dds_read_asu, asu)
            sim.set(circ.csr.dds_read_rdl, rdl)
            sim.set(circ.csr.dds_read_rdhoz, rdhoz)
            targets = set()
            for _ in range(10):
                id = random.randint(0, 10)
                addr = random.randrange(1, 0x7e, 4)
                data = random.randint(0, 0xffff_ffff)

                dummy_result = random.randint(0, 0xffff_ffff)
                await circ.fifo.write.call(sim, data=dummy_result)

                await circ.get_four_bytes.call(sim, id=id, addr=addr)

                await circ.check_read2(sim, id, addr, data)
                targets.add((id, addr >> 1))
                targets.add((id, (addr >> 1) + 1))

                dummy_result2 = random.randint(0, 0xffff_ffff)
                await circ.fifo.write.call(sim, data=dummy_result2)

                await sim.tick()

                assert (await circ.fifo.read.call(sim)).data == dummy_result
                assert (await circ.fifo.read.call(sim)).data == data
                assert (await circ.fifo.read.call(sim)).data == dummy_result2
            await circ.check_cache(sim, sorted(targets))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)
