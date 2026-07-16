#

from amaranth import *

from transactron import TModule
from transactron.testing import TestCaseWithSimulator, TestbenchIO as _TestbenchIO
from transactron.testing.testbenchio import CallTrigger
from transactron.lib.adapters import AdapterTrans

from molecube_amaranth.csr import Registers
from molecube_amaranth.config import Config
from molecube_amaranth.dds import FSMState as DDSFSMState
from molecube_amaranth.io import PulseIO, sma_pin
from molecube_amaranth.fifo import Fifos
from molecube_amaranth.inst_runner import InstRunner, InstDispatcher, InstConsumer
from molecube_amaranth.controllers import IOController

from .utils import TTLChecker, ClockoutChecker, DDSChecker, SPIChecker, InstBuilder, check_fields

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

class InstRunnerTester(Elaboratable, TTLChecker, ClockoutChecker, DDSChecker, SPIChecker):
    def __init__(self, conf):
        self.pulseio = PulseIO.from_config(None, conf)
        self.csr = Registers(conf)
        self.fifos = Fifos(32)
        self.clock_shift = conf.CLOCK_SHIFT
        self.ioctrl = IOController(self.pulseio, self.csr, self.fifos,
                                   clock_shift=self.clock_shift)

        self._write_cmd = _TestbenchIO(AdapterTrans.create(self.fifos.cmd_fifo.write))
        self.read_result = _TestbenchIO(AdapterTrans.create(self.fifos.result_fifo.read))

        TTLChecker.__init__(self, self.pulseio, self.csr)
        ClockoutChecker.__init__(self, self.pulseio, self.csr, self.clock_shift)
        DDSChecker.__init__(self, self.pulseio, self.csr,
                            (self.ioctrl.dds0, self.ioctrl.dds1))
        SPIChecker.__init__(self, self.pulseio, self.csr, self.ioctrl.spi)

    def elaborate(self, _):
        m = TModule()

        m.submodules.pulseio = self.pulseio
        m.submodules.csr = self.csr
        m.submodules.fifos = self.fifos
        m.submodules.ioctrl = self.ioctrl
        m.submodules.inst_runner = inst_runner = InstRunner(self.pulseio, self.csr,
                                                            self.fifos, self.ioctrl,
                                                            clock_shift=self.clock_shift)
        m.submodules._write_cmd = self._write_cmd
        m.submodules.read_result = self.read_result

        return m

    def add_testbenches(self, sim):
        sim.add_testbench(self.check_ttl, background=True)
        sim.add_testbench(self.check_clockout, background=True)
        sim.add_testbench(self.check_dds0, background=True)
        sim.add_testbench(self.check_dds1, background=True)
        sim.add_testbench(self.check_spi, background=True)

    async def write_cmd(self, sim, v1, v2):
        await self._write_cmd.call(sim, data=v1)
        await self._write_cmd.call(sim, data=v2)

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

        div1 = random.randint(0, 20)
        t1 = random.randint(60, 200)
        div2 = random.randint(0, 20)
        t2 = random.randint(60, 200)

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

        div1 = random.randint(0, 20)
        t1 = random.randint(60, 200)
        t2 = random.randint(550, 600)

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

        div1 = random.randint(0, 20)
        t1 = random.randint(60, 500)

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

