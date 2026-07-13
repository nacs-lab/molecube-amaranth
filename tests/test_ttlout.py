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

        self._set_byte_user = Method(i=[('byte', 5), ('value', 8)])

        self.set_byte_user = _TestbenchIO(AdapterTrans.create(self._set_byte_user))
        self.set_bank_inst = _TestbenchIO(AdapterTrans.create(self.ttlout.set_bank_inst))
        self.set_mask = _TestbenchIO(AdapterTrans.create(self.ttlout.set_mask))

    def elaborate(self, _):
        m = TModule()

        m.submodules.pulseio = self.pulseio
        m.submodules.csr = self.csr
        m.submodules.ttlout = self.ttlout

        @def_method(m, self._set_byte_user)
        def _(byte, value):
            with m.Switch(byte[2:]):
                for i in range(8):
                    with m.Case(i):
                        getattr(self.ttlout, f'set_bank_user{i}')(m, byte=byte[:2],
                                                                  hi=value, lo=~value)

        m.submodules.set_bank_inst = self.set_bank_inst
        m.submodules.set_byte_user = self.set_byte_user
        m.submodules.set_mask = self.set_mask

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

    def set_byte(self, byte, value):
        mask = ~(0xff << (byte * 8))
        self._ttl_out_reg = (self._ttl_out_reg & mask) | ((value << (byte * 8)) & ((1 << 56) - 1))

    def set_mask(self, mask, value):
        self._ttl_out_reg = (self._ttl_out_reg & ~mask) | value

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
    def test_set_mask(self, delay):
        circ = TTLOutControllerTester(delay)
        checker = TTLChecker(delay)

        async def f(sim):
            for _ in range(1000):
                mask = random.randint(0, 0xff_ffff_ffff_ffff)
                value = random.randint(0, 0xff_ffff_ffff_ffff) & mask
                await circ.set_mask.call(sim, mask=mask, value=value)
                checker.set_mask(mask, value)
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
                byte = random.randint(0, 7)
                value = random.randint(0, 0xff)
                await circ.set_byte_user.call(sim, byte=byte, value=value)
                checker.set_byte(byte, value)
                for _ in range(6):
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
