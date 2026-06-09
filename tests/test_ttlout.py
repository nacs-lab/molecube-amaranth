#

from amaranth import *
from amaranth.lib import io

from transactron import TModule, Method, def_method
from transactron.testing import TestCaseWithSimulator, TestbenchIO as _TestbenchIO
from transactron.lib.adapters import AdapterTrans

from molecube_amaranth.config import Config
from molecube_amaranth.csr import Registers
from molecube_amaranth.ttlout import TTLOutController
from molecube_amaranth.io import PulseIO

from .utils import DDSChecker

import pytest
import random

class TTLOutControllerTester(Elaboratable):
    def __init__(self, delay):
        config = Config()
        self.pulseio = PulseIO.from_config(None, config)
        self.csr = Registers(config)
        self.ttlout = TTLOutController(self.pulseio.ttlout, self.csr, delay=delay)

        self._set_bank_user = Method(i=[('bank', 3), ('value', 32)])

        self.set_bank_user = _TestbenchIO(AdapterTrans.create(self._set_bank_user))
        self.set_bank_inst = _TestbenchIO(AdapterTrans.create(self.ttlout.set_bank_inst))

    def elaborate(self, _):
        m = TModule()

        m.submodules.pulseio = self.pulseio
        m.submodules.csr = self.csr
        m.submodules.ttlout = self.ttlout

        @def_method(m, self._set_bank_user)
        def _(bank, value):
            with m.Switch(bank):
                with m.Case(0):
                    self.ttlout.set_bank_user0(m, value)
                with m.Case(1):
                    self.ttlout.set_bank_user1(m, value)
                with m.Case(2):
                    self.ttlout.set_bank_user2(m, value)
                with m.Case(3):
                    self.ttlout.set_bank_user3(m, value)
                with m.Case(4):
                    self.ttlout.set_bank_user4(m, value)
                with m.Case(5):
                    self.ttlout.set_bank_user5(m, value)
                with m.Case(6):
                    self.ttlout.set_bank_user6(m, value)
                with m.Case(7):
                    self.ttlout.set_bank_user7(m, value)

        m.submodules.set_bank_inst = self.set_bank_inst
        m.submodules.set_bank_user = self.set_bank_user

        return m

class TTLChecker:
    def __init__(self, delay):
        self.delay = delay

        self.ttl_hi_mask = 0
        self.ttl_lo_mask = 0
        self._real_ttl_hi_mask = 0
        self._real_ttl_lo_mask = 0
        self._ttl_out_reg = 0
        self._ttl_out_reg_delay = 0

    @property
    def ttl_out_reg(self):
        if self.delay == 1:
            return self._ttl_out_reg_delay
        return self._ttl_out_reg

    @property
    def ttl_out_io(self):
        return ((self.ttl_out_reg | self._real_ttl_hi_mask) & ~self._real_ttl_lo_mask)

    def tick(self):
        self._real_ttl_hi_mask = self.ttl_hi_mask
        self._real_ttl_lo_mask = self.ttl_lo_mask
        self._ttl_out_reg_delay = self._ttl_out_reg

    def set_bank(self, bank, value):
        mask = ~(0xffff_ffff << (bank * 32))
        self._ttl_out_reg = (self._ttl_out_reg & mask) | ((value << (bank * 32)) & ((1 << 56) - 1))

class TestTTLOut(TestCaseWithSimulator):
    @pytest.mark.parametrize("delay", [0, 1])
    def test_set_inst(self, delay):
        circ = TTLOutControllerTester(delay)
        checker = TTLChecker(delay)

        async def f(sim):
            for _ in range(1000):
                bank = random.randint(0, 1)
                value = random.randint(0, 0xffff_ffff)
                await circ.set_bank_inst.call(sim, bank=bank, value=value)
                checker.set_bank(bank, value)
                assert sim.get(circ.csr.ttl_out) == checker.ttl_out_reg
                assert sim.get(circ.pulseio.ttlout_port.o) == checker.ttl_out_io
                checker.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("delay", [0, 1])
    def test_set_user(self, delay):
        circ = TTLOutControllerTester(delay)
        checker = TTLChecker(delay)

        async def f(sim):
            for _ in range(1000):
                bank = random.randint(0, 1)
                value = random.randint(0, 0xffff_ffff)
                await circ.set_bank_user.call(sim, bank=bank, value=value)
                checker.set_bank(bank, value)
                for _ in range(4):
                    checker.tick()
                    await sim.tick()
                assert sim.get(circ.csr.ttl_out) == checker.ttl_out_reg
                assert sim.get(circ.pulseio.ttlout_port.o) == checker.ttl_out_io

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("delay", [0, 1])
    def test_ttl_ovr(self, delay):
        circ = TTLOutControllerTester(delay)
        checker = TTLChecker(delay)

        async def f(sim):
            for _ in range(1000):
                hi_mask = random.randint(0, 0xff_ffff_ffff_ffff)
                lo_mask = random.randint(0, 0xff_ffff_ffff_ffff)
                sim.set(circ.csr.ttl_hi_mask, hi_mask)
                sim.set(circ.csr.ttl_lo_mask, lo_mask)
                checker.ttl_hi_mask = hi_mask
                checker.ttl_lo_mask = lo_mask
                assert sim.get(circ.csr.ttl_out) == checker.ttl_out_reg
                assert sim.get(circ.pulseio.ttlout_port.o) == checker.ttl_out_io
                await sim.tick()
                checker.tick()
                assert sim.get(circ.csr.ttl_out) == checker.ttl_out_reg
                assert sim.get(circ.pulseio.ttlout_port.o) == checker.ttl_out_io

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)
