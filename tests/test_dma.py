#

from amaranth import *
from amaranth.lib import io
from amaranth.lib.memory import Memory, MemoryData
from amaranth.utils import exact_log2

from amaranth_axi.axibus import AXI3
from amaranth_axi.axitools import AXISlaveReadIFace

from transactron import Transaction, TModule, Method, def_method
from transactron.testing import TestCaseWithSimulator, TestbenchIO as _TestbenchIO, SimpleTestCircuit, CallTrigger
from transactron.lib.adapters import AdapterTrans

from molecube_amaranth.config import Config
from molecube_amaranth.csr import Registers
from molecube_amaranth.dma import CountKeeper, AXIReadStream, DMAController
from molecube_amaranth.fifo import Fifos

import pytest
import random

class TestCountKeeper(TestCaseWithSimulator):
    def test_latency(self):
        circ = SimpleTestCircuit(CountKeeper(6))

        async def f(sim):
            assert (await circ.add.call_try(sim, count=5)) is not None
            assert not (await circ.done.call_try(sim)).last
            assert not (await circ.done.call_try(sim)).last
            assert not (await circ.done.call_try(sim)).last
            assert not (await circ.done.call_try(sim)).last
            assert not (await circ.done.call_try(sim)).last
            add, done = await CallTrigger(sim).call(circ.add, count=0).call(circ.done)
            assert add is not None
            assert done.last
            add, done = await CallTrigger(sim).call(circ.add, count=0).call(circ.done)
            assert add is not None
            assert done.last
            add, done = await CallTrigger(sim).call(circ.add, count=1).call(circ.done)
            assert add is not None
            assert done.last
            assert not (await circ.done.call_try(sim)).last

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    def test_random(self):
        count_width = 3
        circ = SimpleTestCircuit(CountKeeper(count_width))

        nblocks = 100
        counts = []

        async def producer(sim):
            for _ in range(nblocks):
                for _ in range(random.randint(0, 4)):
                    await sim.tick()
                count = random.randint(0, (1 << count_width) - 1)
                assert (await circ.add.call(sim, count=count)) is not None
                counts.append(count)

        async def consumer(sim):
            for _ in range(nblocks):
                await sim.delay(0)
                while not counts:
                    await sim.tick()
                    await sim.delay(0)
                count = counts.pop(0)
                for i in range(count + 1):
                    for _ in range(random.randint(0, 4)):
                        await sim.tick()
                    done = await circ.done.call_try(sim)
                    assert done.last == (i == count)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)


class AXIReadStreamWrapper(Elaboratable):
    def __init__(self, *, addr_width, block_len, blocks_width):
        axi = AXI3(64, addr_width, 3).create()
        self.stream = AXIReadStream(axi, block_len, blocks_width)

        self.read_slave = AXISlaveReadIFace(axi)

        self._read_get_and_done = Method(i=[('data', 64)],
                                         o=[('addr', addr_width), ('last', 1)])

        self.read_queue = _TestbenchIO(AdapterTrans.create(self.stream.queue))
        self.read_reply = _TestbenchIO(AdapterTrans.create(self.stream.get))
        self.read_get = _TestbenchIO(AdapterTrans.create(self.read_slave.get))
        self.read_done = _TestbenchIO(AdapterTrans.create(self.read_slave._done))
        self.read_get_and_done = _TestbenchIO(AdapterTrans.create(self._read_get_and_done))

    def elaborate(self, plat):
        m = TModule()

        @def_method(m, self._read_get_and_done)
        def _(data):
            req = self.read_slave.get(m)
            self.read_slave.done(m, id=req.id, data=data, last=req.last)
            m.d.sync += Assert(req.id == 0)
            return dict(addr=req.addr, last=req.last)

        m.submodules.stream = self.stream
        m.submodules.read_slave = self.read_slave

        m.submodules.read_queue = self.read_queue
        m.submodules.read_reply = self.read_reply
        m.submodules.read_get = self.read_get
        m.submodules.read_done = self.read_done
        m.submodules.read_get_and_done = self.read_get_and_done

        return m


