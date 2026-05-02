#

from amaranth import *
from amaranth.lib import io

from transactron import TModule, Method, def_method
from transactron.testing import TestCaseWithSimulator, TestbenchIO as _TestbenchIO, SimpleTestCircuit
from transactron.lib.adapters import AdapterTrans

from molecube_amaranth.config import Config
from molecube_amaranth.csr import Registers
from molecube_amaranth.dds import DDSController
from molecube_amaranth.io import get_dds_ports, DDSBuff
from molecube_amaranth.fifo import ResultFifo

from .utils import DDSChecker

import pytest
import random

class DDSControllerTester(Elaboratable):
    def __init__(self):
        self.port = port = get_dds_ports(None, 0)
        self._buff = DDSBuff(port)

        config = Config()

        self.csr = Registers(config)

        fifo = ResultFifo(32, 256)
        self.fifo = SimpleTestCircuit(fifo)

        self.controller = DDSController(self._buff, fifo, self.csr)

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

        @def_method(m, self._set_freq)
        def _(id, freq):
            self.controller.set(m, self.controller.set_freq(id=id, freq=freq))

        @def_method(m, self._set_amp_phase)
        def _(id, amp, phase):
            self.controller.set(m, self.controller.set_amp_phase(id=id, amp=amp, phase=phase))

        @def_method(m, self._set_two_bytes)
        def _(id, addr, data):
            self.controller.set(m, self.controller.set_two_bytes(id=id, addr=addr, data=data))

        @def_method(m, self._set_four_bytes)
        def _(id, addr, data):
            self.controller.set(m, self.controller.set_four_bytes(id=id, addr=addr, data=data))

        @def_method(m, self._reset)
        def _(id):
            self.controller.set(m, self.controller.reset(id=id))

        @def_method(m, self._get_two_bytes)
        def _(id, addr):
            self.controller.set(m, self.controller.get_two_bytes(id=id, addr=addr))

        @def_method(m, self._get_four_bytes)
        def _(id, addr):
            self.controller.set(m, self.controller.get_four_bytes(id=id, addr=addr))

        return m

async def _check_write_reg1(sim, circ, id, addr1, data1):
    await DDSChecker.set1(sim, circ.csr, circ.port, id=id, addr1=addr1, data1=data1)
    await DDSChecker.idle(sim, circ.port)

async def _check_write_reg2(sim, circ, id, addr1, data1, addr2, data2):
    await DDSChecker.set2(sim, circ.csr, circ.port, id=id, addr1=addr1, data1=data1,
                          addr2=addr2, data2=data2)
    await DDSChecker.idle(sim, circ.port)

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

                await _check_write_reg2(sim, circ, id, 0x2d, freq & 0xffff, 0x2f, freq >> 16)

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

                await _check_write_reg2(sim, circ, id, 0x33, amp, 0x31, phase)

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
            for _ in range(100):
                id = random.randint(0, 10)
                addr = random.randint(0, 0x7f)
                data = random.randint(0, 0xffff)

                await circ.set_two_bytes.call(sim, id=id, addr=addr, data=data)

                await _check_write_reg1(sim, circ, id, addr, data)

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
            for _ in range(100):
                id = random.randint(0, 10)
                addr = random.randint(0, 0x7c)
                data = random.randint(0, 0xffff_ffff)

                await circ.set_four_bytes.call(sim, id=id, addr=addr, data=data)

                await _check_write_reg2(sim, circ, id, addr, data & 0xffff,
                                        addr + 2, data >> 16)

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
            for _ in range(10):
                id = random.randint(0, 10)
                addr = random.randint(0, 0x7f)
                data = random.randint(0, 0xffff)

                dummy_result = random.randint(0, 0xffff_ffff)
                await circ.fifo.write.call(sim, data=dummy_result)

                await circ.get_two_bytes.call(sim, id=id, addr=addr)

                await DDSChecker.get1(sim, circ.csr, circ.port,
                                      id=id, addr=addr, data=data)
                await DDSChecker.idle(sim, circ.port)

                dummy_result2 = random.randint(0, 0xffff_ffff)
                await circ.fifo.write.call(sim, data=dummy_result2)

                await sim.tick()

                assert (await circ.fifo.read.call(sim)).data == dummy_result
                assert (await circ.fifo.read.call(sim)).data == data
                assert (await circ.fifo.read.call(sim)).data == dummy_result2

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
            for _ in range(10):
                id = random.randint(0, 10)
                addr = random.randint(0, 0x7d)
                data = random.randint(0, 0xffff_ffff)

                dummy_result = random.randint(0, 0xffff_ffff)
                await circ.fifo.write.call(sim, data=dummy_result)

                await circ.get_four_bytes.call(sim, id=id, addr=addr)

                await DDSChecker.get2(sim, circ.csr, circ.port,
                                      id=id, addr=addr, data=data)
                await DDSChecker.idle(sim, circ.port)

                dummy_result2 = random.randint(0, 0xffff_ffff)
                await circ.fifo.write.call(sim, data=dummy_result2)

                await sim.tick()

                assert (await circ.fifo.read.call(sim)).data == dummy_result
                assert (await circ.fifo.read.call(sim)).data == data
                assert (await circ.fifo.read.call(sim)).data == dummy_result2

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)