class InstDispatcherTester(Elaboratable):
    def __init__(self):
        self.conf = Config()
        self.csr = Registers(self.conf)
        self.fifos = Fifos(32)

        self._dds_write_adsu = self.csr.dds_write_adsu.init
        self._dds_reset_rshd = self.csr.dds_reset_rshd.init
        self._dds_read_asu = self.csr.dds_read_asu.init

        self._write_cmd = _TestbenchIO(AdapterTrans.create(self.fifos.cmd2_fifo.write))
        self.read_dds0 = _TestbenchIO(AdapterTrans.create(self.fifos.dds0_cmd_fifo.read))
        self.read_dds1 = _TestbenchIO(AdapterTrans.create(self.fifos.dds1_cmd_fifo.read))
        self.read_spi = _TestbenchIO(AdapterTrans.create(self.fifos.spi_cmd_fifo.read))

        self.dds0_queue = []
        self.dds1_queue = []
        self.spi_queue = []

    def elaborate(self, _):
        m = TModule()

        m.submodules.csr = self.csr
        m.submodules.fifos = self.fifos
        m.submodules.inst_dispatcher = InstDispatcher(self.csr, self.fifos)
        m.submodules._write_cmd = self._write_cmd
        m.submodules.read_dds0 = self.read_dds0
        m.submodules.read_dds1 = self.read_dds1
        m.submodules.read_spi = self.read_spi

        return m

    async def write_cmd(self, sim, v1, v2):
        await self._write_cmd.call(sim, data=v1)
        await self._write_cmd.call(sim, data=v2)

    def _add_dds_queue(self, **data):
        id = data['id']
        if id < 11:
            self.dds0_queue.append(data)
        else:
            data['id'] = id - 11
            self.dds1_queue.append(data)

    def _dds_write1(self, *, id, addr1, data1, fud=1):
        self._add_dds_queue(state=DDSFSMState.WR_ADSETUP2.value,
                            id=id,
                            hold_cnt=self._dds_write_adsu,
                            hold_end=self._dds_write_adsu == 0,
                            read=0, reset=0, fud=fud,
                            addr1=addr1, data1=data1)

    def _dds_write2(self, *, id, addr1, data1, addr2, data2, fud=1):
        self._add_dds_queue(state=DDSFSMState.WR_ADSETUP1.value,
                            id=id,
                            hold_cnt=self._dds_write_adsu,
                            hold_end=self._dds_write_adsu == 0,
                            read=0, reset=0, fud=fud,
                            addr1=addr1, data1=data1, addr2=addr2, data2=data2)

    def rand_dds_set_freq(self):
        id = random.randint(0, 21)
        freq = random.randint(0, 0xffff_ffff)
        timecheck = random.randint(0, 1)
        inst = InstBuilder.dds_set_freq(id=id, freq=freq, timecheck=timecheck)
        self._dds_write2(id=id, addr1=0x2d >> 1, data1=freq & 0xffff,
                         addr2=0x2f >> 1, data2=freq >> 16)
        return inst

    def rand_dds_set_amp_phase(self):
        id = random.randint(0, 21)
        amp = random.randint(0, 0xfff)
        phase = random.randint(0, 0xffff)
        timecheck = random.randint(0, 1)
        inst = InstBuilder.dds_set_amp_phase(id=id, amp=amp, phase=phase,
                                             timecheck=timecheck)
        self._dds_write2(id=id, addr1=0x33 >> 1, data1=amp,
                         addr2=0x31 >> 1, data2=phase)
        return inst

    def rand_dds_set_two_bytes(self):
        id = random.randint(0, 21)
        addr = random.randint(0, 0x3f)
        data = random.randint(0, 0xffff)
        timecheck = random.randint(0, 1)
        inst = InstBuilder.dds_set_two_bytes(id=id, addr=addr << 1, data=data,
                                             timecheck=timecheck)
        self._dds_write1(id=id, addr1=addr, data1=data)
        return inst

    def rand_dds_set_four_bytes(self):
        id = random.randint(0, 21)
        addr = random.randint(0, 0x1f) << 1
        data = random.randint(0, 0xffff_ffff)
        timecheck = random.randint(0, 1)
        inst = InstBuilder.dds_set_four_bytes(id=id, addr=addr << 1, data=data,
                                              timecheck=timecheck)
        addr_2 = addr | 1
        self._dds_write2(id=id, addr1=addr, data1=data & 0xffff,
                         addr2=addr_2, data2=data >> 16)
        return inst

    def rand_dds_reset(self):
        id = random.randint(0, 21)
        timecheck = random.randint(0, 1)
        inst = InstBuilder.dds_reset(id=id, timecheck=timecheck)
        self._add_dds_queue(state=DDSFSMState.RESET.value,
                            id=id, hold_cnt=self._dds_reset_rshd,
                            hold_end=self._dds_reset_rshd == 0,
                            read=0, reset=1,
                            addr1=0, data1=0)
        return inst

    def rand_dds_get_two_bytes(self):
        id = random.randint(0, 21)
        addr = random.randint(0, 0x3f)
        data = random.randint(0, 0xffff)
        timecheck = random.randint(0, 1)
        inst = InstBuilder.dds_get_two_bytes(id=id, addr=addr << 1, timecheck=timecheck)
        self._add_dds_queue(state=DDSFSMState.RD_ASETUP2.value,
                            id=id,
                            hold_cnt=self._dds_read_asu,
                            hold_end=self._dds_read_asu == 0,
                            read=1, reset=0,
                            addr1=addr, data1=0, data2=0)
        return inst

    def rand_dds_get_four_bytes(self):
        id = random.randint(0, 21)
        addr = random.randint(0, 0x1f) << 1
        timecheck = random.randint(0, 1)
        inst = InstBuilder.dds_get_four_bytes(id=id, addr=addr << 1, timecheck=timecheck)
        addr_2 = addr | 1
        self._add_dds_queue(state=DDSFSMState.RD_ASETUP1.value,
                            id=id,
                            hold_cnt=self._dds_read_asu,
                            hold_end=self._dds_read_asu == 0,
                            read=1, reset=0,
                            addr1=addr_2, data1=0,
                            addr2=addr)
        return inst

    def rand_spi(self):
        id = random.randint(0, 3)
        div = random.randint(0, 0xff)
        data = random.randint(0, (1 << 18) - 1)
        pha = random.randint(0, 1)
        pol = random.randint(0, 1)
        save_result = random.randint(0, 1)
        timecheck = random.randint(0, 1)
        inst = InstBuilder.spi(id=id, div=div + 1, pha=pha, pol=pol, data=data,
                               save_result=save_result, timecheck=timecheck)
        self.spi_queue.append(dict(data=data, clk_div=div,
                                   save_result=save_result,
                                   id=id, clk_pha=pha, clk_pol=pol))
        return inst

    def rand_ttl(self):
        return InstBuilder.ttl(ttl=random.randint(0, 0xffff_ffff),
                               t=random.randint(0, 0xff_ffff),
                               bank=random.randint(0, 7),
                               timecheck=random.randint(0, 1))

    def rand_clockout(self):
        return InstBuilder.clockout(div=random.randint(0, 255),
                                    timecheck=random.randint(0, 1))

    def rand_wait(self):
        return InstBuilder.wait(t=random.randint(0, 0xff_ffff),
                                trig_chn=random.randint(-1, 255),
                                trig_raise=random.randint(0, 1),
                                timecheck=random.randint(0, 1))

    def rand_clear_error(self):
        return InstBuilder.clear_error()

    def rand_loopback(self):
        return InstBuilder.loopback(data=random.randint(0, 0xffff_ffff),
                                    timecheck=random.randint(0, 1))

