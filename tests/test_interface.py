#

from amaranth import *
from amaranth.lib import io

from transactron import TModule, Method, def_method
from transactron.testing import TestCaseWithSimulator, TestbenchIO as _TestbenchIO
from transactron.lib.adapters import AdapterTrans

from amaranth_axi.axibus import AXI4
from amaranth_axi.axitools import AXIMasterWriteIFace, AXIMasterReadIFace

from molecube_amaranth.utils import get_init
from molecube_amaranth.csr import Registers
from molecube_amaranth.config import MAJOR_VERSION, MINOR_VERSION, Config
from molecube_amaranth.fifo import Fifos
from molecube_amaranth.controllers import IOController
from molecube_amaranth.interface import ControlInterface
from molecube_amaranth.io import PulseIO

import pytest
import random

def update_data(old_data, data, strb):
    for i in range(4):
        bitmask = 1 << i
        bytemask = 0xff << (i * 8)
        if not (strb & bitmask):
            data = (data & ~bytemask) | (old_data & bytemask)
    return data

def reg_mask(idx, reg):
    if idx in (0x10, 0x11):
        # ttl hi/lo mask bank 1
        return (1 << 24) - 1
    elif idx == 0x50:
        # dds timing 1
        return 0x071c71c7
    elif idx == 0x51:
        # dds timing 2
        return 0x007df7df
    else:
        return (1 << len(reg)) - 1

def rand_strb(idx):
    if idx == 0x5:
        return 1
    return random.randint(0, 0xf)

