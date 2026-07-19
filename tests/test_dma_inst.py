#

from amaranth import *
from amaranth.lib import io

from transactron import TModule
from transactron.testing import TestCaseWithSimulator, TestbenchIO as _TestbenchIO
from transactron.lib.adapters import AdapterTrans

from amaranth_axi.axibus import AXI3

from molecube_amaranth.config import Config
from molecube_amaranth.controllers import IOController
from molecube_amaranth.csr import Registers
from molecube_amaranth.dds import FSMState as DDSFSMState
from molecube_amaranth.dma import DMAController
from molecube_amaranth.dma_inst import DMAInstParser, DMAInstRunner
from molecube_amaranth.fifo import Fifos
from molecube_amaranth.io import PulseIO, sma_pin

from .utils import TTLChecker, ClockoutChecker, DDSChecker, SPIChecker

import pytest
import random

class ParserTester(Elaboratable):
    def __init__(self):
        self.csr = Registers(Config())
        self.parser = DMAInstParser(self.csr, 56)
        self.read = _TestbenchIO(AdapterTrans.create(self.parser.read))
        self.write = _TestbenchIO(AdapterTrans.create(self.parser.write))

    def elaborate(self, plat):
        m = TModule()

        m.submodules.csr = self.csr
        m.submodules.parser = self.parser
        m.submodules.read = self.read
        m.submodules.write = self.write

        return m

def __inst(l, op, data):
    assert l >= 0 and l <= 2
    assert op >> 2 == 0
    assert data >> (16 * (l + 1) - 4) == 0
    return (l | (op << 2) | (data << 4))

def _inst(l, op, spec, **kw):
    bit = 0
    data = 0
    for name, nbits in spec:
        val = kw[name]
        assert val >> nbits == 0
        data |= val << bit
        bit += nbits
    assert bit <= (l + 1) * 16 - 4
    return __inst(l, op, data)

def def_inst(l, op, spec):
    def cb(**kw):
        return _inst(l, op, spec, **kw)
    cb.inst_spec = spec
    return cb

wait1_inst = def_inst(0, 0, [('cycle', 12)])
ttl_set4_inst = def_inst(0, 1, [('bank4_1', 6), ('val1', 4)])
clockout_inst = def_inst(0, 3, [('period', 9)])

wait2_inst = def_inst(1, 0, [('cycle', 28)])
ttl_set16_inst = def_inst(1, 1, [('bank8_1', 5), ('val1', 8), ('bank8_2', 5), ('val2', 8)])
dds_set16_inst = def_inst(1, 2, [('bus_id', 1), ('dds_id', 4), ('fud', 1),
                                 ('addr', 6), ('data', 16)])

wait_trig_inst = def_inst(2, 0, [('chn', 8), ('edge', 1), ('cycle', 35)])
ttl_set32_inst = def_inst(2, 1, [('bank16_1', 4), ('val1', 16),
                                 ('bank16_2', 4), ('val2', 16)])
dds_set32_inst = def_inst(2, 2, [('bus_id', 1), ('dds_id', 4), ('fud', 1),
                                 ('addr', 6), ('data', 32)])
dac_inst = def_inst(2, 3, [('id', 2), ('cycle', 9), ('clk_pha', 1), ('clk_pol', 1),
                           ('data', 18)])

def rand_inst(instf, **kw):
    spec = instf.inst_spec
    args = {}
    for name, nbits in spec:
        args[name] = random.choice(kw.pop(name, range(1<<nbits)))
    assert not kw
    return instf(**args), args

def _check_fields(v, flds):
    for name, fldval in flds.items():
        assert getattr(v, name) == fldval

def _set_ttl(mask, value, bank, setval, bank_width):
    setmask = ((1 << bank_width) - 1) << (bank * bank_width)
    mask |= ((1 << bank_width) - 1) << (bank * bank_width)
    value = (value & ~setmask) | (setval << (bank * bank_width))
    return mask, value

