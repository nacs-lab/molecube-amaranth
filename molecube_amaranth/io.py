#

from amaranth import *
from amaranth.build import *
from amaranth.lib import io

from types import SimpleNamespace

def _sim_port(pins):
    return io.SimulationPort(pins.dir, len(pins))

def get_dds_ports(plat, i):
    assert i in (0, 1)
    addr = Pins(f"fmc_{i}:LA22_N fmc_{i}:LA21_P fmc_{i}:LA22_P fmc_{i}:LA19_N "
                f"fmc_{i}:LA20_N fmc_{i}:LA19_P fmc_{i}:LA20_P", dir="o")
    data = Pins(f"fmc_{i}:LA15_N fmc_{i}:LA16_N fmc_{i}:LA15_P fmc_{i}:LA16_P "
                f"fmc_{i}:LA11_N fmc_{i}:LA12_N fmc_{i}:LA11_P fmc_{i}:LA12_P "
                f"fmc_{i}:LA07_N fmc_{i}:LA08_N fmc_{i}:LA07_P fmc_{i}:LA08_P "
                f"fmc_{i}:LA04_N fmc_{i}:LA03_N fmc_{i}:LA04_P fmc_{i}:LA03_P", dir="io")
    wrb = Pins(f"fmc_{i}:LA24_P", dir="o")
    rdb = Pins(f"fmc_{i}:LA25_N", dir="o")
    reset = Pins(f"fmc_{i}:LA25_P", dir="o")
    fud = Pins(f"fmc_{i}:LA21_N", dir="o")
    cs = Pins(f"fmc_{i}:LA24_N fmc_{i}:LA29_P fmc_{i}:LA28_P fmc_{i}:LA29_N "
              f"fmc_{i}:LA28_N fmc_{i}:LA31_P fmc_{i}:LA30_P fmc_{i}:LA31_N "
              f"fmc_{i}:LA30_N fmc_{i}:LA33_P fmc_{i}:LA33_N", dir="o")

    if plat is None:
        port = SimpleNamespace()
        port.addr = _sim_port(addr)
        port.data = _sim_port(data)
        port.wrb = _sim_port(wrb)
        port.rdb = _sim_port(rdb)
        port.reset = _sim_port(reset)
        port.fud = _sim_port(fud)
        port.cs = _sim_port(cs)
        return port

    plat.add_resources(
        [Resource("DDS", i, Subsignal("addr", addr), Subsignal("data", data),
                  Subsignal("wrb", wrb), Subsignal("rdb", rdb),
                  Subsignal("reset", reset), Subsignal("fud", fud),
                  Subsignal("cs", cs), Attrs(IOSTANDARD="LVCMOS33", DRIVE="4"))])
    return plat.request("DDS", i, dir="-")

TTL_BOARD_PINS = ["LA27_P", "LA26_P", "LA27_N", "LA26_N", # E1
                  "LA18_P_CC", "LA18_N_CC", "LA23_P", "LA23_N", # E2
                  "LA17_N_CC", "LA17_P_CC", "LA14_N", "LA13_N", # E3
                  "LA10_N", "LA09_N", "LA13_P", "LA14_P", # E4
                  "LA09_P", "LA10_P", "LA05_N", "LA05_P", # E5
                  "LA01_P_CC", "LA01_N_CC", "LA06_P", "LA06_N", # E6
                  "LA02_N", "LA00_N_CC", "LA02_P", "LA00_P_CC", # E8
                  ]

def ttl_bd_pin(fmc, idx):
    return f"fmc_{fmc}:{TTL_BOARD_PINS[idx]}"

SMA_OUT_PINS = ["CLK0_M2C_P", "CLK0_M2C_N", "CLK1_M2C_P", "CLK1_M2C_N", "LA32_P", "LA32_N"]

def sma_pin(fmc, idx):
    return f"fmc_{fmc}:{SMA_OUT_PINS[idx]}"

def get_ttlin_ports(plat, pins):
    ttlin = Pins(pins, dir="i")
    if plat is None:
        return _sim_port(ttlin)
    plat.add_resources(
        [Resource("TTL_IN", 0, ttlin, Attrs(IOSTANDARD="LVCMOS33"))])
    return plat.request("TTL_IN", 0, dir="-")

def get_ttlout_ports(plat, pins):
    ttlout = Pins(pins, dir="o")
    if plat is None:
        return _sim_port(ttlout)
    plat.add_resources(
        [Resource("TTL_OUT", 0, ttlout, Attrs(IOSTANDARD="LVCMOS33", DRIVE="4"))])
    return plat.request("TTL_OUT", 0, dir="-")

def get_clockout_ports(plat, pin):
    clkout = Pins(pin, dir="o")
    if plat is None:
        return _sim_port(clkout)
    plat.add_resources(
        [Resource("CLOCK_OUT", 0, clkout, Attrs(IOSTANDARD="LVCMOS33", DRIVE="4"))])
    return plat.request("CLOCK_OUT", 0, dir="-")