class InterfaceWrapper(Elaboratable):
    def __init__(self, *, addr_prefix=0, addr_width=9, clock_shift=1):
        config = Config(CLOCK_SHIFT=clock_shift)
        self.csr = Registers(config)
        self.fifos = Fifos(32)
        axi = AXI4(32, addr_width, 6, len_width=4).create()
        self.pulseio = PulseIO.from_config(None, config)
        self.ioctrl = IOController(self.pulseio, self.csr, self.fifos,
                                   clock_shift=config.CLOCK_SHIFT)
        self.iface = ControlInterface(axi, self.csr, self.fifos, self.ioctrl,
                                      prefix=addr_prefix, valid_width=9)

        self.reader = AXIMasterReadIFace(self.iface.axi)
        self.writer = AXIMasterWriteIFace(self.iface.axi)

        self._read_request = Method(i=[('addr', addr_width)])
        self._read_reply = Method(o=[('data', 32), ('resp', 2)])

        self._write_request = Method(i=[('addr', addr_width), ('data', 32), ('strb', 4)])
        self._write_reply = Method(o=[('resp', 2)])

        self.read_request = _TestbenchIO(AdapterTrans.create(self._read_request))
        self.read_reply = _TestbenchIO(AdapterTrans.create(self._read_reply))
        self.write_request = _TestbenchIO(AdapterTrans.create(self._write_request))
        self.write_reply = _TestbenchIO(AdapterTrans.create(self._write_reply))

        self.read_inst = _TestbenchIO(AdapterTrans.create(self.fifos.cmd_fifo.read))
        self.write_result = _TestbenchIO(AdapterTrans.create(self.fifos.result_fifo.write))

        self.read_only_regs = {
            0x02: self.csr.timing_status,
            # 0x04: self.ttl_out_reg(0),
            0x06: MAJOR_VERSION,
            0x07: MINOR_VERSION,
            0x20: self.csr.dbg_inst_word_count.value,
            0x21: self.csr.dbg_inst_count.value,
            0x22: self.csr.dbg_ttl_count.value,
            0x23: self.csr.dbg_dds_count.value,
            0x24: self.csr.dbg_wait_count.value,
            0x25: self.csr.dbg_clear_count.value,
            0x26: self.csr.dbg_loopback_count.value,
            0x27: self.csr.dbg_clock_count.value,
            0x28: self.csr.dbg_spi_count.value,
            0x29: self.csr.dbg_underflow_cycle.value,
            0x2a: self.csr.dbg_inst_cycle.value,
            # 0x2b: self.csr.dbg_ttl_cycle.value,
            # 0x2c: self.csr.dbg_wait_cycle.value,
            # 0x2d: self.csr.dbg_result_overflow_count.value,
            0x2e: self.csr.dbg_result_count,
            0x2f: self.csr.dbg_result_generated.value,
            0x30: self.csr.dbg_result_consumed.value,

            # 0x40: self.ttl_out_reg(1),
            # 0x41: self.ttl_out_reg(2),
            # 0x42: self.ttl_out_reg(3),
            # 0x43: self.ttl_out_reg(4),
            # 0x44: self.ttl_out_reg(5),
            # 0x45: self.ttl_out_reg(6),
            # 0x46: self.ttl_out_reg(7),
        }

        self.read_write_regs = {
            0x00: self.ttl_hi_reg(0),
            0x01: self.ttl_lo_reg(0),
            0x03: self.csr.timing_ctrl,
            0x05: self.csr.clockout_div,
            0x10: self.ttl_hi_reg(1),
            0x11: self.ttl_lo_reg(1),
            # 0x12: self.ttl_hi_reg(2),
            # 0x13: self.ttl_lo_reg(2),
            # 0x14: self.ttl_hi_reg(3),
            # 0x15: self.ttl_lo_reg(3),
            # 0x16: self.ttl_hi_reg(4),
            # 0x17: self.ttl_lo_reg(4),
            # 0x18: self.ttl_hi_reg(5),
            # 0x19: self.ttl_lo_reg(5),
            # 0x1a: self.ttl_hi_reg(6),
            # 0x1b: self.ttl_lo_reg(6),
            # 0x1c: self.ttl_hi_reg(7),
            # 0x1d: self.ttl_lo_reg(7),
            0x1e: self.csr.loopback,

            0x50: self.csr.dds_timing1,
            0x51: self.csr.dds_timing2,
        }

    def ttl_out_reg(self, idx):
        return self.csr.ttl_out[idx * 32:(idx + 1) * 32]

    def ttl_hi_reg(self, idx):
        return self.csr.ttl_hi_mask[idx * 32:(idx + 1) * 32]

    def ttl_lo_reg(self, idx):
        return self.csr.ttl_lo_mask[idx * 32:(idx + 1) * 32]

    def randomize_read_only_regs(self, sim):
        vals = {}
        for idx, reg in self.read_only_regs.items():
            if isinstance(reg, int):
                val = reg
            else:
                val = random.randint(0, (1 << len(reg)) - 1)
                sim.set(reg, val)
            vals[idx] = val
        return vals

    def elaborate(self, plat):
        m = TModule()

        m.submodules.csr = self.csr
        m.submodules.fifos = self.fifos
        m.submodules.iface = self.iface

        m.submodules.reader = self.reader
        m.submodules.writer = self.writer
        m.submodules.pulseio = self.pulseio
        m.submodules.ioctrl = self.ioctrl

        @def_method(m, self._read_request)
        def _(addr):
            self.reader.request(m, id=0, size=2, len=0, burst=0, addr=addr)

        @def_method(m, self._read_reply)
        def _():
            reply = self.reader.reply(m)
            m.d.sync += [Assert(reply.id == 0),
                         Assert(reply.last == 1)]
            return dict(data=reply.data, resp=reply.resp)

        @def_method(m, self._write_request)
        def _(addr, data, strb):
            self.writer.addr_request(m, addr=addr, id=0, size=2, len=0, burst=0)
            self.writer.data_request(m, data=data, strb=strb, last=1)

        @def_method(m, self._write_reply)
        def _():
            reply = self.writer.reply(m)
            m.d.sync += [Assert(reply.id == 0)]
            return dict(resp=reply.resp)

        m.submodules.read_request = self.read_request
        m.submodules.read_reply = self.read_reply
        m.submodules.write_request = self.write_request
        m.submodules.write_reply = self.write_reply

        m.submodules.read_inst = self.read_inst
        m.submodules.write_result = self.write_result

        return m