class ParserState:
    def __init__(self, csr=None):
        self.actions = {}
        self.queue = []
        self.checker_actions = {}
        self.checker_queue = []
        self.dds_write_adsu = None
        if csr is not None:
            self.dds_write_adsu = self.csr.dds_write_adsu.init
        self.ttl_mask = 0
        self.ttl_value = 0

    def to_parsed_args(self, cmd):
        if 'wait' in cmd:
            wait = {'wait': cmd['wait']}
            is_trig = 0
        else:
            wait = {'wait_trig': cmd['wait_trig']}
            is_trig = 1
        actions = dict(**cmd['actions'])
        for name in ('clockout', 'ttl', 'dds0', 'dds1', 'dac'):
            actions[f'{name}_en'] = name in actions
        return dict(is_trig=is_trig, action=actions, wait=wait)

    async def run_checker_action(self, cmd, sim, checker):
        actions = cmd['actions']
        if 'clockout' in actions:
            checker.clockout_set_shifted(**actions['clockout'])
        if 'ttl' in actions:
            checker.ttl_set(actions['ttl'])
        for (offset, name) in ((0, 'dds0'), (11, 'dds1')):
            if name in actions:
                dds = dict(**actions[name])
                dds_type = dds.pop('type')
                if dds_type == 'set1':
                    checker.dds_set_two_bytes(**dds)
                else:
                    assert dds_type == 'set2'
                    checker.dds_set_four_bytes(**dds)
        if 'dac' in actions:
            checker.spi_set(**actions['dac'])
        if 'wait' in cmd:
            wait = cmd['wait']
            for _ in range(wait['cycle'] + 1):
                await sim.tick()
            return
        else:
            return cmd['wait_trig']

    def _finalize_actions(self):
        if 'ttl' in self.actions:
            ttl = self.actions['ttl']
            mask = ttl['mask']
            val = ttl['val']
            self.ttl_value = (self.ttl_value & ~mask) | val
            self.checker_actions['ttl'] = self.ttl_value

    def add_wait(self, *, cycle):
        self._finalize_actions()
        self.queue.append({'wait': {'cycle': cycle, 'is0': cycle == 0},
                           'actions': self.actions})
        self.actions = {}

        self.checker_queue.append({'wait': {'cycle': cycle},
                                   'actions': self.checker_actions})
        self.checker_actions = {}

    def add_wait_trig(self, *, chn, edge, cycle):
        self._finalize_actions()
        self.queue.append({'wait_trig': {'chn': chn, 'edge': edge, 'cycle': cycle},
                           'actions': self.actions})
        self.actions = {}

        self.checker_queue.append({'wait_trig': {'chn': chn, 'edge': edge, 'cycle': cycle},
                                   'actions': self.checker_actions})
        self.checker_actions = {}

    def add_ttl_set4(self, *, bank4_1, val1):
        ttl_action = self.actions.get('ttl', {'mask': 0, 'val': 0})
        self.actions['ttl'] = ttl_action
        mask, val = _set_ttl(0, 0, bank4_1, val1, 4)
        ttl_action['mask'] |= (mask & self.ttl_mask)
        ttl_action['val'] |= (val & self.ttl_mask)

    def add_clockout(self, *, period):
        self.actions['clockout'] = {'period': period}
        self.checker_actions['clockout'] = {'div': period}

    def add_ttl_set16(self, *, bank8_1, val1, bank8_2, val2):
        ttl_action = self.actions.get('ttl', {'mask': 0, 'val': 0})
        self.actions['ttl'] = ttl_action
        mask, val = _set_ttl(0, 0, bank8_1, val1, 8)
        mask, val = _set_ttl(mask, val, bank8_2, val2, 8)
        ttl_action['mask'] |= (mask & self.ttl_mask)
        ttl_action['val'] |= (val & self.ttl_mask)

    def add_dds_set16(self, bus_id, dds_id, fud, addr, data):
        assert self.dds_write_adsu is not None
        name = 'dds0' if bus_id == 0 else 'dds1'
        self.actions[name] = dict(state=DDSFSMState.WR_ADSETUP2.value,
                                  id=dds_id, hold_cnt=self.dds_write_adsu,
                                  hold_end=self.dds_write_adsu == 0,
                                  read=0, reset=0, fud=fud,
                                  addr1=addr >> 1, data1=data)
        self.checker_actions[name] = dict(type='set1', id=dds_id + bus_id * 11,
                                          addr=addr, data=data, fud=fud)

    def add_ttl_set32(self, *, bank16_1, val1, bank16_2, val2):
        ttl_action = self.actions.get('ttl', {'mask': 0, 'val': 0})
        self.actions['ttl'] = ttl_action
        mask, val = _set_ttl(0, 0, bank16_1, val1, 16)
        mask, val = _set_ttl(mask, val, bank16_2, val2, 16)
        ttl_action['mask'] |= (mask & self.ttl_mask)
        ttl_action['val'] |= (val & self.ttl_mask)

    def add_dds_set32(self, bus_id, dds_id, fud, addr, data):
        assert self.dds_write_adsu is not None
        name = 'dds0' if bus_id == 0 else 'dds1'
        self.actions[name] = dict(state=DDSFSMState.WR_ADSETUP1.value,
                                  id=dds_id, hold_cnt=self.dds_write_adsu,
                                  hold_end=self.dds_write_adsu == 0,
                                  read=0, reset=0, fud=fud,
                                  addr1=addr >> 1, data1=data & 0xffff,
                                  addr2=(addr >> 1) | 1, data2=data >> 16)
        self.checker_actions[name] = dict(type='set2', id=dds_id + bus_id * 11,
                                          addr=addr, data=data, fud=fud)

    def add_dac(self, *, id, cycle, clk_pha, clk_pol, data):
        self.actions['dac'] = {'id': id, 'cycle': cycle, 'clk_pha': clk_pha,
                               'clk_pol': clk_pol, 'data': data}
        self.checker_actions['dac'] = {'id': id, 'div': cycle, 'nbits': 18,
                                       'pha': clk_pha, 'pol': clk_pol, 'data': data,
                                       'result': 0}

    def _rand_ttl_bank_val(self, banklen):
        maxbank = (56 + banklen - 1) // banklen
        bank = random.randint(0, maxbank - 1)
        bankmask = (1 << banklen) - 1
        val = random.randint(0, bankmask)
        val &= self.ttl_mask >> (bank * banklen)
        return bank, val

    def rand_wait1(self, *, min_cycle=0, max_cycle=2**12 - 1):
        inst, kws = rand_inst(wait1_inst, cycle=range(max(min_cycle, 0),
                                                      min(max_cycle + 1, 2**12)))
        self.add_wait(**kws)
        return inst

    def rand_ttl_set4(self):
        bank4_1, val1 = self._rand_ttl_bank_val(4)
        inst = ttl_set4_inst(bank4_1=bank4_1, val1=val1)
        self.add_ttl_set4(bank4_1=bank4_1, val1=val1)
        return inst

    def rand_clockout(self, *, min_div=0, max_div=2**9 - 1):
        inst, kws = rand_inst(clockout_inst, period=range(max(min_div, 0),
                                                          min(max_div + 1, 2**9)))
        self.add_clockout(**kws)
        return inst

    def rand_wait2(self, *, min_cycle=0, max_cycle=2**28 - 1):
        inst, kws = rand_inst(wait2_inst, cycle=range(max(min_cycle, 0),
                                                      min(max_cycle + 1, 2**28)))
        self.add_wait(**kws)
        return inst

    def rand_ttl_set16(self):
        bank8_1, val1 = self._rand_ttl_bank_val(8)
        bank8_2, val2 = self._rand_ttl_bank_val(8)
        inst = ttl_set16_inst(bank8_1=bank8_1, val1=val1, bank8_2=bank8_2, val2=val2)
        self.add_ttl_set16(bank8_1=bank8_1, val1=val1, bank8_2=bank8_2, val2=val2)
        return inst

    def rand_dds_set16(self, bus_id=range(1)):
        inst, kws = rand_inst(dds_set16_inst, bus_id=bus_id,
                              addr=range(0, 1<<6, 2), dds_id=range(11))
        self.add_dds_set16(**kws)
        return inst

    def rand_wait_trig(self, *, min_cycle=1, max_cycle=2**35 - 1, chn=range(255)):
        inst, kws = rand_inst(wait_trig_inst, chn=chn,
                              cycle=range(max(min_cycle, 1),
                                          min(max_cycle + 1, 2**35)))
        self.add_wait_trig(**kws)
        return inst

    def rand_ttl_set32(self):
        bank16_1, val1 = self._rand_ttl_bank_val(16)
        bank16_2, val2 = self._rand_ttl_bank_val(16)
        inst = ttl_set32_inst(bank16_1=bank16_1, val1=val1, bank16_2=bank16_2, val2=val2)
        self.add_ttl_set32(bank16_1=bank16_1, val1=val1, bank16_2=bank16_2, val2=val2)
        return inst

    def rand_dds_set32(self, bus_id=range(1)):
        inst, kws = rand_inst(dds_set32_inst, bus_id=bus_id,
                              addr=range(0, 1<<6, 4), dds_id=range(11))
        self.add_dds_set32(**kws)
        return inst

    def rand_dac(self):
        inst, kws = rand_inst(dac_inst, cycle=range(1))
        self.add_dac(**kws)
        return inst

    def check_action(self, hw_action):
        sw_action = self.queue.pop(0)
        if 'wait' in sw_action:
            assert 'wait_trig' not in sw_action
            assert not hw_action.is_trig
            _check_fields(hw_action.wait.wait, sw_action['wait'])
        else:
            assert 'wait_trig' in sw_action
            assert hw_action.is_trig
            _check_fields(hw_action.wait.wait_trig, sw_action['wait_trig'])
        sw_actions = sw_action['actions']
        hw_actions = hw_action.action
        for action_name in ('clockout', 'ttl', 'dds0', 'dds1', 'dac'):
            if action_name not in sw_actions:
                assert not getattr(hw_actions, f'{action_name}_en')
                continue
            assert getattr(hw_actions, f'{action_name}_en')
            _check_fields(getattr(hw_actions, action_name), sw_actions[action_name])