class TestInstDispatcher(TestCaseWithSimulator):
    def test_rand(self):
        circ = InstDispatcherTester()

        insts = []
        for _ in range(300):
            insts.append(random.choice((circ.rand_dds_set_freq,
                                        circ.rand_dds_set_amp_phase,
                                        circ.rand_dds_set_two_bytes,
                                        circ.rand_dds_set_four_bytes,
                                        circ.rand_dds_reset,
                                        circ.rand_dds_get_two_bytes,
                                        circ.rand_dds_get_four_bytes,
                                        circ.rand_spi,
                                        circ.rand_ttl,
                                        circ.rand_clockout,
                                        circ.rand_wait,
                                        circ.rand_clear_error,
                                        circ.rand_loopback))())

        async def producer(sim):
            for inst in insts:
                await circ.write_cmd(sim, *inst)

        async def consume_dds0(sim):
            while circ.dds0_queue:
                check_fields(await circ.read_dds0.call(sim), circ.dds0_queue.pop(0))

        async def consume_dds1(sim):
            while circ.dds1_queue:
                check_fields(await circ.read_dds1.call(sim), circ.dds1_queue.pop(0))

        async def consume_spi(sim):
            while circ.spi_queue:
                check_fields(await circ.read_spi.call(sim), circ.spi_queue.pop(0))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consume_dds0)
            sim.add_testbench(consume_dds1)
            sim.add_testbench(consume_spi)