def get_spi(plat, *, miso, mosi, sclk, cs):
    if miso == '' and mosi == '' and sclk == '' and cs == '':
        return
    if miso == '' or mosi == '' or sclk == '' or cs == '':
        raise ValueError('SPI ports must be all empty or all non-empty')

    miso = Pins(miso, dir="i")
    mosi = Pins(mosi, dir="o")
    sclk = Pins(sclk, dir="o")
    cs = Pins(cs, dir="o")

    if plat is None:
        port = SimpleNamespace()
        port.miso = _sim_port(miso)
        port.mosi = _sim_port(mosi)
        port.sclk = _sim_port(sclk)
        port.cs = _sim_port(cs)
        return port

    plat.add_resources(
        [Resource("SPI", 0,
                  Subsignal("miso", miso),
                  Subsignal("mosi", mosi, Attrs(DRIVE="4")),
                  Subsignal("sclk", sclk, Attrs(DRIVE="4")),
                  Subsignal("cs", cs, Attrs(DRIVE="4")),
                  Attrs(IOSTANDARD="LVCMOS33"))])
    return plat.request("SPI", 0, dir="-")

def io_buffer(dir, port, iobuf_instance):
    if not iobuf_instance:
        return io.Buffer(dir, port)
    from amaranth.hdl import IOBufferInstance
    if dir == "i":
        return IOBufferInstance(port.io, i=Signal(len(port)))
    elif dir == "io":
        return IOBufferInstance(port.io, i=Signal(len(port)), o=Signal(len(port)),
                                oe=Signal())
    else:
        raise TypeError(f"Unsupported buffer direction: {io}")

class DDSBuff(Elaboratable):
    def __init__(self, ddsport, iobuf_instance=False):
        self.addr = io.Buffer("o", ddsport.addr)
        self.data = io_buffer("io", ddsport.data, iobuf_instance)
        self.wrb = io.Buffer("o", ddsport.wrb)
        self.rdb = io.Buffer("o", ddsport.rdb)
        self.reset = io.Buffer("o", ddsport.reset)
        self.fud = io.Buffer("o", ddsport.fud)
        self.cs = io.Buffer("o", ddsport.cs)

    def elaborate(self, plat):
        m = Module()

        m.submodules.addr = self.addr
        m.submodules.data = self.data
        m.submodules.wrb = self.wrb
        m.submodules.rdb = self.rdb
        m.submodules.reset = self.reset
        m.submodules.fud = self.fud
        m.submodules.cs = self.cs

        return m

class SPIBuff(Elaboratable):
    def __init__(self, spiport):
        self.miso = io.Buffer("i", spiport.miso)
        self.mosi = io.Buffer("o", spiport.mosi)
        self.sclk = io.Buffer("o", spiport.sclk)
        self.cs = io.Buffer("o", spiport.cs)

    def elaborate(self, plat):
        m = Module()

        m.submodules.miso = self.miso
        m.submodules.mosi = self.mosi
        m.submodules.sclk = self.sclk
        m.submodules.cs = self.cs

        return m

class PulseIO(Elaboratable):
    @classmethod
    def from_config(cls, plat, config):
        return cls(ttlin=get_ttlin_ports(plat, config.TTLIN),
                   ttlout=get_ttlout_ports(plat, config.TTLOUT),
                   dds0=get_dds_ports(plat, 0),
                   dds1=get_dds_ports(plat, 1),
                   clockout=get_clockout_ports(plat, config.CLOCKOUT),
                   spi=get_spi(plat, miso=config.SPI_MISO, mosi=config.SPI_MOSI,
                               sclk=config.SPI_SCLK, cs=config.SPI_CS),
                   iobuf_instance=config.IOBUF_INSTANCE)

    def __init__(self, *, ttlin, ttlout, dds0, dds1, clockout, spi, iobuf_instance=False):
        self.ttlin_port = ttlin
        self.ttlout_port = ttlout
        self.dds0_port = dds0
        self.dds1_port = dds1
        self.clockout_port = clockout
        self.spi_port = spi

        self.ttlin = io_buffer("i", ttlin, iobuf_instance)
        self.ttlout = io.Buffer("o", ttlout)
        self.dds0 = DDSBuff(dds0, iobuf_instance)
        self.dds1 = DDSBuff(dds1, iobuf_instance)
        self.clockout = io.Buffer("o", clockout)
        if spi is None:
            self.spi = None
        else:
            self.spi = SPIBuff(spi)

    def elaborate(self, plat):
        m = Module()

        m.submodules.ttlin = self.ttlin
        m.submodules.ttlout = self.ttlout
        m.submodules.dds0 = self.dds0
        m.submodules.dds1 = self.dds1
        m.submodules.clockout = self.clockout
        if self.spi is not None:
            m.submodules.spi = self.spi

        return m
