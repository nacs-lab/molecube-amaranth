#

from amaranth import *
from amaranth.lib import io

from transactron import TModule
from transactron.testing import TestCaseWithSimulator, TestbenchIO as _TestbenchIO
from transactron.lib.adapters import AdapterTrans

from molecube_amaranth.csr import Registers
from molecube_amaranth.config import Config
from molecube_amaranth.io import PulseIO, sma_pin
from molecube_amaranth.fifo import Fifos
from molecube_amaranth.inst_runner import InstRunner
from molecube_amaranth.controllers import IOController

from .utils import DDSChecker, SPIChecker, InstBuilder

import pytest
import random

def config(*, spi=False, clock_shift=1):
    if spi:
        kws = dict(SPI_MOSI=sma_pin(1, 1),
                   SPI_MISO=sma_pin(1, 2),
                   SPI_SCLK=sma_pin(1, 3),
                   SPI_CS=sma_pin(1, 4))
    else:
        kws = dict()
    return Config(TTLIN=sma_pin(0, 0), **kws, CLOCK_SHIFT=clock_shift)

class InstRunnerTester(Elaboratable):
    def __init__(self, conf):
        self.pulseio = PulseIO.from_config(None, conf)
        self.csr = Registers(conf)
        self.fifos = Fifos(32)
        self.clock_shift = conf.CLOCK_SHIFT

        self._write_cmd = _TestbenchIO(AdapterTrans.create(self.fifos.cmd_fifo.write))
        self.read_result = _TestbenchIO(AdapterTrans.create(self.fifos.result_fifo.read))

        self._ttl = 0
        self._ttl_hi = 0
        self._ttl_lo = 0

        self._new_clockout = False
        self._clockout_div = 255

        self._dds_cmd = None

        self._spi_cmd = None

    def elaborate(self, _):
        m = TModule()

        m.submodules.pulseio = self.pulseio
        m.submodules.csr = self.csr
        m.submodules.fifos = self.fifos
        m.submodules.ioctrl = ioctrl = IOController(self.pulseio, self.csr, self.fifos,
                                                    clock_shift=self.clock_shift)
        m.submodules.inst_runner = inst_runner = InstRunner(self.pulseio,
                                                            self.csr, self.fifos, ioctrl,
                                                            clock_shift=self.clock_shift)
        m.submodules._write_cmd = self._write_cmd
        m.submodules.read_result = self.read_result

        return m

    def add_testbenches(self, sim):
        sim.add_testbench(self.check_ttl, background=True)
        sim.add_testbench(self.check_clockout, background=True)
        async def check_dds0(sim):
            await self.check_dds(sim, 0)
        async def check_dds1(sim):
            await self.check_dds(sim, 1)
        sim.add_testbench(check_dds0, background=True)
        sim.add_testbench(check_dds1, background=True)
        sim.add_testbench(self.check_spi, background=True)

    async def write_cmd(self, sim, v1, v2):
        await self._write_cmd.call(sim, data=v1)
        await self._write_cmd.call(sim, data=v2)

    def ttl_set(self, ttl):
        self._ttl = ttl

    def ttl_set_ovr(self, lo, hi):
        self._ttl_lo = lo
        self._ttl_hi = hi

    async def check_ttl(self, sim):
        ttlout_port = self.pulseio.ttlout_port
        ttlout_reg = self.csr.ttl_out
        while True:
            # Make sure we see the command added by user coroutine
            await sim.delay(0)
            assert sim.get(ttlout_reg) == self._ttl
            assert sim.get(ttlout_port.o) == (self._ttl | self._ttl_hi) & ~self._ttl_lo
            await sim.tick()

    def clockout_set(self, div):
        self._new_clockout = True
        self._clockout_div = div

    async def _check_clockout_cycle(self, sim, clockout_port):
        self._new_clockout = False
        for _ in range((self._clockout_div + 1) << self.clock_shift):
            # Make sure we see the command added by user coroutine
            await sim.delay(0)
            if self._new_clockout:
                return
            assert sim.get(clockout_port.o) == 0
            await sim.tick()
        for _ in range((self._clockout_div + 1) << self.clock_shift):
            # Make sure we see the command added by user coroutine
            await sim.delay(0)
            if self._new_clockout:
                  return
            assert sim.get(clockout_port.o) == 1
            await sim.tick()

    async def check_clockout(self, sim):
        clockout_port = self.pulseio.clockout_port
        while True:
            # Make sure we see the command added by user coroutine
            await sim.delay(0)
            assert sim.get(self.csr.clockout_div) == self._clockout_div
            if self._clockout_div == 255:
                assert sim.get(clockout_port.o) == 0
                await sim.tick()
            else:
                await self._check_clockout_cycle(sim, clockout_port)

    def dds_set_freq(self, id, freq):
        self._dds_cmd = dict(cmd='set2', id=id, addr1=0x2d, data1=freq & 0xffff,
                             addr2=0x2f, data2=freq >> 16)

    def dds_set_amp_phase(self, id, amp, phase):
        self._dds_cmd = dict(cmd='set2', id=id, addr1=0x33, data1=amp,
                             addr2=0x31, data2=phase)

    def dds_set_two_bytes(self, id, addr, data):
        self._dds_cmd = dict(cmd='set1', id=id, addr1=addr + 1, data1=data)

    def dds_set_four_bytes(self, id, addr, data):
        self._dds_cmd = dict(cmd='set2', id=id, addr1=addr + 1, data1=data & 0xffff,
                             addr2=addr + 3, data2=data >> 16)

    def dds_reset(self, id):
        self._dds_cmd = dict(cmd='reset', id=id)

    def dds_get_two_bytes(self, id, addr, data):
        self._dds_cmd = dict(cmd='get1', id=id, addr=addr + 1, data=data)

    def dds_get_four_bytes(self, id, addr, data):
        self._dds_cmd = dict(cmd='get2', id=id, addr=addr + 1, data=data)

    def _get_dds_cmd(self, bank):
        if self._dds_cmd is None:
            return
        cmd = self._dds_cmd
        id = cmd['id']
        if bank == 0 and id < 11:
            self._dds_cmd = None
            return cmd
        if bank == 1 and id >= 11:
            self._dds_cmd = None
            cmd['id'] = id - 11
            return cmd

    async def _check_dds_cmd(self, sim, bank, port):
        # Make sure we see the command added by user coroutine
        await sim.delay(0)
        cmd = self._get_dds_cmd(bank)
        if cmd is None:
            await DDSChecker.idle(sim, port, 1)
            return
        op = cmd.pop('cmd')
        if op == 'set1':
            await DDSChecker.set1(sim, self.csr, port, **cmd)
        elif op == 'set2':
            await DDSChecker.set2(sim, self.csr, port, **cmd)
        elif op == 'reset':
            await DDSChecker.reset(sim, self.csr, port, **cmd)
        elif op == 'get1':
            await DDSChecker.get1(sim, self.csr, port, **cmd)
        elif op == 'get2':
            await DDSChecker.get2(sim, self.csr, port, **cmd)
        else:
            raise ValueError(f"Unknown DDS command {op}")

    async def check_dds(self, sim, bank):
        port = self.pulseio.dds0_port if bank == 0 else self.pulseio.dds1_port
        while True:
            await self._check_dds_cmd(sim, bank, port)

    def spi_set(self, *, id, div, nbits, pha, pol, data, result):
        self._spi_cmd = dict(id=id, div=div, nbits=nbits, pha=pha, pol=pol,
                             data=data, result=result)

    async def check_spi(self, sim):
        port = self.pulseio.spi_port
        if port is None:
            return
        while True:
            # Make sure we see the command added by user coroutine
            await sim.delay(0)
            cmd = self._spi_cmd
            self._spi_cmd = None
            if cmd is None:
                await SPIChecker.idle(sim, port, 1)
            else:
                await SPIChecker.spi(sim, port, **cmd)
                await sim.tick()