class InstConsumerTester(Elaboratable, DDSChecker, SPIChecker):
    def __init__(self, conf):
        self.pulseio = PulseIO.from_config(None, conf)
        self.csr = Registers(conf)
        self.fifos = Fifos(32)
        self.clock_shift = conf.CLOCK_SHIFT
        self.ioctrl = IOController(self.pulseio, self.csr, self.fifos,
                                   clock_shift=self.clock_shift)
        self.enable = Signal()

        self._dds_write_adsu = self.csr.dds_write_adsu.init
        self._dds_reset_rshd = self.csr.dds_reset_rshd.init
        self._dds_read_asu = self.csr.dds_read_asu.init

        self.dds_set1_cycle = (self.csr.dds_write_adsu.init + 1 +
                               self.csr.dds_write_wrlow.init + 1 +
                               self.csr.dds_write_fuddl.init + 1 +
                               self.csr.dds_write_fudhd.init + 1)
        self.dds_set2_cycle = ((self.csr.dds_write_adsu.init + 1) * 2 +
                               (self.csr.dds_write_wrlow.init + 1) * 2 +
                               self.csr.dds_write_adhd.init + 1 +
                               self.csr.dds_write_fuddl.init + 1 +
                               self.csr.dds_write_fudhd.init + 1)
        self.dds_reset_cycle = self.csr.dds_reset_rshd.init + 1
        self.dds_get1_cycle = (self.csr.dds_read_asu.init + 1 +
                               self.csr.dds_read_rdhoz.init + 1)
        self.dds_get2_cycle = ((self.csr.dds_read_asu.init + 1) * 2 +
                               self.csr.dds_read_rdl.init + 1 +
                               self.csr.dds_read_rdhoz.init + 1)

        self.write_dds0 = _TestbenchIO(AdapterTrans.create(self.fifos.dds0_cmd_fifo.write))
        self.write_dds1 = _TestbenchIO(AdapterTrans.create(self.fifos.dds1_cmd_fifo.write))
        self.write_spi = _TestbenchIO(AdapterTrans.create(self.fifos.spi_cmd_fifo.write))
        self.read_result = _TestbenchIO(AdapterTrans.create(self.fifos.result_fifo.read))

        self.dds0_queue = []
        self.dds1_queue = []
        self.spi_queue = []

        self.dds0_check_queue = []
        self.dds1_check_queue = []
        self.spi_check_queue = []

        DDSChecker.__init__(self, self.pulseio, self.csr,
                            (self.ioctrl.dds0, self.ioctrl.dds1))
        SPIChecker.__init__(self, self.pulseio, self.csr, self.ioctrl.spi)

    def elaborate(self, _):
        m = TModule()

        m.submodules.pulseio = self.pulseio
        m.submodules.csr = self.csr
        m.submodules.fifos = self.fifos
        m.submodules.ioctrl = self.ioctrl
        m.submodules.inst_consumer = InstConsumer(self.enable, self.ioctrl, self.fifos,
                                                  clock_shift=self.clock_shift)
        m.submodules.write_dds0 = self.write_dds0
        m.submodules.write_dds1 = self.write_dds1
        m.submodules.write_spi = self.write_spi
        m.submodules.read_result = self.read_result

        return m

    def add_testbenches(self, sim):
        sim.add_testbench(self.check_dds0, background=True)
        sim.add_testbench(self.check_dds1, background=True)
        sim.add_testbench(self.check_spi, background=True)

    async def queue_cmd(self, sim):
        trig = CallTrigger(sim)
        if self.dds0_queue:
            trig = trig.call(self.write_dds0, **self.dds0_queue[0])
        if self.dds1_queue:
            trig = trig.call(self.write_dds1, **self.dds1_queue[0])
        if self.spi_queue:
            trig = trig.call(self.write_spi, **self.spi_queue[0])
        res = await trig.until_done()
        if self.dds0_queue:
            dds0_res, *res = res
            if dds0_res is not None:
                self.dds0_queue.pop(0)
        if self.dds1_queue:
            dds1_res, *res = res
            if dds1_res is not None:
                self.dds1_queue.pop(0)
        if self.spi_queue:
            spi_res, *res = res
            if spi_res is not None:
                self.spi_queue.pop(0)
        assert not res
        return bool(self.dds0_queue or self.dds1_queue or self.spi_queue)

    async def queue_all_cmd(self, sim):
        while await self.queue_cmd(sim):
            pass

    def _add_dds_queue(self, **data):
        id = data['id']
        if id < 11:
            self.dds0_queue.append(data)
        else:
            data['id'] = id - 11
            self.dds1_queue.append(data)

    def _add_dds_check_queue(self, **data):
        id = data['id']
        if id < 11:
            self.dds0_check_queue.append(data)
        else:
            self.dds1_check_queue.append(data)

    def _dds_write1(self, *, id, addr1, data1, fud=1):
        self._add_dds_queue(state=DDSFSMState.WR_ADSETUP2.value,
                            id=id,
                            hold_cnt=self._dds_write_adsu,
                            hold_end=self._dds_write_adsu == 0,
                            read=0, reset=0, fud=fud,
                            addr1=addr1, data1=data1)
        self._add_dds_check_queue(id=id, cmd='set1', addr1=addr1 << 1, data1=data1, fud=fud)

    def _dds_write2(self, *, id, addr1, data1, addr2, data2, fud=1):
        self._add_dds_queue(state=DDSFSMState.WR_ADSETUP1.value,
                            id=id,
                            hold_cnt=self._dds_write_adsu,
                            hold_end=self._dds_write_adsu == 0,
                            read=0, reset=0, fud=fud,
                            addr1=addr1, data1=data1, addr2=addr2, data2=data2)
        self._add_dds_check_queue(id=id, cmd='set2', addr1=addr1 << 1, data1=data1,
                                  addr2=addr2 << 1, data2=data2, fud=fud)

    def rand_dds_set_freq(self, *, id=range(22)):
        id = random.choice(id)
        freq = random.randint(0, 0xffff_ffff)
        timecheck = random.randint(0, 1)
        self._dds_write2(id=id, addr1=0x2d >> 1, data1=freq & 0xffff,
                         addr2=0x2f >> 1, data2=freq >> 16)

    def rand_dds_set_amp_phase(self, *, id=range(22)):
        id = random.choice(id)
        amp = random.randint(0, 0xfff)
        phase = random.randint(0, 0xffff)
        self._dds_write2(id=id, addr1=0x33 >> 1, data1=amp,
                         addr2=0x31 >> 1, data2=phase)

    def rand_dds_set_two_bytes(self, *, id=range(22)):
        id = random.choice(id)
        addr = random.randint(0, 0x3f)
        data = random.randint(0, 0xffff)
        self._dds_write1(id=id, addr1=addr, data1=data)

    def rand_dds_set_four_bytes(self, *, id=range(22)):
        id = random.choice(id)
        addr = random.randint(0, 0x1f) << 1
        data = random.randint(0, 0xffff_ffff)
        addr_2 = addr | 1
        self._dds_write2(id=id, addr1=addr, data1=data & 0xffff,
                         addr2=addr_2, data2=data >> 16)

    def rand_dds_reset(self, *, id=range(22)):
        id = random.choice(id)
        self._add_dds_queue(state=DDSFSMState.RESET.value,
                            id=id, hold_cnt=self._dds_reset_rshd,
                            hold_end=self._dds_reset_rshd == 0,
                            read=0, reset=1,
                            addr1=0, data1=0)
        self._add_dds_check_queue(id=id, cmd='reset')

    def rand_dds_get_two_bytes(self, *, id=range(22)):
        id = random.choice(id)
        addr = random.randint(0, 0x3f)
        data = random.randint(0, 0xffff)
        self._add_dds_queue(state=DDSFSMState.RD_ASETUP2.value,
                            id=id,
                            hold_cnt=self._dds_read_asu,
                            hold_end=self._dds_read_asu == 0,
                            read=1, reset=0,
                            addr1=addr, data1=0, data2=0)
        self._add_dds_check_queue(id=id, cmd='get1', addr=addr << 1, data=data)

    def rand_dds_get_four_bytes(self, *, id=range(22)):
        id = random.choice(id)
        addr = random.randint(0, 0x1f) << 1
        addr_2 = addr | 1
        data = random.randint(0, 0xffff_ffff)
        self._add_dds_queue(state=DDSFSMState.RD_ASETUP1.value,
                            id=id,
                            hold_cnt=self._dds_read_asu,
                            hold_end=self._dds_read_asu == 0,
                            read=1, reset=0,
                            addr1=addr_2, data1=0,
                            addr2=addr)
        self._add_dds_check_queue(id=id, cmd='get2', addr=addr << 1, data=data)

    def rand_spi(self, *, div=range(0x100)):
        id = 0
        div = random.choice(div)
        data = random.randint(0, (1 << 18) - 1)
        pha = random.randint(0, 1)
        pol = random.randint(0, 1)
        save_result = random.randint(0, 1)
        self.spi_queue.append(dict(data=data, clk_div=div,
                                   save_result=save_result,
                                   id=id, clk_pha=pha, clk_pol=pol))
        self.spi_check_queue.append(dict(data=data, div=(div + 1) << self.clock_shift,
                                         nbits=18, save_result=save_result,
                                         result=random.randint(0, (1 << 18) - 1),
                                         id=id, pha=pha, pol=pol))


    def _pop_dds_check(self, sim, dds_check_queue):
        dds = dds_check_queue.pop(0)
        dds_type = dds.pop('cmd')
        if dds_type == 'set1':
            self.dds_set1(**dds)
        elif dds_type == 'set2':
            self.dds_set2(**dds)
        elif dds_type == 'reset':
            self.dds_reset(**dds)
        elif dds_type == 'get1':
            self.dds_get_two_bytes(**dds)
        else:
            assert dds_type == 'get2'
            self.dds_get_four_bytes(**dds)
        dds['cmd'] = dds_type
        return dds

    def pop_dds0_check(self, sim):
        return self._pop_dds_check(sim, self.dds0_check_queue)

    def pop_dds1_check(self, sim):
        return self._pop_dds_check(sim, self.dds1_check_queue)

    def pop_spi_check(self, sim):
        spi = self.spi_check_queue.pop(0)
        save_result = spi.pop('save_result')
        self.spi_set(**spi)
        spi['save_result'] = save_result
        return spi

