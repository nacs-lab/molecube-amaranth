#

def get_bits(data, nbits):
    for bit in range(nbits):
        yield (data >> (nbits - bit - 1)) & 1


class TTLChecker:
    def __init__(self, pulseio, csr):
        self.__ttl = 0
        self.__ttl_hi = 0
        self.__ttl_lo = 0
        self.__pulseio = pulseio
        self.__csr = csr

    def ttl_set(self, ttl):
        self.__ttl = ttl

    def ttl_set_ovr(self, lo, hi):
        self.__ttl_lo = lo
        self.__ttl_hi = hi

    async def check_ttl(self, sim):
        ttlout_port = self.__pulseio.ttlout_port
        ttlout_reg = self.__csr.ttl_out
        while True:
            # Make sure we see the command added by user coroutine
            await sim.delay(0)
            assert sim.get(ttlout_reg) == self.__ttl
            assert sim.get(ttlout_port.o) == (self.__ttl | self.__ttl_hi) & ~self.__ttl_lo
            await sim.tick()


class ClockoutChecker:
    def __init__(self, pulseio, csr, clock_shift):
        self.__new_clockout = False
        self.__clockout_off = (256 << clock_shift) - 1
        self.__clockout_div = self.__clockout_off
        self.__clock_shift = clock_shift
        self.__pulseio = pulseio
        self.__csr = csr

    def clockout_set(self, div):
        self.__new_clockout = True
        self.__clockout_div = ((div + 1) << self.__clock_shift) - 1

    def clockout_set_shifted(self, div):
        self.__new_clockout = True
        self.__clockout_div = div

    async def __check_clockout_cycle(self, sim, clockout_port):
        self.__new_clockout = False
        for _ in range(self.__clockout_div + 1):
            # Make sure we see the command added by user coroutine
            await sim.delay(0)
            if self.__new_clockout:
                return
            assert sim.get(clockout_port.o) == 0
            await sim.tick()
        for _ in range(self.__clockout_div + 1):
            # Make sure we see the command added by user coroutine
            await sim.delay(0)
            if self.__new_clockout:
                  return
            assert sim.get(clockout_port.o) == 1
            await sim.tick()

    async def check_clockout(self, sim):
        clockout_port = self.__pulseio.clockout_port
        while True:
            # Make sure we see the command added by user coroutine
            await sim.delay(0)
            assert sim.get(self.__csr.clockout_div) == (self.__clockout_div >> self.__clock_shift)
            if self.__clockout_div == self.__clockout_off:
                assert sim.get(clockout_port.o) == 0
                await sim.tick()
            else:
                await self.__check_clockout_cycle(sim, clockout_port)