class TestInterface(TestCaseWithSimulator):
    @pytest.mark.parametrize("addr_width", [9, 20])
    def test_idle(self, addr_width):
        iface = InterfaceWrapper(addr_width=addr_width)
        async def f(sim):
            for _ in range(10):
                assert sim.get(iface.csr.ttl_hi_mask) == 0
                assert sim.get(iface.csr.ttl_lo_mask) == 0
                assert sim.get(iface.csr.ttl_out) == 0

                assert sim.get(iface.csr.timing_status) == 0
                assert sim.get(iface.csr.timing_ctrl) == 0
                assert sim.get(iface.csr.clockout_div) == 255
                assert sim.get(iface.csr.loopback) == 0

                dds_timing1 = sim.get(iface.csr.dds_timing1)
                assert dds_timing1 != 0
                assert sim.get(iface.csr.dds_write_adsu) == dds_timing1 & 0x3f
                assert sim.get(iface.csr.dds_write_wrlow) == (dds_timing1 >> 6) & 0x3f
                assert sim.get(iface.csr.dds_write_adhd) == (dds_timing1 >> 12) & 0x3f
                assert sim.get(iface.csr.dds_write_fuddl) == (dds_timing1 >> 18) & 0x3f
                assert sim.get(iface.csr.dds_write_fudhd) == (dds_timing1 >> 24) & 0x3f

                dds_timing2 = sim.get(iface.csr.dds_timing2)
                assert dds_timing2 != 0
                assert sim.get(iface.csr.dds_read_asu) == dds_timing2 & 0x3f
                assert sim.get(iface.csr.dds_read_rdl) == (dds_timing2 >> 6) & 0x3f
                assert sim.get(iface.csr.dds_read_rdhoz) == (dds_timing2 >> 12) & 0x3f
                assert sim.get(iface.csr.dds_reset_rshd) == (dds_timing2 >> 18) & 0x3f

                for (k, v) in iface.csr.all_counters.items():
                    assert sim.get(v.value) == 0

                assert (await iface.read_reply.call_try(sim)) is None
                assert (await iface.write_reply.call_try(sim)) is None
                assert (await iface.read_inst.call_try(sim)) is None

                await sim.tick()

        with self.run_simulation(iface) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("addr_width", [9, 20])
    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_read(self, addr_width, clock_shift):
        iface = InterfaceWrapper(addr_width=addr_width, clock_shift=clock_shift)

        async def f(sim):
            for _ in range(5):
                vals = iface.randomize_read_only_regs(sim)
                # Make sure the values have propagated to the shadow version
                await sim.tick()
                await sim.tick()
                for idx, val in vals.items():
                    assert (await iface.read_request.call_try(sim, addr=idx * 4)) is not None
                    for _ in range(25):
                        await sim.tick()
                    assert (await iface.read_reply.call_try(sim)).data == val

        with self.run_simulation(iface) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("addr_width", [9, 20])
    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_read_throughput(self, addr_width, clock_shift):
        iface = InterfaceWrapper(addr_width=addr_width, clock_shift=clock_shift)

        ncycles = 100
        vals = {}
        read_req = []

        async def producer(sim):
            vals.update(iface.randomize_read_only_regs(sim))
            idxs = list(vals.keys())

            # Make sure the values have propagated to the shadow version
            await sim.tick()
            await sim.tick()

            for _ in range(ncycles):
                idx = random.choice(idxs)
                read_req.append(idx)
                assert (await iface.read_request.call_try(sim, addr=idx * 4)) is not None

        async def consumer(sim):
            assert (await iface.read_reply.call(sim)).data == vals[read_req.pop(0)]
            for _ in range(ncycles - 1):
                assert (await iface.read_reply.call_try(sim)).data == vals[read_req.pop(0)]

        with self.run_simulation(iface) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)

    @pytest.mark.parametrize("addr_width", [9, 20])
    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_write(self, addr_width, clock_shift):
        iface = InterfaceWrapper(addr_width=addr_width, clock_shift=clock_shift)
        masks = {}
        vals = {}
        for idx, reg in iface.read_write_regs.items():
            masks[idx] = reg_mask(idx, reg)
            vals[idx] = Const.cast(get_init(reg)).value

        async def f(sim):
            for _ in range(5):
                for idx, reg in iface.read_write_regs.items():
                    data = random.randint(0, 0xffff_ffff)
                    strb = rand_strb(idx)
                    assert (await iface.write_request.call_try(sim, addr=idx * 4,
                                                               strb=strb,
                                                               data=data)) is not None
                    vals[idx] = data = update_data(vals[idx], data, strb) & masks[idx]
                    for _ in range(3):
                        await sim.tick()
                    assert (await iface.write_reply.call_try(sim)) is not None
                    for _ in range(3):
                        await sim.tick()
                    assert sim.get(reg) == data

                    assert (await iface.read_request.call_try(sim, addr=idx * 4)) is not None
                    for _ in range(25):
                        await sim.tick()
                    assert (await iface.read_reply.call_try(sim)).data == data

                    assert sim.get(iface.pulseio.ttlout_port.o) == (sim.get(iface.csr.ttl_out) | sim.get(iface.csr.ttl_hi_mask)) & ~sim.get(iface.csr.ttl_lo_mask) & 0xff_ffff_ffff_ffff

        with self.run_simulation(iface) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("addr_width", [9, 20])
    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_write_throughput(self, addr_width, clock_shift):
        iface = InterfaceWrapper(addr_width=addr_width, clock_shift=clock_shift)
        masks = {}
        vals = {}
        for idx, reg in iface.read_write_regs.items():
            masks[idx] = reg_mask(idx, reg)
            vals[idx] = Const.cast(get_init(reg)).value
        idxs = list(vals.keys())

        ncycles = 100

        async def producer(sim):
            for _ in range(ncycles):
                idx = random.choice(idxs)
                data = random.randint(0, 0xffff_ffff)
                strb = rand_strb(idx)
                assert (await iface.write_request.call_try(sim, addr=idx * 4,
                                                           strb=strb,
                                                           data=data)) is not None
                vals[idx] = update_data(vals[idx], data, strb) & masks[idx]

        async def consumer(sim):
            await iface.write_reply.call(sim)
            for _ in range(ncycles - 1):
                assert (await iface.write_reply.call_try(sim)) is not None

            # Make sure the data is propagated to the target from the shadow
            for _ in range(10):
                await sim.tick()

            for idx, reg in iface.read_write_regs.items():
                assert sim.get(reg) == vals[idx]

        with self.run_simulation(iface) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)

    @pytest.mark.parametrize("addr_width", [9, 20])
    @pytest.mark.parametrize("clock_shift", [0, 1])
    def test_ttlout(self, addr_width, clock_shift):
        iface = InterfaceWrapper(addr_width=addr_width, clock_shift=clock_shift)
        idxs = [0x4, 0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46]

        async def f(sim):
            ttl_val = 0
            for _ in range(20):
                for bank, idx in enumerate(idxs):
                    byte = random.randint(0, 3)
                    hi = random.randint(0, 0xff)
                    lo = random.randint(0, 0xff)
                    lo = lo & ~hi

                    cmd = hi | (lo << 8) | (byte << 16)

                    assert (await iface.write_request.call_try(sim, addr=idx * 4,
                                                               strb=0xf,
                                                               data=cmd)) is not None
                    for _ in range(3):
                        await sim.tick()
                    assert (await iface.write_reply.call_try(sim)) is not None

                    shift = (byte + 4 * bank) * 8
                    ttl_val = ((ttl_val | (hi << shift)) & ~(lo << shift)) & 0xff_ffff_ffff_ffff
                    for _ in range(8):
                        await sim.tick()

                    assert sim.get(iface.csr.ttl_out) == ttl_val

            for bank, idx in enumerate(idxs):
                assert (await iface.read_request.call_try(sim, addr=idx * 4)) is not None
                for _ in range(25):
                    await sim.tick()
                assert (await iface.read_reply.call_try(sim)).data == (ttl_val >> (bank * 32)) & 0xffff_ffff

            assert sim.get(iface.pulseio.ttlout_port.o) == (sim.get(iface.csr.ttl_out) | sim.get(iface.csr.ttl_hi_mask)) & ~sim.get(iface.csr.ttl_lo_mask) & 0xff_ffff_ffff_ffff

        with self.run_simulation(iface) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("addr_width", [9, 20])
    def test_write_command(self, addr_width):
        iface = InterfaceWrapper(addr_width=addr_width)
        written = []
        start_reading = False

        async def producer(sim):
            nonlocal start_reading
            while True:
                data = random.randint(0, 0xffff_ffff)
                if (await iface.write_request.call_try(sim, addr=0x1f * 4,
                                                       strb=0xf, data=data)) is None:
                    break
                written.append(data)
            assert len(written) > 2000
            assert sim.get(iface.fifos.cmd_fifo.full) == 1
            start_reading = True
            if len(written) % 2 != 0:
                data = random.randint(0, 0xffff_ffff)
                written.append(data)
                await iface.write_request.call(sim, addr=0x1f * 4, strb=0xf, data=data)

        async def receiver(sim):
            await iface.write_reply.call(sim)
            reply_count = 1
            while reply_count < iface.fifos.cmd_fifo.depth * 2:
                assert (await iface.write_reply.call_try(sim)) is not None
                reply_count += 1
            while reply_count < len(written):
                await iface.write_reply.call(sim)
                reply_count += 1

        async def consumer(sim):
            while not start_reading:
                await sim.tick()
                await sim.delay(0)

            read_count = 0
            while read_count * 2 < len(written):
                inst = (await iface.read_inst.call_try(sim)).data
                assert inst == written[read_count * 2] | (written[read_count * 2 + 1] << 32)
                read_count += 1

            assert sim.get(iface.csr.dbg_inst_word_count.value) == len(written)

        with self.run_simulation(iface) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(receiver)
            sim.add_testbench(consumer)

    @pytest.mark.parametrize("addr_width", [9, 20])
    def test_result(self, addr_width):
        iface = InterfaceWrapper(addr_width=addr_width)

        async def f(sim):
            n1 = 10
            for _ in range(n1):
                assert (await iface.read_request.call_try(sim, addr=0x1f * 4)) is not None
                for _ in range(25):
                    await sim.tick()
                assert (await iface.read_reply.call_try(sim)).data == 0
            assert sim.get(iface.csr.dbg_result_generated.value) == 0
            assert sim.get(iface.csr.dbg_result_consumed.value) == n1

            n2 = 100
            results = []
            for _ in range(n2):
                data = random.randint(0, 0xffff_ffff)
                assert (await iface.write_result.call_try(sim, data=data)) is not None
                results.append(data)

            await sim.tick()
            await sim.tick()
            assert sim.get(iface.csr.dbg_result_generated.value) == n2
            assert sim.get(iface.csr.dbg_result_consumed.value) == n1

            for i in range(n2):
                assert (await iface.read_request.call_try(sim, addr=0x1f * 4)) is not None
                for _ in range(25):
                    await sim.tick()
                assert (await iface.read_reply.call_try(sim)).data == results[i]

            assert sim.get(iface.csr.dbg_result_generated.value) == n2
            assert sim.get(iface.csr.dbg_result_consumed.value) == n1 + n2

        with self.run_simulation(iface) as sim:
            sim.add_testbench(f)

    @pytest.mark.parametrize("prefix", [0, 0x23_0000])
    def test_error(self, prefix):
        iface = InterfaceWrapper(addr_width=32, addr_prefix=prefix)

        async def f(sim):
            valid_mask = (1 << 9) - 1
            for _ in range(5):
                while True:
                    addr = random.randint(0, valid_mask)
                    if addr >> 2 != 0x1f:
                        break
                addr |= prefix
                assert (await iface.read_request.call_try(sim, addr=addr)) is not None
                for _ in range(25):
                    await sim.tick()
                assert (await iface.read_reply.call_try(sim)).resp == 0

                assert (await iface.write_request.call_try(sim, addr=addr,
                                                           strb=0, data=0)) is not None
                for _ in range(3):
                    await sim.tick()
                assert (await iface.write_reply.call_try(sim)).resp == 0

            rw_idxs = list(iface.read_write_regs)
            for _ in range(10):
                addr = (random.choice(rw_idxs) << 2) | prefix
                assert (await iface.write_request.call_try(sim, addr=addr, strb=0xf,
                                                           data=random.randint(0, 0xffff_ffff))) is not None
                for _ in range(3):
                    await sim.tick()
                assert (await iface.write_reply.call_try(sim)).resp == 0

            results = []
            for _ in range(10):
                data = random.randint(0, 0xffff_ffff)
                assert (await iface.write_result.call_try(sim, data=data)) is not None
                results.append(data)
            reg_values = {idx: sim.get(reg) for idx, reg in iface.read_write_regs.items()}
            assert (await iface.read_inst.call_try(sim)) is None

            for _ in range(50):
                while True:
                    addr = random.randint(0, 0xffff_ffff)
                    if (addr >> 9) != (prefix >> 9):
                        break
                assert (await iface.read_request.call_try(sim, addr=addr)) is not None
                for _ in range(25):
                    await sim.tick()
                assert (await iface.read_reply.call_try(sim)).resp == 3

                assert (await iface.write_request.call_try(sim, addr=addr,
                                                           strb=0, data=0)) is not None
                for _ in range(3):
                    await sim.tick()
                assert (await iface.write_reply.call_try(sim)).resp == 3

            while results:
                assert (await iface.read_request.call_try(sim, addr=0x1f * 4 | prefix)) is not None
                for _ in range(25):
                    await sim.tick()
                assert (await iface.read_reply.call_try(sim)).data == results.pop(0)

            reg_values2 = {idx: sim.get(reg) for idx, reg in iface.read_write_regs.items()}
            assert reg_values2 == reg_values
            assert (await iface.read_inst.call_try(sim)) is None

        with self.run_simulation(iface) as sim:
            sim.add_testbench(f)