class TestAXIReadStream(TestCaseWithSimulator):
    # The limited depth for the count keeper and the AXI read master latency
    # makes it possible for shorter block length (<=2)
    # to not saturate the throughput.
    @pytest.mark.parametrize("block_len", [3, 5, 16])
    def test_stream_full_throughput(self, block_len):
        index_width = 10
        blocks_width = 4
        circ = AXIReadStreamWrapper(addr_width=index_width + 3, block_len=block_len,
                                    blocks_width=blocks_width)
        mem = [random.randint(0, (1 << 64) - 1) for _ in range(1 << index_width)]

        def rand_req():
            blocks = random.randint(0, (1 << blocks_width) - 1)
            req_size = block_len * (blocks + 1)
            # The page alignment code can only handle one page boundary crossing
            assert req_size <= 512
            idx = random.randint(0, (1 << index_width) - req_size)
            page0 = idx >> 9
            page1 = (idx + req_size) >> 9
            if page0 != page1: # Crossing 4K boundary
                orig_idx = idx
                page1_start = page1 << 9
                # Place the transfer boundary on the page boundary
                idx = ((idx - page1_start) // block_len) * block_len + page1_start
                assert idx >= 0
                assert idx <= (1 << index_width) - req_size
            addr = (idx << 3) | random.randint(0, 7)
            return dict(addr=addr, blocks=blocks)

        nrequests = 100
        requests = [rand_req() for _ in range(nrequests)]

        async def producer(sim):
            assert (await circ.read_queue.call_try(sim, **requests[0])) is not None
            for i in range(1, nrequests):
                await circ.read_queue.call(sim, **requests[i])

        async def read_get_and_done(sim, data, wait=True):
            res = CallTrigger(sim).call(circ.read_get_and_done, data=data)
            if wait:
                res = res.until_done()
            return (await res)[0]

        async def read_return_block(sim, start_idx, wait0=True):
            for i in range(block_len):
                req = await read_get_and_done(sim, mem[start_idx + i], wait0 and (i == 0))
                assert req is not None
                assert (req.addr >> 3) == start_idx + i
                assert req.last == (i == block_len - 1)

        async def read_return_blocks(sim, start_idx, blocks, wait0=True):
            for i in range(blocks + 1):
                await read_return_block(sim, start_idx + i * block_len, wait0 and (i == 0))

        async def replier(sim):
            for i in range(nrequests):
                await read_return_blocks(sim, requests[i]['addr'] >> 3,
                                         requests[i]['blocks'], i == 0)

        async def read_reply(sim, wait=True):
            res = CallTrigger(sim).call(circ.read_reply)
            if wait:
                res = res.until_done()
            return (await res)[0]

        async def consume_blocks(sim, start_idx, blocks, wait0=True):
            total_trans = (blocks + 1) * block_len
            for i in range(total_trans):
                rep = await read_reply(sim, wait0 and (i == 0))
                assert rep.data == mem[start_idx + i]
                assert rep.last == (i == total_trans - 1)

        async def consumer(sim):
            for i in range(nrequests):
                await consume_blocks(sim, requests[i]['addr'] >> 3,
                                     requests[i]['blocks'], i == 0)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(replier)
            sim.add_testbench(consumer)

    @pytest.mark.parametrize("block_len", [2, 5, 16])
    def test_stream_random(self, block_len):
        index_width = 10
        blocks_width = 4
        circ = AXIReadStreamWrapper(addr_width=index_width + 3, block_len=block_len,
                                    blocks_width=blocks_width)
        mem = [random.randint(0, (1 << 64) - 1) for _ in range(1 << index_width)]

        def rand_req():
            blocks = random.randint(0, (1 << blocks_width) - 1)
            req_size = block_len * (blocks + 1)
            # The page alignment code can only handle one page boundary crossing
            assert req_size <= 512
            idx = random.randint(0, (1 << index_width) - req_size)
            page0 = idx >> 9
            page1 = (idx + req_size) >> 9
            if page0 != page1: # Crossing 4K boundary
                orig_idx = idx
                page1_start = page1 << 9
                # Place the transfer boundary on the page boundary
                idx = ((idx - page1_start) // block_len) * block_len + page1_start
                assert idx >= 0
                assert idx <= (1 << index_width) - req_size
            addr = (idx << 3) | random.randint(0, 7)
            return dict(addr=addr, blocks=blocks)

        nrequests = 100
        requests = [rand_req() for _ in range(nrequests)]

        async def rand_wait(sim, maxwait=3):
            for _ in range(random.randint(0, maxwait)):
                await sim.tick()

        async def producer(sim):
            await rand_wait(sim)
            req = rand_req()
            assert (await circ.read_queue.call_try(sim, **requests[0])) is not None
            for i in range(1, nrequests):
                await rand_wait(sim, 20)
                req = rand_req()
                await circ.read_queue.call(sim, **requests[i])

        async def read_get_and_done(sim, data):
            await rand_wait(sim)
            wait_cycle = random.randint(0, 3)
            if wait_cycle == 0:
                return await circ.read_get_and_done.call(sim, data=data)
            req = await circ.read_get.call(sim)
            assert req.id == 0
            for _ in range(wait_cycle - 1):
                await sim.tick()
            await circ.read_done.call(sim, id=req.id, data=data, last=req.last, resp=0)
            return req

        async def read_return_block(sim, start_idx):
            for i in range(block_len):
                req = await read_get_and_done(sim, mem[start_idx + i])
                assert req is not None
                assert (req.addr >> 3) == start_idx + i
                assert req.last == (i == block_len - 1)

        async def read_return_blocks(sim, start_idx, blocks):
            for i in range(blocks + 1):
                await read_return_block(sim, start_idx + i * block_len)

        async def replier(sim):
            for i in range(nrequests):
                await read_return_blocks(sim, requests[i]['addr'] >> 3,
                                         requests[i]['blocks'])

        async def read_reply(sim):
            res = CallTrigger(sim).call(circ.read_reply)
            res = res.until_done()
            return (await res)[0]

        async def consume_blocks(sim, start_idx, blocks):
            total_trans = (blocks + 1) * block_len
            for i in range(total_trans):
                await rand_wait(sim, 5)
                rep = await read_reply(sim)
                assert rep.data == mem[start_idx + i]
                assert rep.last == (i == total_trans - 1)

        async def consumer(sim):
            for i in range(nrequests):
                await consume_blocks(sim, requests[i]['addr'] >> 3,
                                     requests[i]['blocks'])

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(replier)
            sim.add_testbench(consumer)


class AXIMem(Elaboratable):
    def __init__(self, axi, mem_len):
        self.data_width = len(axi.RDATA)
        self.axi = axi
        self.data = MemoryData(shape=self.data_width,
                               depth=mem_len, init=())

    def elaborate(self, plat):
        m = TModule()

        m.submodules.read_slave = read_slave = AXISlaveReadIFace(self.axi)
        m.submodules.mem = mem = Memory(self.data)
        read_port = mem.read_port(domain='comb')

        addr_shift = exact_log2(self.data_width) - 3

        with Transaction().body(m):
            req = read_slave.get(m)
            m.d.comb += read_port.addr.eq(req.addr >> addr_shift)
            read_slave.done(m, id=req.id, data=read_port.data, last=req.last)

        return m


class DMAControllerWrapper(Elaboratable):
    def __init__(self):
        axi = AXI3(64, 16, 3).create()
        config = Config()

        self.mem = AXIMem(axi, 1 << 10)
        self.csr = Registers(config)
        self.fifos = Fifos(32, dma_addr_width=len(axi.ARADDR))
        self.fifo = self.fifos.dma_cmd_fifo
        self.ctrl = DMAController(axi, self.csr, self.fifos)

        self.data = self.mem.data

        self.queue_cmd = _TestbenchIO(AdapterTrans.create(self.fifo.write))
        self.read_inst = _TestbenchIO(AdapterTrans.create(self.ctrl.read_inst))
        self.inst_started = _TestbenchIO(AdapterTrans.create(self.ctrl.inst_started))
        self.inst_stopped = _TestbenchIO(AdapterTrans.create(self.ctrl.inst_stopped))
        self.trig_timeout = _TestbenchIO(AdapterTrans.create(self.ctrl.trig_timeout))

    def elaborate(self, plat):
        m = TModule()

        m.submodules.mem = self.mem
        m.submodules.csr = self.csr
        m.submodules.fifos = self.fifos
        m.submodules.ctrl = self.ctrl

        m.submodules.queue_cmd = self.queue_cmd
        m.submodules.read_inst = self.read_inst
        m.submodules.inst_started = self.inst_started
        m.submodules.inst_stopped = self.inst_stopped
        m.submodules.trig_timeout = self.trig_timeout

        return m


class TestDMAController(TestCaseWithSimulator):
    def test_overflow(self):
        circ = DMAControllerWrapper()

        async def wait(sim, n=5):
            for _ in range(n):
                await sim.tick()

        async def f(sim):
            assert sim.get(circ.csr.dma_status) == 8 << 8

            await circ.inst_started.call(sim)
            await wait(sim)
            # Running
            assert sim.get(circ.csr.dma_status) == 9 << 8
            await circ.inst_stopped.call(sim)
            await wait(sim)
            # Stopped
            assert sim.get(circ.csr.dma_status) == 8 << 8

            await circ.inst_started.call(sim)
            await wait(sim)
            # Running & underflown
            assert sim.get(circ.csr.dma_status) == 11 << 8
            await circ.inst_stopped.call(sim)
            await wait(sim)
            # Underflown
            assert sim.get(circ.csr.dma_status) == 10 << 8

            await circ.inst_started.call(sim)
            await wait(sim)
            # Running & underflown
            assert sim.get(circ.csr.dma_status) == 11 << 8
            await circ.inst_stopped.call(sim)
            await wait(sim)
            # Underflown
            assert sim.get(circ.csr.dma_status) == 10 << 8

            await circ.queue_cmd.call(sim, first=1)
            await wait(sim)
            # Cleared
            assert sim.get(circ.csr.dma_status) == 8 << 8

            await circ.inst_started.call(sim)
            await wait(sim)
            # Running
            assert sim.get(circ.csr.dma_status) == 9 << 8
            await circ.inst_stopped.call(sim)
            await wait(sim)
            # Stopped
            assert sim.get(circ.csr.dma_status) == 8 << 8

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)


    def test_trig_timeout(self):
        circ = DMAControllerWrapper()

        async def wait(sim, n=5):
            for _ in range(n):
                await sim.tick()

        async def f(sim):
            assert sim.get(circ.csr.dma_status) == 8 << 8

            await circ.trig_timeout.call(sim)
            await wait(sim)
            assert sim.get(circ.csr.dma_status) == 12 << 8

            await circ.queue_cmd.call(sim, first=1)
            await wait(sim)
            # Cleared
            assert sim.get(circ.csr.dma_status) == 8 << 8

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)


    def test_rand_inst(self):
        circ = DMAControllerWrapper()

        write_data = bytearray()
        insts = []

        def add_rand_inst(l):
            data_len = (l + 1) * 16 - 2
            data = random.randint(0, (1 << data_len) - 1)
            inst = (data << 2) | l
            inst_mask = (1 << (data_len + 2)) - 1
            insts.append((inst, inst_mask))
            write_data.extend(inst.to_bytes((l + 1) * 2, 'little'))

        # Generate 2kB data which we'll split into two blocks of 1kB memory
        # Make sure the instruction crosses the 1kB boundary
        while len(write_data) < 1024 - 6:
            add_rand_inst(random.randint(0, 2))
        if len(write_data) == 1024 - 6:
            add_rand_inst(1)
            assert len(write_data) < 1024
        add_rand_inst(2)
        assert len(write_data) > 1024
        while len(write_data) <= 2048 - 6:
            add_rand_inst(random.randint(0, 2))

        padding_len = 2048 - len(write_data)
        if padding_len != 0:
            assert padding_len in (2, 4)
            add_rand_inst((padding_len - 2) >> 1)

        assert len(write_data) == 2048

        def test_inst(res):
            inst, mask = insts.pop(0)
            assert inst == res.inst & mask

        async def f(sim):
            for i in range(128):
                sim.set(circ.data[i],
                        int.from_bytes(write_data[i * 8:i * 8 + 8], 'little'))

            for i in range(128):
                sim.set(circ.data[i + 512],
                        int.from_bytes(write_data[(i + 128) * 8:(i + 128) * 8 + 8],
                                       'little'))

            await circ.queue_cmd.call(sim, addr=0, blocks=7)
            await circ.queue_cmd.call(sim, addr=4096, blocks=7)

            test_inst(await circ.read_inst.call(sim))
            while insts:
                test_inst(await circ.read_inst.call_try(sim))

            for _ in range(10):
                assert await circ.read_inst.call_try(sim) is None
                await sim.tick()

            assert sim.get(circ.csr.dma_status) == (8 << 8) | 2

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)