class TestParser(TestCaseWithSimulator):
    def test_wait(self):
        circ = ParserTester()
        state = ParserState()

        insts = []
        for _ in range(100):
            insts.append(random.choice((state.rand_wait1,
                                        state.rand_wait2,
                                        state.rand_wait_trig))())

        async def producer(sim):
            for inst in insts:
                assert (await circ.write.call_try(sim, inst=inst)) is not None

        async def consumer(sim):
            state.check_action(await circ.read.call(sim))
            while state.queue:
                req = await circ.read.call_try(sim)
                assert req is not None
                state.check_action(req)

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)


    @pytest.mark.parametrize("mask", [0, 0x7b4cce1354725, 0x1a0ad2cb302106,
                                      0x404f171c902e04, 0x482091b1d8378b,
                                      0x488491e87561f1, 0x637ddfd1c73aaa,
                                      0x90d759670f8b14, 0xa8b590f28436aa,
                                      0xbec2cd133ca9ef, 0xf05b02a8ca24b0,
                                      (1 << 56) - 1])
    def test_ttl(self, mask):
        circ = ParserTester()
        state = ParserState()
        state.ttl_mask = mask

        insts = []
        for _ in range(100):
            for _ in range(random.randint(0, 4)):
                insts.append(random.choice((state.rand_ttl_set4,
                                            state.rand_ttl_set16,
                                            state.rand_ttl_set32))())
            insts.append(random.choice((state.rand_wait1,
                                        state.rand_wait2,
                                        state.rand_wait_trig))())

        async def producer(sim):
            sim.set(circ.csr.dma_ttl_mask, mask)
            for inst in insts:
                assert (await circ.write.call_try(sim, inst=inst)) is not None

        async def consumer(sim):
            while state.queue:
                state.check_action(await circ.read.call(sim))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)


    def test_clockout(self):
        circ = ParserTester()
        state = ParserState()

        insts = []
        for _ in range(100):
            for _ in range(random.randint(0, 4)):
                insts.append(state.rand_clockout())
            insts.append(random.choice((state.rand_wait1,
                                        state.rand_wait2,
                                        state.rand_wait_trig))())

        async def producer(sim):
            for inst in insts:
                assert (await circ.write.call_try(sim, inst=inst)) is not None

        async def consumer(sim):
            while state.queue:
                state.check_action(await circ.read.call(sim))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)


    def test_dds(self):
        circ = ParserTester()
        state = ParserState()
        state.dds_write_adsu = 7

        insts = []
        for _ in range(100):
            for _ in range(random.randint(0, 4)):
                insts.append(random.choice((state.rand_dds_set16,
                                            state.rand_dds_set32))())
            insts.append(random.choice((state.rand_wait1,
                                        state.rand_wait2,
                                        state.rand_wait_trig))())

        async def producer(sim):
            sim.set(circ.csr.dds_write_adsu, 7)
            for _ in range(3):
                await sim.tick()
            for inst in insts:
                assert (await circ.write.call_try(sim, inst=inst)) is not None

        async def consumer(sim):
            while state.queue:
                state.check_action(await circ.read.call(sim))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)


    def test_dac(self):
        circ = ParserTester()
        state = ParserState()

        insts = []
        for _ in range(100):
            for _ in range(random.randint(0, 2)):
                insts.append(state.rand_dac())
            insts.append(random.choice((state.rand_wait1,
                                        state.rand_wait2,
                                        state.rand_wait_trig))())

        async def producer(sim):
            for inst in insts:
                assert (await circ.write.call_try(sim, inst=inst)) is not None

        async def consumer(sim):
            while state.queue:
                state.check_action(await circ.read.call(sim))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)


    def test_rand(self):
        circ = ParserTester()
        state = ParserState()
        state.ttl_mask = (1 << 56) - 1
        state.dds_write_adsu = 7

        insts = []
        for _ in range(300):
            insts.append(random.choice((state.rand_wait1,
                                        state.rand_wait2,
                                        state.rand_wait_trig,
                                        state.rand_ttl_set4,
                                        state.rand_ttl_set16,
                                        state.rand_ttl_set32,
                                        state.rand_clockout,
                                        state.rand_dds_set16,
                                        state.rand_dds_set32,
                                        state.rand_dac))())
        insts.append(state.rand_wait1())

        async def producer(sim):
            sim.set(circ.csr.dds_write_adsu, 7)
            sim.set(circ.csr.dma_ttl_mask, (1 << 56) - 1)
            for _ in range(3):
                await sim.tick()
            for inst in insts:
                assert (await circ.write.call_try(sim, inst=inst)) is not None

        async def consumer(sim):
            while state.queue:
                state.check_action(await circ.read.call(sim))

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)