FIFO_LATENCY = 8
RELEASE_LATENCY = 3

class TestInstRunner(TestCaseWithSimulator):
    @pytest.mark.parametrize("spi", [False, True])
    def test_idle(self, spi):
        circ = InstRunnerTester(config(spi=spi))
        if not spi:
            assert circ.pulseio.spi is None
            assert circ.pulseio.spi_port is None
        else:
            assert circ.pulseio.spi is not None
            assert circ.pulseio.spi_port is not None

        async def f(sim):
            # Test idle state
            for _ in range(100):
                await sim.tick()

            assert sim.get(circ.csr.dbg_inst_count.value) == 0
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == 0
            assert sim.get(circ.csr.dbg_result_generated.value) == 0
            assert sim.get(circ.csr.dbg_result_consumed.value) == 0

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_ttl(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = random.randint(10, 100)
        ttl2 = random.randint(0, 0xff_ffff)
        t2 = random.randint(10, 100)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0))
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl2, t=t2, bank=1))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.ttl_set(ttl1 | (ttl2 << 32))
            for _ in range(t2 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 2
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t1 + t2) << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_ttl_ovr(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = random.randint(10, 100)
        ttl_lo = random.randint(0, 0xffff_ffff)
        ttl_hi = random.randint(0, 0xffff_ffff)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            sim.set(circ.csr.ttl_lo_mask, ttl_lo)
            sim.set(circ.csr.ttl_hi_mask, ttl_hi)
            await sim.tick()
            circ.ttl_set_ovr(ttl_lo, ttl_hi)
            assert sim.get(circ.csr.timing_status) == 0x4
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 1
            assert sim.get(circ.csr.dbg_ttl_count.value) == 1
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == (t1 << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_short_ttl(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = random.randint(10, 100)
        ttl2 = random.randint(0, 0xffff_ffff)
        t2 = 1
        ttl3 = random.randint(0, 0xffff_ffff)
        t3 = random.randint(10, 100)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0))
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl2, t=t2, bank=0))
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl3, t=t3, bank=0))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.ttl_set(ttl2)
            for _ in range(t2 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.ttl_set(ttl3)
            for _ in range(t3 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 3
            assert sim.get(circ.csr.dbg_ttl_count.value) == 3
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t1 + t2 + t3) << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_short_ttl2(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = random.randint(10, 100)
        ttl2 = random.randint(0, 0xffff_ffff)
        ttl3 = random.randint(0, 0xffff_ffff)
        t3 = random.randint(10, 100)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0))
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl2, t=0, bank=0))
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl3, t=t3, bank=0))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.ttl_set(ttl2)
            for _ in range(1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.ttl_set(ttl3)
            for _ in range(t3 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 3
            assert sim.get(circ.csr.dbg_ttl_count.value) == 3
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t1 + 1 + t3) << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_wait(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        t1 = random.randint(1000, 2000)
        t2 = random.randint(1000, 2000)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.wait(t=t1))
            await circ.write_cmd(sim, *InstBuilder.wait(t=t2))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            for _ in range((t1 + t2) << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 2
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t1 + t2) << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_clockout(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        div1 = random.randint(0, 254)
        t1 = random.randint(1000, 2000)
        div2 = random.randint(0, 254)
        t2 = random.randint(1000, 2000)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.clockout(div=div1))
            await circ.write_cmd(sim, *InstBuilder.wait(t=t1))
            await circ.write_cmd(sim, *InstBuilder.clockout(div=div2))
            await circ.write_cmd(sim, *InstBuilder.wait(t=t2))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.clockout_set(div1)
            for _ in range((t1 + 5) << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.clockout_set(div2)
            for _ in range((t2 + 5) << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 4
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 2
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 2
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t1 + t2 + 10) << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_clockout_off(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        div1 = random.randint(0, 254)
        t1 = random.randint(1000, 2000)
        t2 = random.randint(1000, 2000)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.clockout(div=div1))
            await circ.write_cmd(sim, *InstBuilder.wait(t=t1))
            await circ.write_cmd(sim, *InstBuilder.clockout(div=255))
            await circ.write_cmd(sim, *InstBuilder.wait(t=t2))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.clockout_set(div1)
            for _ in range((t1 + 5) << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.clockout_set(255)
            for _ in range((t2 + 5) << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 4
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 2
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 2
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t1 + t2 + 10) << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_clockout_init(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        div1 = random.randint(0, 254)
        t1 = random.randint(1000, 2000)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.clockout(div=div1))
            await circ.write_cmd(sim, *InstBuilder.wait(t=t1 + 200))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.clockout_set(div1)
            for _ in range((t1 + 5) << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            sim.set(circ.csr.timing_ctrl, 1 << 8)
            await sim.tick()
            circ.clockout_set(255)
            assert sim.get(circ.csr.timing_status) == 0x0
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 0
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == 0

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_loopback(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        data1 = random.randint(0, 0xffff_ffff)
        data2 = random.randint(0, 0xffff_ffff)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.loopback(data=data1))
            await circ.write_cmd(sim, *InstBuilder.loopback(data=data2))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) & 7 == 0x4
            for _ in range((5 * 2) << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) & 7 == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) & 7 == 0x4

            assert sim.get(circ.csr.timing_status) >> 4 == 2
            assert (await circ.read_result.call(sim)).data == data1
            assert sim.get(circ.csr.timing_status) >> 4 == 2
            # two cycles delay before the new number of result shows up
            await sim.tick()
            await sim.tick()
            assert sim.get(circ.csr.timing_status) >> 4 == 1
            assert (await circ.read_result.call(sim)).data == data2
            assert sim.get(circ.csr.timing_status) >> 4 == 1
            # two cycles delay before the new number of result shows up
            await sim.tick()
            await sim.tick()
            assert sim.get(circ.csr.timing_status) >> 4 == 0

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 2
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == (10 << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_timecheck_succeed(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = 2
        ttl2 = random.randint(0, 0xffff_ffff)
        t2 = random.randint(10, 100)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0,
                                                       timecheck=True))
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl2, t=t2, bank=0))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.ttl_set(ttl2)
            for _ in range(t2 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 2
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t1 + t2) << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_timecheck_fail(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = 1
        ttl2 = random.randint(0, 0xffff_ffff)
        t2 = random.randint(10, 100)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0,
                                                       timecheck=True))
            await sim.tick()
            await sim.tick()
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl2, t=t2, bank=0))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(4 - (1 << circ.clock_shift)):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x5
            circ.ttl_set(ttl2)
            for _ in range(t2 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x1
            for _ in range(10):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x5

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 2
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 4 - (1 << circ.clock_shift)
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t1 + t2) << circ.clock_shift)

            # Set init, which should clear the underflow flag
            sim.set(circ.csr.timing_ctrl, 1 << 8)
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x5
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x5
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x4
            sim.set(circ.csr.timing_ctrl, 0)
            for _ in range(10):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_clear_underflow(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = 1

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0,
                                                       timecheck=True))
            await sim.tick()
            await sim.tick()
            await circ.write_cmd(sim, *InstBuilder.clear_error())

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(4 - (1 << circ.clock_shift)):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x5
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x1
            for _ in range((5 << circ.clock_shift) - 1):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(10):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 1
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 1
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t1 + 5) << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_trigger_timeout1(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        t0 = random.randint(20, 100)
        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = random.randint(20, 100)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.wait(t=t0, trig_chn=0))
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0))
            await circ.write_cmd(sim, *InstBuilder.clear_error())

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            for _ in range((t0 << circ.clock_shift) - 1):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            await sim.tick()
            # The trigger timeout flag is set one cycle earlier
            # than underflow/completion flags.
            assert sim.get(circ.csr.timing_status) == 0x2
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x2
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x2
            for _ in range((5 << circ.clock_shift) - 1):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 3
            assert sim.get(circ.csr.dbg_ttl_count.value) == 1
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 1
            assert sim.get(circ.csr.dbg_clear_count.value) == 1
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t0 + t1 + 5) << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_trigger_timeout2(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        t0 = random.randint(20, 100)
        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = random.randint(20, 100)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.wait(t=t0, trig_chn=0))
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            for _ in range((t0 << circ.clock_shift) - 1):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            await sim.tick()
            # The trigger timeout flag is set one cycle earlier
            # than underflow/completion flags.
            assert sim.get(circ.csr.timing_status) == 0x2
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x2
            for _ in range(10):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x6

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 1
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 1
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t0 + t1) << circ.clock_shift)

            # Set init, which should clear the timeout flag
            sim.set(circ.csr.timing_ctrl, 1 << 8)
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x6
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x6
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x4
            sim.set(circ.csr.timing_ctrl, 0)
            for _ in range(10):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("trig_raise", [False, True])
    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_trigger1(self, clock_shift, trig_raise):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        t0 = random.randint(20, 100)
        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = random.randint(20, 100)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.wait(t=t0, trig_chn=0,
                                                        trig_raise=trig_raise))
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0))

        async def consumer(sim):
            sim.set(circ.pulseio.ttlin_port.i, trig_raise)
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            for _ in range(6 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            sim.set(circ.pulseio.ttlin_port.i, not trig_raise)
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x0
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x0
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x0
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(10):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 1
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 1
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((6 + t1) << circ.clock_shift) + 3

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("trig_raise", [False, True])
    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_trigger2(self, clock_shift, trig_raise):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        t0 = random.randint(20, 100)
        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = random.randint(20, 100)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.wait(t=t0, trig_chn=0,
                                                        trig_raise=trig_raise))
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0))

        async def consumer(sim):
            sim.set(circ.pulseio.ttlin_port.i, not trig_raise)
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            for _ in range(6 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            sim.set(circ.pulseio.ttlin_port.i, trig_raise)
            for _ in range(6 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            sim.set(circ.pulseio.ttlin_port.i, not trig_raise)
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x0
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x0
            await sim.tick()
            assert sim.get(circ.csr.timing_status) == 0x0
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(10):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 1
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 1
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((12 + t1) << circ.clock_shift) + 3

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_hold(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        ttl1 = random.randint(0, 0xffff_ffff)
        t1 = random.randint(10, 100)
        ttl2 = random.randint(0, 0xff_ffff)
        t2 = random.randint(10, 100)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl1, t=t1, bank=0))
            await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl2, t=t2, bank=1))

        async def consumer(sim):
            # Set hold
            sim.set(circ.csr.timing_ctrl, 1 << 7)
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            # Release hold
            sim.set(circ.csr.timing_ctrl, 0)
            for _ in range(RELEASE_LATENCY):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.ttl_set(ttl1)
            for _ in range(t1 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.ttl_set(ttl1 | (ttl2 << 32))
            for _ in range(t2 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 2
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == ((t1 + t2) << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_force_release(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))
        fifo_depth = circ.fifos.cmd_fifo.depth + 2

        ttls = [random.randint(0, 0xffff_ffff) for _ in range(fifo_depth * 2)]
        ts = [random.randint(1, 2) for _ in range(fifo_depth * 2)]

        async def producer(sim):
            for (ttl, t) in zip(ttls, ts):
                await circ.write_cmd(sim, *InstBuilder.ttl(ttl=ttl, t=t, bank=0))

        async def consumer(sim):
            # Set hold
            sim.set(circ.csr.timing_ctrl, 1 << 7)
            for _ in range((fifo_depth + 2) * 2): # 2 cycles per write
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            # It takes one extra cycle for force release to trigger
            # but the full signal is actually generated one cycle before the
            # fifo is actually full due to the input buffer on the fifo
            # as well as the width converter so the two cycles cancel out.
            for _ in range(RELEASE_LATENCY):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            for (ttl, t) in zip(ttls, ts):
                circ.ttl_set(ttl)
                for _ in range(t << circ.clock_shift):
                    await sim.tick()
                    assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(20):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            assert sim.get(circ.csr.timing_ctrl) == 1 << 7
            # Release hold
            sim.set(circ.csr.timing_ctrl, 0)
            for _ in range(20):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == fifo_depth * 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == fifo_depth * 2
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == (sum(ts) << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            sim.add_testbench(circ.check_ttl, background=True)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_set_freq(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        id1 = random.randint(0, 10)
        freq1 = random.randint(0, 0xffff_ffff)
        id2 = random.randint(11, 21)
        freq2 = random.randint(0, 0xffff_ffff)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.dds_set_freq(id=id1, freq=freq1))
            await circ.write_cmd(sim, *InstBuilder.dds_set_freq(id=id2, freq=freq2))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.dds_set_freq(id1, freq1)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.dds_set_freq(id2, freq2)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 2
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == (100 << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_set_amp_phase(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        id1 = random.randint(0, 10)
        amp1 = random.randint(0, 0xfff)
        phase1 = random.randint(0, 0xffff)
        id2 = random.randint(11, 21)
        amp2 = random.randint(0, 0xfff)
        phase2 = random.randint(0, 0xffff)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.dds_set_amp_phase(id=id1, amp=amp1,
                                                                     phase=phase1))
            await circ.write_cmd(sim, *InstBuilder.dds_set_amp_phase(id=id2, amp=amp2,
                                                                     phase=phase2))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.dds_set_amp_phase(id1, amp1, phase1)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.dds_set_amp_phase(id2, amp2, phase2)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 2
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == (100 << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_set_two_bytes(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        id1 = random.randint(0, 10)
        addr1 = random.randrange(0, 0x7f, 2)
        data1 = random.randint(0, 0xffff)
        id2 = random.randint(11, 21)
        addr2 = random.randrange(0, 0x7f, 2)
        data2 = random.randint(0, 0xffff)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.dds_set_two_bytes(id=id1, addr=addr1,
                                                                     data=data1))
            await circ.write_cmd(sim, *InstBuilder.dds_set_two_bytes(id=id2, addr=addr2,
                                                                     data=data2))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.dds_set_two_bytes(id1, addr1, data1)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.dds_set_two_bytes(id2, addr2, data2)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 2
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == (100 << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_set_four_bytes(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        id1 = random.randint(0, 10)
        addr1 = random.randrange(0, 0x7d, 4)
        data1 = random.randint(0, 0xffff_ffff)
        id2 = random.randint(11, 21)
        addr2 = random.randrange(0, 0x7d, 4)
        data2 = random.randint(0, 0xffff_ffff)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.dds_set_four_bytes(id=id1, addr=addr1,
                                                                      data=data1))
            await circ.write_cmd(sim, *InstBuilder.dds_set_four_bytes(id=id2, addr=addr2,
                                                                      data=data2))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.dds_set_four_bytes(id1, addr1, data1)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.dds_set_four_bytes(id2, addr2, data2)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 2
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == (100 << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_reset(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        id1 = random.randint(0, 10)
        id2 = random.randint(11, 21)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.dds_reset(id=id1))
            await circ.write_cmd(sim, *InstBuilder.dds_reset(id=id2))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.dds_reset(id1)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            circ.dds_reset(id2)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x0
            for _ in range(100):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 2
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == (100 << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_get_two_bytes(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        id1 = random.randint(0, 10)
        addr1 = random.randrange(0, 0x7f, 2)
        data1 = random.randint(0, 0xffff)
        id2 = random.randint(11, 21)
        addr2 = random.randrange(0, 0x7f, 2)
        data2 = random.randint(0, 0xffff)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.dds_get_two_bytes(id=id1, addr=addr1))
            await circ.write_cmd(sim, *InstBuilder.dds_get_two_bytes(id=id2, addr=addr2))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.dds_get_two_bytes(id1, addr1, data1)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) & 0xf == 0x0
            circ.dds_get_two_bytes(id2, addr2, data2)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) & 0xf == 0x0
            for _ in range(5):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x24

            assert sim.get(circ.csr.timing_status) >> 4 == 2
            assert (await circ.read_result.call(sim)).data == data1
            assert sim.get(circ.csr.timing_status) >> 4 == 2
            # two cycles delay before the new number of result shows up
            await sim.tick()
            await sim.tick()
            assert sim.get(circ.csr.timing_status) >> 4 == 1
            assert (await circ.read_result.call(sim)).data == data2
            assert sim.get(circ.csr.timing_status) >> 4 == 1
            # two cycles delay before the new number of result shows up
            await sim.tick()
            await sim.tick()
            assert sim.get(circ.csr.timing_status) >> 4 == 0

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 2
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == (100 << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_get_four_bytes(self, clock_shift):
        circ = InstRunnerTester(config(clock_shift=clock_shift))

        id1 = random.randint(0, 10)
        addr1 = random.randrange(0, 0x7d, 4)
        data1 = random.randint(0, 0xffff_ffff)
        id2 = random.randint(11, 21)
        addr2 = random.randrange(0, 0x7d, 4)
        data2 = random.randint(0, 0xffff_ffff)

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.dds_get_four_bytes(id=id1, addr=addr1))
            await circ.write_cmd(sim, *InstBuilder.dds_get_four_bytes(id=id2, addr=addr2))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4
            circ.dds_get_four_bytes(id1, addr1, data1)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) & 0xf == 0x0
            circ.dds_get_four_bytes(id2, addr2, data2)
            for _ in range(50 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) & 0xf == 0x0
            for _ in range(5):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x24

            assert sim.get(circ.csr.timing_status) >> 4 == 2
            assert (await circ.read_result.call(sim)).data == data1
            assert sim.get(circ.csr.timing_status) >> 4 == 2
            # two cycles delay before the new number of result shows up
            await sim.tick()
            await sim.tick()
            assert sim.get(circ.csr.timing_status) >> 4 == 1
            assert (await circ.read_result.call(sim)).data == data2
            assert sim.get(circ.csr.timing_status) >> 4 == 1
            # two cycles delay before the new number of result shows up
            await sim.tick()
            await sim.tick()
            assert sim.get(circ.csr.timing_status) >> 4 == 0

            assert sim.get(circ.csr.dbg_inst_count.value) == 2
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 2
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 0
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == (100 << circ.clock_shift)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("spi", [False, True])
    @pytest.mark.parametrize("clock_shift", [0, 1])
    @pytest.mark.parametrize("save_result", range(2))
    def test_spi(self, spi, clock_shift, save_result):
        circ = InstRunnerTester(config(spi=spi, clock_shift=clock_shift))

        id = 0
        nbits = 18

        data1 = random.randint(0, (1 << nbits) - 1)
        result_data1 = random.randint(0, (1 << nbits) - 1)
        div1 = 1
        data2 = random.randint(0, (1 << nbits) - 1)
        result_data2 = random.randint(0, (1 << nbits) - 1)
        div2 = 1
        data3 = random.randint(0, (1 << nbits) - 1)
        result_data3 = random.randint(0, (1 << nbits) - 1)
        div3 = 1
        data4 = random.randint(0, (1 << nbits) - 1)
        result_data4 = random.randint(0, (1 << nbits) - 1)
        div4 = 1

        async def producer(sim):
            await circ.write_cmd(sim, *InstBuilder.spi(id=id, div=div1, pha=0, pol=0,
                                                       data=data1,
                                                       save_result=save_result))
            await circ.write_cmd(sim, *InstBuilder.spi(id=id, div=div2, pha=0, pol=1,
                                                       data=data2,
                                                       save_result=save_result))
            await circ.write_cmd(sim, *InstBuilder.spi(id=id, div=div3, pha=1, pol=0,
                                                       data=data3,
                                                       save_result=save_result))
            await circ.write_cmd(sim, *InstBuilder.spi(id=id, div=div4, pha=1, pol=1,
                                                       data=data4,
                                                       save_result=save_result))

        async def consumer(sim):
            for _ in range(FIFO_LATENCY + 2): # 2 cycles to write the command
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4

            circ.spi_set(id=id, div=div1 << circ.clock_shift, nbits=18, pha=0, pol=0,
                         data=data1, result=result_data1)
            for _ in range(45 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) & 0xf == 0x0

            circ.spi_set(id=id, div=div2 << circ.clock_shift, nbits=18, pha=0, pol=1,
                         data=data2, result=result_data2)
            for _ in range(45 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) & 0xf == 0x0

            circ.spi_set(id=id, div=div3 << circ.clock_shift, nbits=18, pha=1, pol=0,
                         data=data3, result=result_data3)
            for _ in range(45 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) & 0xf == 0x0

            circ.spi_set(id=id, div=div4 << circ.clock_shift, nbits=18, pha=1, pol=1,
                         data=data4, result=result_data4)
            for _ in range(45 << circ.clock_shift):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) & 0xf == 0x0

            nres = 4 if save_result else 0

            for _ in range(5):
                await sim.tick()
                assert sim.get(circ.csr.timing_status) == 0x4 | (nres << 4)

            assert sim.get(circ.csr.dbg_inst_count.value) == 4
            assert sim.get(circ.csr.dbg_ttl_count.value) == 0
            assert sim.get(circ.csr.dbg_dds_count.value) == 0
            assert sim.get(circ.csr.dbg_wait_count.value) == 0
            assert sim.get(circ.csr.dbg_clear_count.value) == 0
            assert sim.get(circ.csr.dbg_loopback_count.value) == 0
            assert sim.get(circ.csr.dbg_clock_count.value) == 0
            assert sim.get(circ.csr.dbg_spi_count.value) == 4
            assert sim.get(circ.csr.dbg_underflow_cycle.value) == 0
            assert sim.get(circ.csr.dbg_inst_cycle.value) == (180 << circ.clock_shift)

            result_datas = [result_data1, result_data2, result_data3, result_data4] if spi else [0, 0, 0, 0]

            if save_result:
                for i in range(4):
                    assert (await circ.read_result.call(sim)).data == result_datas[i]
                    assert sim.get(circ.csr.timing_status) >> 4 == 4 - i
                    # two cycles delay before the new number of result shows up
                    await sim.tick()
                    await sim.tick()
                    assert sim.get(circ.csr.timing_status) >> 4 == 4 - i - 1

            assert sim.get(circ.csr.timing_status) == 0x4

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)