class DDSChecker:
    def __init__(self, pulseio, csr):
        self.__dds_cmd = None
        self.__pulseio = pulseio
        self.__csr = csr

    def dds_set_freq(self, id, freq):
        self.__dds_cmd = dict(cmd='set2', id=id, addr1=0x2d, data1=freq & 0xffff,
                             addr2=0x2f, data2=freq >> 16)

    def dds_set_amp_phase(self, id, amp, phase):
        self.__dds_cmd = dict(cmd='set2', id=id, addr1=0x33, data1=amp,
                             addr2=0x31, data2=phase)

    def dds_set_two_bytes(self, id, addr, data):
        self.__dds_cmd = dict(cmd='set1', id=id, addr1=addr + 1, data1=data)

    def dds_set_four_bytes(self, id, addr, data):
        self.__dds_cmd = dict(cmd='set2', id=id, addr1=addr + 1, data1=data & 0xffff,
                             addr2=addr + 3, data2=data >> 16)

    def dds_reset(self, id):
        self.__dds_cmd = dict(cmd='reset', id=id)

    def dds_get_two_bytes(self, id, addr, data):
        self.__dds_cmd = dict(cmd='get1', id=id, addr=addr + 1, data=data)

    def dds_get_four_bytes(self, id, addr, data):
        self.__dds_cmd = dict(cmd='get2', id=id, addr=addr + 1, data=data)

    def __get_dds_cmd(self, bank):
        if self.__dds_cmd is None:
            return
        cmd = self.__dds_cmd
        id = cmd['id']
        if bank == 0 and id < 11:
            self.__dds_cmd = None
            return cmd
        if bank == 1 and id >= 11:
            self.__dds_cmd = None
            cmd['id'] = id - 11
            return cmd

    async def __check_dds_cmd(self, sim, bank, port):
        # Make sure we see the command added by user coroutine
        await sim.delay(0)
        cmd = self.__get_dds_cmd(bank)
        if cmd is None:
            await self.idle(sim, port, 1)
            return
        op = cmd.pop('cmd')
        if op == 'set1':
            await self.set1(sim, self.__csr, port, **cmd)
        elif op == 'set2':
            await self.set2(sim, self.__csr, port, **cmd)
        elif op == 'reset':
            await self.reset(sim, self.__csr, port, **cmd)
        elif op == 'get1':
            await self.get1(sim, self.__csr, port, **cmd)
        elif op == 'get2':
            await self.get2(sim, self.__csr, port, **cmd)
        else:
            raise ValueError(f"Unknown DDS command {op}")

    async def check_dds(self, sim, bank):
        port = self.__pulseio.dds0_port if bank == 0 else self.__pulseio.dds1_port
        while True:
            await self.__check_dds_cmd(sim, bank, port)

    async def check_dds0(self, sim):
        await self.check_dds(sim, 0)

    async def check_dds1(self, sim):
        await self.check_dds(sim, 1)

    @staticmethod
    async def idle(sim, port, n=10):
        for _ in range(n):
            assert sim.get(port.addr.o) == 1
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == 0
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == (1 << 11) - 1
            await sim.tick()

    @staticmethod
    async def set1(sim, csr, port, *, id, addr1, data1):
        assert addr1 & 1 == 1
        t_adsu = sim.get(csr.dds_write_adsu) + 1
        t_wrlow = sim.get(csr.dds_write_wrlow) + 1
        t_adhd = sim.get(csr.dds_write_adhd) + 1
        t_fuddl = sim.get(csr.dds_write_fuddl) + 1
        t_fudhd = sim.get(csr.dds_write_fudhd) + 1

        cs = ((1 << 11) - 1) ^ (1 << id)

        for _ in range(t_adsu):
            assert sim.get(port.addr.o) == addr1
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == data1
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(t_wrlow):
            assert sim.get(port.addr.o) == addr1
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == data1
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 0
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(t_fuddl):
            assert sim.get(port.addr.o) == addr1
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == data1
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(t_fudhd):
            assert sim.get(port.addr.o) == addr1
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == data1
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 1
            assert sim.get(port.cs.o) == cs
            await sim.tick()

    @staticmethod
    async def set2(sim, csr, port, *, id, addr1, data1, addr2, data2):
        assert addr1 & 1 == 1
        assert addr2 & 1 == 1
        t_adsu = sim.get(csr.dds_write_adsu) + 1
        t_wrlow = sim.get(csr.dds_write_wrlow) + 1
        t_adhd = sim.get(csr.dds_write_adhd) + 1
        t_fuddl = sim.get(csr.dds_write_fuddl) + 1
        t_fudhd = sim.get(csr.dds_write_fudhd) + 1

        cs = ((1 << 11) - 1) ^ (1 << id)

        for _ in range(t_adsu):
            assert sim.get(port.addr.o) == addr1
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == data1
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(t_wrlow):
            assert sim.get(port.addr.o) == addr1
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == data1
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 0
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(t_adhd):
            assert sim.get(port.addr.o) == addr1
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == data1
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(t_adsu):
            assert sim.get(port.addr.o) == addr2
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == data2
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(t_wrlow):
            assert sim.get(port.addr.o) == addr2
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == data2
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 0
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(t_fuddl):
            assert sim.get(port.addr.o) == addr2
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == data2
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(t_fudhd):
            assert sim.get(port.addr.o) == addr2
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == data2
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 1
            assert sim.get(port.cs.o) == cs
            await sim.tick()

    @staticmethod
    async def reset(sim, csr, port, *, id):
        rshd = sim.get(csr.dds_reset_rshd)

        cs = ((1 << 11) - 1) ^ (1 << id)

        for _ in range(rshd + 1):
            assert sim.get(port.addr.o) == 1
            assert sim.get(port.data.oe) == (1 << 16) - 1
            assert sim.get(port.data.o) == 0
            assert sim.get(port.reset.o) == 1
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

    @staticmethod
    async def get1(sim, csr, port, *, id, addr, data):
        assert addr & 1 == 1
        asu = sim.get(csr.dds_read_asu)
        rdhoz = sim.get(csr.dds_read_rdhoz)

        cs = ((1 << 11) - 1) ^ (1 << id)

        sim.set(port.data.i, data)

        for _ in range(asu + 1):
            assert sim.get(port.addr.o) == addr
            assert sim.get(port.data.oe) == 0
            assert sim.get(port.data.o) == 0
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 0
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(rdhoz + 1):
            assert sim.get(port.addr.o) == 1
            assert sim.get(port.data.oe) == 0
            assert sim.get(port.data.o) == 0
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

    @staticmethod
    async def get2(sim, csr, port, *, id, addr, data):
        assert addr & 1 == 1
        asu = sim.get(csr.dds_read_asu)
        rdl = sim.get(csr.dds_read_rdl)
        rdhoz = sim.get(csr.dds_read_rdhoz)

        cs = ((1 << 11) - 1) ^ (1 << id)

        sim.set(port.data.i, data >> 16)

        for _ in range(asu + 1):
            assert sim.get(port.addr.o) == addr + 2
            assert sim.get(port.data.oe) == 0
            assert sim.get(port.data.o) == 0
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 0
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(rdl + 1):
            assert sim.get(port.addr.o) == addr
            assert sim.get(port.data.oe) == 0
            assert sim.get(port.data.o) == 0
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        sim.set(port.data.i, data & 0xffff)

        for _ in range(asu + 1):
            assert sim.get(port.addr.o) == addr
            assert sim.get(port.data.oe) == 0
            assert sim.get(port.data.o) == 0
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 0
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()

        for _ in range(rdhoz + 1):
            assert sim.get(port.addr.o) == 1
            assert sim.get(port.data.oe) == 0
            assert sim.get(port.data.o) == 0
            assert sim.get(port.reset.o) == 0
            assert sim.get(port.rdb.o) == 1
            assert sim.get(port.wrb.o) == 1
            assert sim.get(port.fud.o) == 0
            assert sim.get(port.cs.o) == cs
            await sim.tick()