def config(*, spi=False, clock_shift=1):
    if spi:
        kws = dict(SPI_MOSI=sma_pin(1, 1),
                   SPI_MISO=sma_pin(1, 2),
                   SPI_SCLK=sma_pin(1, 3),
                   SPI_CS=sma_pin(1, 4))
    else:
        kws = dict()
    return Config(TTLIN=' '.join(sma_pin(0, i) for i in range(4)),
                  **kws, CLOCK_SHIFT=clock_shift)


class RunnerTester(Elaboratable, TTLChecker, ClockoutChecker, DDSChecker, SPIChecker):
    def __init__(self, conf):
        axi = AXI3(64, 16, 3).create()
        self.pulseio = PulseIO.from_config(None, conf)
        self.csr = Registers(conf)
        self.fifos = Fifos(32, dma_addr_width=len(axi.ARADDR))
        self.ioctrl = IOController(self.pulseio, self.csr, self.fifos,
                                   clock_shift=conf.CLOCK_SHIFT)
        self.dmactrl = DMAController(axi, self.csr, self.fifos)
        self.runner = DMAInstRunner(self.pulseio, self.csr, self.ioctrl, self.dmactrl)

        self.write = _TestbenchIO(AdapterTrans.create(self.runner.write))

        TTLChecker.__init__(self, self.pulseio, self.csr)
        ClockoutChecker.__init__(self, self.pulseio, self.csr, conf.CLOCK_SHIFT)
        DDSChecker.__init__(self, self.pulseio, self.csr,
                            (self.ioctrl.dds0, self.ioctrl.dds1))
        SPIChecker.__init__(self, self.pulseio, self.csr, self.ioctrl.spi)

    def elaborate(self, plat):
        m = TModule()

        m.submodules.pulseio = self.pulseio
        m.submodules.csr = self.csr
        m.submodules.fifos = self.fifos
        m.submodules.ioctrl = self.ioctrl
        m.submodules.dmactrl = self.dmactrl
        m.submodules.runner = self.runner

        m.submodules.write = self.write

        return m

    def add_testbenches(self, sim):
        sim.add_testbench(self.check_ttl, background=True)
        sim.add_testbench(self.check_clockout, background=True)
        sim.add_testbench(self.check_dds0, background=True)
        sim.add_testbench(self.check_dds1, background=True)
        sim.add_testbench(self.check_spi, background=True)