CONSUMER_LATENCY = 5

class TestInstConsumer(TestCaseWithSimulator):
    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_set_freq(self, clock_shift):
        circ = InstConsumerTester(config(clock_shift=clock_shift))

        circ.rand_dds_set_freq(id=range(11))
        circ.rand_dds_set_freq(id=range(11, 22))
        circ.rand_dds_set_freq(id=range(11, 22))
        circ.rand_dds_set_freq(id=range(11, 22))

        async def consumer(sim):
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            sim.set(circ.enable, 0)
            circ.pop_dds0_check(sim)
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set2_cycle):
                await sim.tick()
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set2_cycle + CONSUMER_LATENCY):
                await sim.tick()
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set2_cycle):
                await sim.tick()
            for _ in range(50):
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(circ.queue_all_cmd)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_set_amp_phase(self, clock_shift):
        circ = InstConsumerTester(config(clock_shift=clock_shift))

        circ.rand_dds_set_amp_phase(id=range(11))
        circ.rand_dds_set_amp_phase(id=range(11, 22))
        circ.rand_dds_set_amp_phase(id=range(11, 22))
        circ.rand_dds_set_amp_phase(id=range(11, 22))

        async def consumer(sim):
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            sim.set(circ.enable, 0)
            circ.pop_dds0_check(sim)
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set2_cycle):
                await sim.tick()
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set2_cycle + CONSUMER_LATENCY):
                await sim.tick()
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set2_cycle):
                await sim.tick()
            for _ in range(50):
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(circ.queue_all_cmd)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_set_two_bytes(self, clock_shift):
        circ = InstConsumerTester(config(clock_shift=clock_shift))

        circ.rand_dds_set_two_bytes(id=range(11))
        circ.rand_dds_set_two_bytes(id=range(11, 22))
        circ.rand_dds_set_two_bytes(id=range(11, 22))
        circ.rand_dds_set_two_bytes(id=range(11, 22))

        async def consumer(sim):
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            sim.set(circ.enable, 0)
            circ.pop_dds0_check(sim)
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set1_cycle):
                await sim.tick()
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set1_cycle + CONSUMER_LATENCY):
                await sim.tick()
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set1_cycle):
                await sim.tick()
            for _ in range(50):
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(circ.queue_all_cmd)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_set_four_bytes(self, clock_shift):
        circ = InstConsumerTester(config(clock_shift=clock_shift))

        circ.rand_dds_set_four_bytes(id=range(11))
        circ.rand_dds_set_four_bytes(id=range(11, 22))
        circ.rand_dds_set_four_bytes(id=range(11, 22))
        circ.rand_dds_set_four_bytes(id=range(11, 22))

        async def consumer(sim):
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            sim.set(circ.enable, 0)
            circ.pop_dds0_check(sim)
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set2_cycle):
                await sim.tick()
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set2_cycle + CONSUMER_LATENCY):
                await sim.tick()
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_set2_cycle):
                await sim.tick()
            for _ in range(50):
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(circ.queue_all_cmd)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_reset(self, clock_shift):
        circ = InstConsumerTester(config(clock_shift=clock_shift))

        circ.rand_dds_reset(id=range(11))
        circ.rand_dds_reset(id=range(11, 22))
        circ.rand_dds_reset(id=range(11, 22))
        circ.rand_dds_reset(id=range(11, 22))

        async def consumer(sim):
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            sim.set(circ.enable, 0)
            circ.pop_dds0_check(sim)
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_reset_cycle):
                await sim.tick()
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_reset_cycle + CONSUMER_LATENCY):
                await sim.tick()
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_reset_cycle):
                await sim.tick()
            for _ in range(50):
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(circ.queue_all_cmd)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_get_two_bytes(self, clock_shift):
        circ = InstConsumerTester(config(clock_shift=clock_shift))

        circ.rand_dds_get_two_bytes(id=range(11))
        circ.rand_dds_get_two_bytes(id=range(11, 22))
        circ.rand_dds_get_two_bytes(id=range(11, 22))
        circ.rand_dds_get_two_bytes(id=range(11, 22))

        async def consumer(sim):
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            sim.set(circ.enable, 0)
            circ.pop_dds0_check(sim)
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_get1_cycle):
                await sim.tick()
            # The two read from two dds channel conflicts with each other
            assert sim.get(circ.fifos.result_fifo.level) == 1
            await circ.read_result.call(sim)
            assert sim.get(circ.fifos.result_fifo.level) == 0
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            dds2 = circ.pop_dds1_check(sim)
            for _ in range(circ.dds_get1_cycle + CONSUMER_LATENCY):
                await sim.tick()
            dds3 = circ.pop_dds1_check(sim)
            for _ in range(circ.dds_get1_cycle):
                await sim.tick()
            assert sim.get(circ.fifos.result_fifo.level) == 2
            assert (await circ.read_result.call(sim)).data == dds2['data']
            assert (await circ.read_result.call(sim)).data == dds3['data']
            assert sim.get(circ.fifos.result_fifo.level) == 0
            for _ in range(50):
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(circ.queue_all_cmd)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_dds_get_four_bytes(self, clock_shift):
        circ = InstConsumerTester(config(clock_shift=clock_shift))

        circ.rand_dds_get_four_bytes(id=range(11))
        circ.rand_dds_get_four_bytes(id=range(11, 22))
        circ.rand_dds_get_four_bytes(id=range(11, 22))
        circ.rand_dds_get_four_bytes(id=range(11, 22))

        async def consumer(sim):
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            sim.set(circ.enable, 0)
            circ.pop_dds0_check(sim)
            circ.pop_dds1_check(sim)
            for _ in range(circ.dds_get2_cycle):
                await sim.tick()
            # The two read from two dds channel conflicts with each other
            assert sim.get(circ.fifos.result_fifo.level) == 1
            await circ.read_result.call(sim)
            assert sim.get(circ.fifos.result_fifo.level) == 0
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            dds2 = circ.pop_dds1_check(sim)
            for _ in range(circ.dds_get2_cycle + CONSUMER_LATENCY):
                await sim.tick()
            dds3 = circ.pop_dds1_check(sim)
            for _ in range(circ.dds_get2_cycle):
                await sim.tick()
            assert sim.get(circ.fifos.result_fifo.level) == 2
            assert (await circ.read_result.call(sim)).data == dds2['data']
            assert (await circ.read_result.call(sim)).data == dds3['data']
            assert sim.get(circ.fifos.result_fifo.level) == 0
            for _ in range(50):
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(circ.queue_all_cmd)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)

    @pytest.mark.parametrize("spi", [False, True])
    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_spi(self, spi, clock_shift):
        circ = InstConsumerTester(config(spi=spi, clock_shift=clock_shift))

        circ.rand_spi(div=range(3))
        circ.rand_spi(div=range(3))
        circ.rand_spi(div=range(3))

        def spi_cycle(div):
            return div * (18 * 2 + 1) + 1

        async def consumer(sim):
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            sim.set(circ.enable, 0)
            spi1 = circ.pop_spi_check(sim)
            for _ in range(spi_cycle(spi1['div'])):
                await sim.tick()
            for _ in range(3):
                await sim.tick()
            if spi1['save_result']:
                assert sim.get(circ.fifos.result_fifo.level) == 1
                assert (await circ.read_result.call(sim)).data == (spi1['result'] if spi else 0)
            assert sim.get(circ.fifos.result_fifo.level) == 0
            for _ in range(20):
                await sim.tick()
            sim.set(circ.enable, 1)
            for _ in range(CONSUMER_LATENCY):
                await sim.tick()
            spi2 = circ.pop_spi_check(sim)
            for _ in range(spi_cycle(spi2['div']) + CONSUMER_LATENCY):
                await sim.tick()
            spi3 = circ.pop_spi_check(sim)
            for _ in range(spi_cycle(spi3['div']) + CONSUMER_LATENCY):
                await sim.tick()
            for _ in range(3):
                await sim.tick()
            if spi2['save_result']:
                assert sim.get(circ.fifos.result_fifo.level) >= 1
                assert (await circ.read_result.call(sim)).data == (spi2['result'] if spi else 0)
            if spi3['save_result']:
                assert sim.get(circ.fifos.result_fifo.level) == 1
                assert (await circ.read_result.call(sim)).data == (spi3['result'] if spi else 0)
            assert sim.get(circ.fifos.result_fifo.level) == 0
            for _ in range(50):
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(circ.queue_all_cmd)
            sim.add_testbench(consumer)
            circ.add_testbenches(sim)