class SPIChecker:
    def __init__(self, pulseio, csr):
        self.__spi_cmd = None
        self.__pulseio = pulseio
        self.__csr = csr

    def spi_set(self, *, id, div, nbits, pha, pol, data, result):
        self.__spi_cmd = dict(id=id, div=div, nbits=nbits, pha=pha, pol=pol,
                             data=data, result=result)

    async def check_spi(self, sim):
        port = self.__pulseio.spi_port
        if port is None:
            return
        while True:
            # Make sure we see the command added by user coroutine
            await sim.delay(0)
            cmd = self.__spi_cmd
            self.__spi_cmd = None
            if cmd is None:
                await self.idle(sim, port, 1)
            else:
                await self.spi(sim, port, **cmd)
                await sim.tick()

    @staticmethod
    async def idle(sim, port, n=10):
        ncs = len(port.cs)
        csmask = (1 << ncs) - 1
        for _ in range(n):
            assert sim.get(port.mosi.o) == 0
            assert sim.get(port.sclk.o) == 1
            assert sim.get(port.cs.o) == csmask
            await sim.tick()

    @staticmethod
    async def spi(sim, port, *, id, div, nbits, pha, pol, data, result=0):
        ncs = len(port.cs)
        csmask = (1 << ncs) - 1
        bits = list(get_bits(data, nbits))
        result_bits = list(get_bits(result, nbits))
        cs = (1 << id)^csmask
        assert sim.get(port.mosi.o) == bits[0]
        assert sim.get(port.sclk.o) == pol
        assert sim.get(port.cs.o) == csmask

        async def check_half_period(d, clk):
            for _ in range(div):
                await sim.tick()
                assert sim.get(port.mosi.o) == d
                assert sim.get(port.sclk.o) == clk
                assert sim.get(port.cs.o) == cs

        if pha == 0:
            for n in range(nbits):
                sim.set(port.miso.i, result_bits[n])
                await check_half_period(bits[n], pol)
                await check_half_period(bits[n], 1 - pol)
            await check_half_period(0, pol)
        else:
            await check_half_period(bits[0], pol)
            for n in range(nbits):
                sim.set(port.miso.i, result_bits[n])
                await check_half_period(bits[n], 1 - pol)
                await check_half_period(bits[n], pol)


