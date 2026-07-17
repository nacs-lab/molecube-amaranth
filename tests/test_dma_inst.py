#

from amaranth import *
from amaranth.lib import io

from transactron import TModule
from transactron.testing import TestCaseWithSimulator, TestbenchIO as _TestbenchIO
from transactron.lib.adapters import AdapterTrans

from molecube_amaranth.csr import Registers
from molecube_amaranth.config import Config
from molecube_amaranth.dds import FSMState as DDSFSMState
from molecube_amaranth.dma_inst import DMAInstParser

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
    def __init__(self):
        self.actions = {}
        self.queue = []
        self.dds_write_adsu = None
        self.ttl_mask = 0

    def add_wait(self, *, cycle):
        self.queue.append({'wait': {'cycle': cycle, 'is0': cycle == 0},
                           'actions': self.actions})
        self.actions = {}

    def add_wait_trig(self, *, chn, edge, cycle):
        self.queue.append({'wait_trig': {'chn': chn, 'edge': edge, 'cycle': cycle},
                           'actions': self.actions})
        self.actions = {}

    def add_ttl_set4(self, *, bank4_1, val1):
        ttl_action = self.actions.get('ttl', {'mask': 0, 'val': 0})
        self.actions['ttl'] = ttl_action
        mask, val = _set_ttl(0, 0, bank4_1, val1, 4)
        ttl_action['mask'] |= (mask & self.ttl_mask)
        ttl_action['val'] |= (val & self.ttl_mask)

    def add_clockout(self, *, period):
        self.actions['clockout'] = {'period': period}

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

    def add_dac(self, *, id, cycle, clk_pha, clk_pol, data):
        self.actions['dac'] = {'id': id, 'cycle': cycle, 'clk_pha': clk_pha,
                               'clk_pol': clk_pol, 'data': data}

    def _rand_ttl_bank_val(self, banklen):
        maxbank = (56 + banklen - 1) // banklen
        bank = random.randint(0, maxbank - 1)
        bankmask = (1 << banklen) - 1
        val = random.randint(0, bankmask)
        val &= self.ttl_mask >> (bank * banklen)
        return bank, val

    def rand_wait1(self):
        inst, kws = rand_inst(wait1_inst)
        self.add_wait(**kws)
        return inst

    def rand_ttl_set4(self):
        bank4_1, val1 = self._rand_ttl_bank_val(4)
        inst = ttl_set4_inst(bank4_1=bank4_1, val1=val1)
        self.add_ttl_set4(bank4_1=bank4_1, val1=val1)
        return inst

    def rand_clockout(self):
        inst, kws = rand_inst(clockout_inst)
        self.add_clockout(**kws)
        return inst

    def rand_wait2(self):
        inst, kws = rand_inst(wait2_inst)
        self.add_wait(**kws)
        return inst

    def rand_ttl_set16(self):
        bank8_1, val1 = self._rand_ttl_bank_val(8)
        bank8_2, val2 = self._rand_ttl_bank_val(8)
        inst = ttl_set16_inst(bank8_1=bank8_1, val1=val1, bank8_2=bank8_2, val2=val2)
        self.add_ttl_set16(bank8_1=bank8_1, val1=val1, bank8_2=bank8_2, val2=val2)
        return inst

    def rand_dds_set16(self):
        inst, kws = rand_inst(dds_set16_inst, addr=range(0, 1<<6, 2), dds_id=range(11))
        self.add_dds_set16(**kws)
        return inst

    def rand_wait_trig(self):
        inst, kws = rand_inst(wait_trig_inst)
        self.add_wait_trig(**kws)
        return inst

    def rand_ttl_set32(self):
        bank16_1, val1 = self._rand_ttl_bank_val(16)
        bank16_2, val2 = self._rand_ttl_bank_val(16)
        inst = ttl_set32_inst(bank16_1=bank16_1, val1=val1, bank16_2=bank16_2, val2=val2)
        self.add_ttl_set32(bank16_1=bank16_1, val1=val1, bank16_2=bank16_2, val2=val2)
        return inst

    def rand_dds_set32(self):
        inst, kws = rand_inst(dds_set32_inst, addr=range(0, 1<<6, 4), dds_id=range(11))
        self.add_dds_set32(**kws)
        return inst

    def rand_dac(self):
        inst, kws = rand_inst(dac_inst)
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