RUNNER_LATENCY = 1

class TestRunner(TestCaseWithSimulator):
    def test_ttl(self):
        circ = RunnerTester(config(clock_shift=1))
        state = ParserState()
        state.ttl_mask = 0xff_ffff_ffff_ffff

        for _ in range(40):
            for _ in range(random.randint(0, 4)):
                random.choice((state.rand_ttl_set4, state.rand_ttl_set16,
                               state.rand_ttl_set32))()
            random.choice((state.rand_wait1,
                           state.rand_wait2))(max_cycle=10)

        async def producer(sim):
            for cmd in state.queue:
                await circ.write.call(sim, **state.to_parsed_args(cmd))

        async def consumer(sim):
            for _ in range(RUNNER_LATENCY + 1):
                await sim.tick()
            for cmd in state.checker_queue:
                await state.run_checker_action(cmd, sim, circ)

        async def runstate_check(sim):
            for _ in range(RUNNER_LATENCY + 2):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()
            for cmd in state.checker_queue:
                for _ in range(cmd['wait']['cycle'] + 1):
                    assert sim.get(circ.csr.dma_status) == 9 << 8
                    await sim.tick()
            for _ in range(50):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            sim.add_testbench(runstate_check)
            circ.add_testbenches(sim)

    def test_ttl_ovr(self):
        circ = RunnerTester(config(clock_shift=1))
        state = ParserState()
        state.ttl_mask = 0xff_ffff_ffff_ffff

        for _ in range(40):
            for _ in range(random.randint(0, 4)):
                random.choice((state.rand_ttl_set4, state.rand_ttl_set16,
                               state.rand_ttl_set32))()
            random.choice((state.rand_wait1,
                           state.rand_wait2))(max_cycle=10)

        async def producer(sim):
            for cmd in state.queue:
                await circ.write.call(sim, **state.to_parsed_args(cmd))

        async def consumer(sim):
            for _ in range(RUNNER_LATENCY + 1):
                await sim.tick()
            for cmd in state.checker_queue:
                await state.run_checker_action(cmd, sim, circ)

        async def ttl_ovr_check(sim):
            while True:
                for _ in range(random.randint(1, 10)):
                    await sim.tick()
                lo = random.randint(0, 0xff_ffff_ffff_ffff)
                hi = random.randint(0, 0xff_ffff_ffff_ffff) & ~lo
                sim.set(circ.csr.ttl_lo_mask, lo)
                sim.set(circ.csr.ttl_hi_mask, hi)
                await sim.tick()
                circ.ttl_set_ovr(lo, hi)

        async def runstate_check(sim):
            for _ in range(RUNNER_LATENCY + 2):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()
            for cmd in state.checker_queue:
                for _ in range(cmd['wait']['cycle'] + 1):
                    assert sim.get(circ.csr.dma_status) == 9 << 8
                    await sim.tick()
            for _ in range(50):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            sim.add_testbench(ttl_ovr_check, background=True)
            sim.add_testbench(runstate_check)
            circ.add_testbenches(sim)

    def test_clockout(self):
        circ = RunnerTester(config(clock_shift=1))
        state = ParserState()
        state.ttl_mask = 0xff_ffff_ffff_ffff

        for _ in range(10):
            for _ in range(random.randint(0, 2)):
                state.rand_clockout(max_div=20)
            random.choice((state.rand_wait1,
                           state.rand_wait2))(min_cycle=60, max_cycle=200)

        async def producer(sim):
            for cmd in state.queue:
                await circ.write.call(sim, **state.to_parsed_args(cmd))

        async def consumer(sim):
            for _ in range(RUNNER_LATENCY + 1):
                await sim.tick()
            for cmd in state.checker_queue:
                await state.run_checker_action(cmd, sim, circ)

        async def runstate_check(sim):
            for _ in range(RUNNER_LATENCY + 2):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()
            for cmd in state.checker_queue:
                for _ in range(cmd['wait']['cycle'] + 1):
                    assert sim.get(circ.csr.dma_status) == 9 << 8
                    await sim.tick()
            for _ in range(50):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            sim.add_testbench(runstate_check)
            circ.add_testbenches(sim)

    def test_underflow(self):
        circ = RunnerTester(config(clock_shift=1))
        state = ParserState()
        state.ttl_mask = 0xff_ffff_ffff_ffff

        nt1 = 10
        nt2 = 3

        for _ in range(nt1 + nt2):
            random.choice((state.rand_wait1,
                           state.rand_wait2))(max_cycle=10)

        async def producer(sim):
            for i in range(nt1):
                cmd = state.queue[i]
                await circ.write.call(sim, **state.to_parsed_args(cmd))
                for _ in range(cmd['wait']['cycle']):
                    await sim.tick()
            await sim.tick()
            for i in range(nt2):
                cmd = state.queue[i + nt1]
                await circ.write.call(sim, **state.to_parsed_args(cmd))
                for _ in range(cmd['wait']['cycle']):
                    await sim.tick()

        async def status_checker(sim):
            for _ in range(RUNNER_LATENCY + 2):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()

            for i in range(nt1):
                cmd = state.queue[i]
                for _ in range(cmd['wait']['cycle'] + 1):
                    assert sim.get(circ.csr.dma_status) == 9 << 8
                    await sim.tick()
            assert sim.get(circ.csr.dma_status) == 8 << 8
            await sim.tick()
            for i in range(nt2):
                cmd = state.queue[i + nt1]
                for _ in range(cmd['wait']['cycle'] + 1):
                    assert sim.get(circ.csr.dma_status) == 11 << 8
                    await sim.tick()
            for _ in range(50):
                assert sim.get(circ.csr.dma_status) == 10 << 8
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(status_checker)
            circ.add_testbenches(sim)

    def test_long_wait(self):
        circ = RunnerTester(config(clock_shift=1))
        state = ParserState()
        state.dds_write_adsu = circ.csr.dds_write_adsu.init
        state.ttl_mask = 0xff_ffff_ffff_ffff

        times = range(125, 135)

        for t in times:
            random.choice((state.rand_wait1,
                           state.rand_wait2))(min_cycle=t, max_cycle=t)

        async def producer(sim):
            for cmd in state.queue:
                await circ.write.call(sim, **state.to_parsed_args(cmd))

        async def status_checker(sim):
            for _ in range(RUNNER_LATENCY + 2):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()
            for t in times:
                for _ in range(t + 1):
                    assert sim.get(circ.csr.dma_status) == 9 << 8
                    await sim.tick()
            assert sim.get(circ.csr.dma_status) == 8 << 8

        async def longwait_checker(sim):
            for _ in range(RUNNER_LATENCY):
                await sim.tick()
            has_long_wait = False
            for t in times:
                assert sim.get(circ.runner.long_wait) == 0
                await sim.tick()
                for i in range(t):
                    if t - i > 128:
                        assert sim.get(circ.runner.long_wait) == 1
                        has_long_wait = True
                    else:
                        assert sim.get(circ.runner.long_wait) == 0
                    await sim.tick()
            assert has_long_wait

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(status_checker)
            sim.add_testbench(longwait_checker)
            circ.add_testbenches(sim)

    def test_long_wait2(self):
        circ = RunnerTester(config(clock_shift=1))
        state = ParserState()
        state.dds_write_adsu = circ.csr.dds_write_adsu.init
        state.ttl_mask = 0xff_ffff_ffff_ffff

        times = range(125, 135)

        for t in times:
            random.choice((state.rand_dds_set16,
                           state.rand_dds_set32))(bus_id=(0,))
            random.choice((state.rand_dds_set16,
                           state.rand_dds_set32))(bus_id=(1,))
            state.rand_dac()
            random.choice((state.rand_wait1,
                           state.rand_wait2))(min_cycle=t, max_cycle=t)

        async def producer(sim):
            for cmd in state.queue:
                await circ.write.call(sim, **state.to_parsed_args(cmd))

        async def consumer(sim):
            for _ in range(RUNNER_LATENCY + 1):
                await sim.tick()
            for cmd in state.checker_queue:
                await state.run_checker_action(cmd, sim, circ)

        async def status_checker(sim):
            for _ in range(RUNNER_LATENCY + 2):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()
            for t in times:
                for _ in range(t + 1):
                    assert sim.get(circ.csr.dma_status) == 9 << 8
                    await sim.tick()
            assert sim.get(circ.csr.dma_status) == 8 << 8

        async def longwait_checker(sim):
            for _ in range(RUNNER_LATENCY):
                await sim.tick()
            has_long_wait = False
            for t in times:
                assert sim.get(circ.runner.long_wait) == 0
                assert sim.get(circ.ioctrl.dds0.busy) == 0
                assert sim.get(circ.ioctrl.dds1.busy) == 0
                assert sim.get(circ.ioctrl.spi.busy) == 0
                await sim.tick()
                for i in range(t):
                    if i < 10:
                        assert sim.get(circ.ioctrl.dds0.busy) == 1
                        assert sim.get(circ.ioctrl.dds1.busy) == 1
                        assert sim.get(circ.ioctrl.spi.busy) == 1
                    if t - i > 128:
                        assert sim.get(circ.runner.long_wait) == 1
                        has_long_wait = True
                    else:
                        assert sim.get(circ.runner.long_wait) == 0
                    await sim.tick()
            assert has_long_wait

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            sim.add_testbench(status_checker)
            sim.add_testbench(longwait_checker)
            circ.add_testbenches(sim)

    def test_dds(self):
        circ = RunnerTester(config(clock_shift=1))
        state = ParserState()
        state.dds_write_adsu = circ.csr.dds_write_adsu.init
        state.ttl_mask = 0xff_ffff_ffff_ffff

        for _ in range(20):
            for _ in range(random.randint(0, 4)):
                random.choice((state.rand_dds_set16,
                               state.rand_dds_set32))()
            random.choice((state.rand_wait1,
                           state.rand_wait2))(min_cycle=80, max_cycle=120)

        async def producer(sim):
            for cmd in state.queue:
                await circ.write.call(sim, **state.to_parsed_args(cmd))

        async def consumer(sim):
            for _ in range(RUNNER_LATENCY + 1):
                await sim.tick()
            for cmd in state.checker_queue:
                await state.run_checker_action(cmd, sim, circ)

        async def runstate_check(sim):
            for _ in range(RUNNER_LATENCY + 2):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()
            for cmd in state.checker_queue:
                for _ in range(cmd['wait']['cycle'] + 1):
                    assert sim.get(circ.csr.dma_status) == 9 << 8
                    await sim.tick()
            for _ in range(50):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            sim.add_testbench(runstate_check)
            circ.add_testbenches(sim)

    def test_dac(self):
        circ = RunnerTester(config(clock_shift=1))
        state = ParserState()
        state.dds_write_adsu = circ.csr.dds_write_adsu.init
        state.ttl_mask = 0xff_ffff_ffff_ffff

        for _ in range(20):
            for _ in range(random.randint(0, 1)):
                state.rand_dac()
            random.choice((state.rand_wait1,
                           state.rand_wait2))(min_cycle=90, max_cycle=120)

        async def producer(sim):
            for cmd in state.queue:
                await circ.write.call(sim, **state.to_parsed_args(cmd))

        async def consumer(sim):
            for _ in range(RUNNER_LATENCY + 1):
                await sim.tick()
            for cmd in state.checker_queue:
                await state.run_checker_action(cmd, sim, circ)

        async def runstate_check(sim):
            for _ in range(RUNNER_LATENCY + 2):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()
            for cmd in state.checker_queue:
                for _ in range(cmd['wait']['cycle'] + 1):
                    assert sim.get(circ.csr.dma_status) == 9 << 8
                    await sim.tick()
            for _ in range(50):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            sim.add_testbench(runstate_check)
            circ.add_testbenches(sim)

    def test_mixed(self):
        circ = RunnerTester(config(clock_shift=1))
        state = ParserState()
        state.dds_write_adsu = circ.csr.dds_write_adsu.init
        state.ttl_mask = 0xff_ffff_ffff_ffff

        for _ in range(25):
            for _ in range(random.randint(0, 9)):
                random.choice((state.rand_ttl_set4,
                               state.rand_ttl_set16,
                               state.rand_ttl_set32,
                               lambda: state.rand_clockout(max_div=40),
                               state.rand_dds_set16,
                               state.rand_dds_set32,
                               state.rand_dac))()
            random.choice((state.rand_wait1,
                           state.rand_wait2))(min_cycle=90, max_cycle=120)

        async def producer(sim):
            for cmd in state.queue:
                await circ.write.call(sim, **state.to_parsed_args(cmd))

        async def consumer(sim):
            for _ in range(RUNNER_LATENCY + 1):
                await sim.tick()
            for cmd in state.checker_queue:
                await state.run_checker_action(cmd, sim, circ)

        async def runstate_check(sim):
            for _ in range(RUNNER_LATENCY + 2):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()
            for cmd in state.checker_queue:
                for _ in range(cmd['wait']['cycle'] + 1):
                    assert sim.get(circ.csr.dma_status) == 9 << 8
                    await sim.tick()
            for _ in range(50):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            sim.add_testbench(runstate_check)
            circ.add_testbenches(sim)

    def test_trigger_timeout(self):
        circ = RunnerTester(config(clock_shift=1))
        state = ParserState()
        state.dds_write_adsu = circ.csr.dds_write_adsu.init
        state.ttl_mask = 0xff_ffff_ffff_ffff

        for _ in range(10):
            for _ in range(random.randint(0, 3)):
                random.choice((state.rand_ttl_set4,
                               state.rand_ttl_set16,
                               state.rand_ttl_set32))()
            state.rand_wait_trig(min_cycle=1, max_cycle=10)

        async def producer(sim):
            for cmd in state.queue:
                await circ.write.call(sim, **state.to_parsed_args(cmd))

        async def consumer(sim):
            for _ in range(RUNNER_LATENCY + 1):
                await sim.tick()
            for cmd in state.checker_queue:
                wait_trig = await state.run_checker_action(cmd, sim, circ)
                assert wait_trig is not None
                for _ in range(wait_trig['cycle'] + 3):
                    await sim.tick()

        async def runstate_check(sim):
            for _ in range(RUNNER_LATENCY + 2):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()
            isfirst = True
            for cmd in state.checker_queue:
                status = 9 << 8 if isfirst else 13 << 8
                isfirst = False
                for _ in range(cmd['wait_trig']['cycle'] + 2):
                    assert sim.get(circ.csr.dma_status) == status
                    await sim.tick()
                assert sim.get(circ.csr.dma_status) == 13 << 8
                await sim.tick()
            for _ in range(50):
                assert sim.get(circ.csr.dma_status) == 12 << 8
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            sim.add_testbench(runstate_check)
            circ.add_testbenches(sim)

    def test_trigger_fire(self):
        circ = RunnerTester(config(clock_shift=1))
        state = ParserState()
        state.dds_write_adsu = circ.csr.dds_write_adsu.init
        state.ttl_mask = 0xff_ffff_ffff_ffff

        trigger_times = []

        for _ in range(10):
            for _ in range(random.randint(0, 3)):
                random.choice((state.rand_ttl_set4,
                               state.rand_ttl_set16,
                               state.rand_ttl_set32))()
            trigger_time = random.randint(5, 10)
            trigger_times.append(trigger_time)
            state.rand_wait_trig(min_cycle=trigger_time + 10,
                                 max_cycle=trigger_time + 15,
                                 chn=range(4))

        async def producer(sim):
            for cmd in state.queue:
                await circ.write.call(sim, **state.to_parsed_args(cmd))

        async def consumer(sim):
            for _ in range(RUNNER_LATENCY + 1):
                await sim.tick()
            for trig_time, cmd in zip(trigger_times, state.checker_queue):
                wait_trig = await state.run_checker_action(cmd, sim, circ)
                assert wait_trig is not None
                trig_chn = circ.pulseio.ttlin_port.i[wait_trig['chn']]
                edge = wait_trig['edge']
                sim.set(trig_chn, ~edge)
                for _ in range(trig_time):
                    await sim.tick()
                sim.set(trig_chn, edge)
                for _ in range(6):
                    await sim.tick()

        async def runstate_check(sim):
            for _ in range(RUNNER_LATENCY + 2):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()
            for trig_time in trigger_times:
                for _ in range(trig_time + 6):
                    assert sim.get(circ.csr.dma_status) == 9 << 8
                    await sim.tick()
            for _ in range(50):
                assert sim.get(circ.csr.dma_status) == 8 << 8
                await sim.tick()

        with self.run_simulation(circ) as sim:
            sim.add_testbench(producer)
            sim.add_testbench(consumer)
            sim.add_testbench(runstate_check)
            circ.add_testbenches(sim)