class InstBuilder:
    TTL_OP = 0x00000000
    DDS_OP = 0x10000000
    Wait_OP = 0x20000000
    ClearErr_OP = 0x30000000
    LoopBack_OP = 0x40000000
    ClockOut_OP = 0x50000000
    SPI_OP = 0x60000000
    TimeCheck_Flag = 0x8000000

    @classmethod
    def pulse(cls, ctrl, op, *, timecheck):
        if timecheck:
            ctrl = ctrl | cls.TimeCheck_Flag
        return (op, ctrl)
    @classmethod
    def dds(cls, ctrl, op, *, timecheck):
        return cls.pulse(ctrl | cls.DDS_OP, op, timecheck=timecheck)

    @classmethod
    def dds_set_freq(cls, *, id, freq, timecheck=False):
        return cls.dds(id << 4, freq, timecheck=timecheck)

    @classmethod
    def dds_set_amp_phase(cls, *, id, amp, phase, timecheck=False):
        return cls.dds(0x1 | (id << 4), (phase << 16) | amp, timecheck=timecheck)

    @classmethod
    def dds_set_two_bytes(cls, *, id, addr, data, timecheck=False):
        return cls.dds(0x2 | (id << 4) | (((addr + 1) & 0x7f) << 9),
                       data & 0xffff, timecheck=timecheck)

    @classmethod
    def dds_reset(cls, *, id, timecheck=False):
        return cls.dds(0x4 | (id << 4), 0, timecheck=timecheck)

    @classmethod
    def dds_set_four_bytes(cls, *, id, addr, data, timecheck=False):
        return cls.dds(0xf | (id << 4) | (((addr + 1) & 0x7f) << 9),
                       data, timecheck=timecheck)

    @classmethod
    def dds_get_two_bytes(cls, *, id, addr, timecheck=False):
        return cls.dds(0x3 | (id << 4) | (((addr + 1) & 0x7f) << 9), 0,
                       timecheck=timecheck)

    @classmethod
    def dds_get_four_bytes(cls, *, id, addr, timecheck=False):
        return cls.dds(0xe | (id << 4) | (((addr + 1) & 0x7f) << 9), 0,
                       timecheck=timecheck)

    @classmethod
    def ttl(cls, *, ttl, t, bank, timecheck=False):
        return cls.pulse(cls.TTL_OP | t | (bank << 24), ttl, timecheck=timecheck)

    @classmethod
    def clockout(cls, *, div, timecheck=False):
        return cls.pulse(cls.ClockOut_OP, div & 0xff, timecheck=timecheck)

    @classmethod
    def wait(cls, *, t, trig_chn=-1, trig_raise=True, timecheck=False):
        trig_data = 0
        if trig_chn >= 0:
            trig_data = (trig_chn << 20) | ((1 if trig_raise else 2) << 28)
        return cls.pulse(cls.Wait_OP | t, trig_data, timecheck=timecheck)

    @classmethod
    def clear_error(cls):
        return cls.pulse(cls.ClearErr_OP, 0, timecheck=False)

    @classmethod
    def loopback(cls, *, data, timecheck=False):
        return cls.pulse(cls.LoopBack_OP, data, timecheck=timecheck)

    @classmethod
    def spi(cls, *, id, div, pha, pol, data, save_result, timecheck=False):
        opcode = (div - 1) | (save_result << 10) | (id << 11) | (pha << 13) | (pol << 14)
        return cls.pulse(opcode | cls.SPI_OP, data, timecheck=timecheck)
