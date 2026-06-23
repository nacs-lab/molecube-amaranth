#

from amaranth import *

from transactron import TModule
from transactron.testing import TestCaseWithSimulator, TestbenchIO as _TestbenchIO, CallTrigger

from transactron.lib.adapters import AdapterTrans

from molecube_amaranth.config import Config
from molecube_amaranth.io import PulseIO, sma_pin
from molecube_amaranth.trigger import TriggerController

import random

def config():
    return Config(TTLIN=' '.join(sma_pin(0, i) for i in range(4)))

class TrigCtrlWrapper(Elaboratable):
    def __init__(self):
        conf = config()
        self.pulseio = PulseIO.from_config(None, conf)
        self.ttlin_port = self.pulseio.ttlin_port
        self.trig = TriggerController(self.pulseio.ttlin, 10)
        self.setup = _TestbenchIO(AdapterTrans.create(self.trig.setup))
        self.wait = _TestbenchIO(AdapterTrans.create(self.trig.wait))

    def rand_ttl(self, sim, keeps=None, flips=None, zeros=None, ones=None):
        def iter_chns(chns):
            if chns is None:
                return ()
            if isinstance(chns, int):
                return (chns,)
            else:
                return chns
        nttls = len(self.ttlin_port.i)
        cur_val = sim.get(self.ttlin_port.i)
        val = 0
        mask = 0
        for keep in iter_chns(keeps):
            val |= cur_val & (1 << keep)
            mask |= 1 << keep
        for flip in iter_chns(flips):
            val |= (~cur_val) & (1 << flip)
            mask |= 1 << flip
        for zero in iter_chns(zeros):
            mask |= 1 << zero
        for one in iter_chns(ones):
            val |= 1 << one
            mask |= 1 << one
        full_mask = (1 << nttls) - 1
        if mask != full_mask:
            val = val | (random.randint(0, full_mask) & ~mask)
        sim.set(self.ttlin_port.i, val)

    def elaborate(self, plat):
        m = TModule()

        m.submodules.pulseio = self.pulseio
        m.submodules.trig = self.trig
        m.submodules.setup = self.setup
        m.submodules.wait = self.wait

        return m

class TestTrigCtrl(TestCaseWithSimulator):
    def test_idle(self):
        circ = TrigCtrlWrapper()

        async def f(sim):
            for _ in range(100):
                assert (await circ.wait.call_try(sim)) is None

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    def test_trigger_timeout1(self):
        circ = TrigCtrlWrapper()

        async def f(sim):
            # Cycle == 1 is the smallest supported argument value
            chn = random.randint(0, 3)
            edge = random.randint(0, 1)
            circ.rand_ttl(sim, flips=chn)
            await circ.setup.call(sim, chn=chn, edge=edge, cycle=1)
            for _ in range(1 + 1):
                circ.rand_ttl(sim, keeps=chn)
                assert (await circ.wait.call_try(sim)) is None
            for cycle in range(2, 100):
                chn = random.randint(0, 3)
                edge = random.randint(0, 1)
                circ.rand_ttl(sim, flips=chn)
                setup, wait = await CallTrigger(sim).call(circ.setup, chn=chn, edge=edge,
                                                          cycle=cycle).call(circ.wait)
                assert setup is not None
                assert wait is not None
                assert wait.timeout
                if random.randint(0, 1):
                    kws = dict(keeps=chn)
                elif edge:
                    kws = dict(zeros=chn)
                else:
                    kws = dict(ones=chn)
                for _ in range(cycle + 1):
                    circ.rand_ttl(sim, **kws)
                    assert (await circ.wait.call_try(sim)) is None
            wait = await circ.wait.call_try(sim)
            assert wait is not None
            assert wait.timeout

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    def test_trigger_timeout2(self):
        circ = TrigCtrlWrapper()

        async def f(sim):
            # Cycle == 1 is the smallest supported argument value
            for cycle in range(1, 100):
                chn = random.randint(0, 3)
                edge = random.randint(0, 1)
                circ.rand_ttl(sim, flips=chn)
                setup = await circ.setup.call_try(sim, chn=chn, edge=edge, cycle=cycle)
                assert setup is not None
                if random.randint(0, 1):
                    kws = dict(keeps=chn)
                elif edge:
                    kws = dict(zeros=chn)
                else:
                    kws = dict(ones=chn)
                for _ in range(cycle + 1):
                    circ.rand_ttl(sim, **kws)
                    assert (await circ.wait.call_try(sim)) is None

                wait = await circ.wait.call_try(sim)
                assert wait is not None
                assert wait.timeout

                for _ in range(random.randint(0, 10)):
                    circ.rand_ttl(sim)
                    assert (await circ.wait.call_try(sim)) is None

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    def test_trigger_first(self):
        circ = TrigCtrlWrapper()

        async def f(sim):
            for cycle in range(2, 100):
                chn = random.randint(0, 3)
                edge = random.randint(0, 1)
                if edge:
                    circ.rand_ttl(sim, zeros=chn)
                else:
                    circ.rand_ttl(sim, ones=chn)
                setup = await circ.setup.call_try(sim, chn=chn, edge=edge, cycle=cycle)
                assert setup is not None
                circ.rand_ttl(sim, flips=chn)
                assert (await circ.wait.call_try(sim)) is None
                for _ in range(2):
                    circ.rand_ttl(sim)
                    assert (await circ.wait.call_try(sim)) is None
                circ.rand_ttl(sim)
                wait = await circ.wait.call_try(sim)
                assert wait is not None
                assert not wait.timeout

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)

    def test_trigger_last(self):
        circ = TrigCtrlWrapper()

        async def f(sim):
            for cycle in range(3, 100):
                chn = random.randint(0, 3)
                edge = random.randint(0, 1)
                if edge:
                    circ.rand_ttl(sim, ones=chn)
                else:
                    circ.rand_ttl(sim, zeros=chn)
                setup = await circ.setup.call_try(sim, chn=chn, edge=edge, cycle=cycle)
                assert setup is not None
                arm_cycle = random.randint(0, cycle - 3)
                for _ in range(arm_cycle):
                    circ.rand_ttl(sim, keeps=chn)
                    assert (await circ.wait.call_try(sim)) is None
                circ.rand_ttl(sim, flips=chn)
                assert (await circ.wait.call_try(sim)) is None
                for _ in range(cycle - 3 - arm_cycle):
                    circ.rand_ttl(sim, keeps=chn)
                    assert (await circ.wait.call_try(sim)) is None
                circ.rand_ttl(sim, flips=chn)
                assert (await circ.wait.call_try(sim)) is None
                for _ in range(2):
                    circ.rand_ttl(sim)
                    assert (await circ.wait.call_try(sim)) is None
                circ.rand_ttl(sim)
                wait = await circ.wait.call_try(sim)
                assert wait is not None
                assert not wait.timeout

        with self.run_simulation(circ) as sim:
            sim.add_testbench(f)
