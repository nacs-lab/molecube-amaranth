#

from amaranth import *
from amaranth_axi.axitools import axi_write_reg, AXISlaveReadIFace, AXISlaveWriteIFace

from transactron import TModule, Transaction
from transactron.lib import PipelineBuilder
from transactron.lib import BasicFifo

from types import SimpleNamespace

from .config import MAJOR_VERSION, MINOR_VERSION
from .csr import Registers
from .utils import xvalue

class ControlInterface(Elaboratable):
    def __init__(self, axi, csr_regs, fifos, prefix=0, valid_width=None):
        self.axi = axi
        self.addr_width = len(axi.AWADDR)
        self.data_width = len(axi.WDATA)
        assert self.data_width == 32
        self.id_width = len(axi.AWID)
        self.csr_regs = csr_regs
        self.fifos = fifos
        if valid_width is None:
            valid_width = self.addr_width
        self.prefix = prefix >> valid_width
        self.valid_width = valid_width

    def elaborate(self, plat):
        m = TModule()

        m.submodules.write_iface = write_iface = AXISlaveWriteIFace(self.axi,
                                                                    buffered=True)
        m.submodules.read_iface = read_iface = AXISlaveReadIFace(self.axi,
                                                                 buffered=True)

        csr_shadow0 = SimpleNamespace()
        csr_shadow = SimpleNamespace()

        csr = self.csr_regs

        for wr_reg in ['ttl_hi_mask', 'ttl_lo_mask', 'timing_ctrl',
                       'dds_timing1', 'dds_timing2']:
            tgt = getattr(csr, wr_reg)
            src0 = Signal.like(tgt)
            src = Signal.like(tgt)
            setattr(csr_shadow0, wr_reg, src0)
            setattr(csr_shadow, wr_reg, src)
            m.d.sync += [tgt.eq(src0), src0.eq(src)]

        for rd_reg in ['ttl_out', 'timing_status', 'clockout_div']:
            src = getattr(csr, rd_reg)
            tgt0 = Signal.like(src)
            tgt = Signal.like(src)
            setattr(csr_shadow0, rd_reg, tgt0)
            setattr(csr_shadow, rd_reg, tgt)
            m.d.sync += [tgt0.eq(src), tgt.eq(tgt0)]

        for (k, c) in csr.all_counters.items():
            cv = c.value
            tgt0 = Signal.like(cv)
            tgt = Signal.like(cv)
            setattr(csr_shadow0, k, tgt0)
            setattr(csr_shadow, k, tgt)
            m.d.sync += [tgt0.eq(cv), tgt.eq(tgt0)]

        ttl_out = csr_shadow.ttl_out
        ttl_hi_mask = csr_shadow.ttl_hi_mask
        ttl_lo_mask = csr_shadow.ttl_lo_mask

        def ttl_hi_reg(idx):
            return ttl_hi_mask[idx * 32:(idx + 1) * 32]
        def ttl_lo_reg(idx):
            return ttl_lo_mask[idx * 32:(idx + 1) * 32]
        def ttl_out_reg(idx):
            return ttl_out[idx * 32:(idx + 1) * 32]

        with Transaction().body(m, ready=self.fifos.result_fifo.write.run):
            csr.dbg_result_generated.count(m)

        # Buffer for command fifo to simplify write combinational logic
        m.submodules.cmd_pre_fifo = cmd_pre_fifo = BasicFifo([('data', self.data_width)], 2)
        with Transaction().body(m):
            self.fifos.cmd_fifo.write(m, cmd_pre_fifo.read(m))

        m.submodules.write_pipe = write_pipe = PipelineBuilder()
        start_write = write_pipe.create_external(i=[('idx', self.valid_width - 2),
                                                    ('data', self.data_width),
                                                    ('strb', 4)], o=[])

        @write_pipe.stage(m)
        def _(idx, data):
            with m.If(idx == 0x1f):
                csr.dbg_inst_word_count.count(m)
                cmd_pre_fifo.write(m, data)

        @write_pipe.stage(m)
        def _(idx, data, strb):
            with m.Switch(idx):
                with m.Case(0x00):
                    axi_write_reg(m, ttl_hi_reg(0), data, strb)
                with m.Case(0x01):
                    axi_write_reg(m, ttl_lo_reg(0), data, strb)
                with m.Case(0x03):
                    axi_write_reg(m, csr_shadow.timing_ctrl, data, strb)

                with m.Case(0x10):
                    axi_write_reg(m, ttl_hi_reg(1), data, strb)
                with m.Case(0x11):
                    axi_write_reg(m, ttl_lo_reg(1), data, strb)
                with m.Case(0x12):
                    axi_write_reg(m, ttl_hi_reg(2), data, strb)
                with m.Case(0x13):
                    axi_write_reg(m, ttl_lo_reg(2), data, strb)
                with m.Case(0x14):
                    axi_write_reg(m, ttl_hi_reg(3), data, strb)
                with m.Case(0x15):
                    axi_write_reg(m, ttl_lo_reg(3), data, strb)
                with m.Case(0x16):
                    axi_write_reg(m, ttl_hi_reg(4), data, strb)
                with m.Case(0x17):
                    axi_write_reg(m, ttl_lo_reg(4), data, strb)
                with m.Case(0x18):
                    axi_write_reg(m, ttl_hi_reg(5), data, strb)
                with m.Case(0x19):
                    axi_write_reg(m, ttl_lo_reg(5), data, strb)
                with m.Case(0x1a):
                    axi_write_reg(m, ttl_hi_reg(6), data, strb)
                with m.Case(0x1b):
                    axi_write_reg(m, ttl_lo_reg(6), data, strb)
                with m.Case(0x1c):
                    axi_write_reg(m, ttl_hi_reg(7), data, strb)
                with m.Case(0x1d):
                    axi_write_reg(m, ttl_lo_reg(7), data, strb)
                with m.Case(0x1e):
                    axi_write_reg(m, csr.loopback, data, strb)

                with m.Case(0x50):
                    axi_write_reg(m, Cat(csr_shadow.dds_timing1,
                                         Signal(self.data_width - len(csr_shadow.dds_timing1))),
                                  data, strb)
                with m.Case(0x51):
                    axi_write_reg(m, Cat(csr_shadow.dds_timing2,
                                         Signal(self.data_width - len(csr_shadow.dds_timing2))),
                                  data, strb)

        if self.valid_width != self.addr_width:
            m.submodules.prewrite_pipe = prewrite_pipe = PipelineBuilder()
            start_prewrite = prewrite_pipe.create_external(
                i=[('idx', self.addr_width - 2), ('data', self.data_width),
                   ('strb', 4), ('id', self.id_width), ('last', 1)], o=[])

            @prewrite_pipe.stage(m)
            def _(idx, data, strb, id, last):
                idx_prefix = idx >> (self.valid_width - 2)
                valid = Signal()
                m.d.top_comb += valid.eq(idx_prefix == self.prefix)
                with m.If(last):
                    write_iface.done(m, resp=Mux(valid, 0, 3), id=id)
                with m.If(valid):
                    start_write(m, idx=idx[:self.valid_width - 2], data=data, strb=strb)

        with Transaction().body(m):
            req = write_iface.get(m)
            addr = req.addr
            if self.valid_width == self.addr_width:
                start_write(m, idx=addr >> 2, data=req.data, strb=req.strb)
                with m.If(req.last):
                    write_iface.done(m, id=req.id)
            else:
                start_prewrite(m, idx=addr >> 2, data=req.data, strb=req.strb,
                              id=req.id, last=req.last)

        m.submodules.read_pipe = read_pipe = PipelineBuilder()

        start_read = read_pipe.create_external(i=[('idx', self.addr_width - 2),
                                                  ('id', self.id_width),
                                                  ('last', 1)], o=[])

        @read_pipe.stage(m)
        def _():
            pass

        read_pipe.fifo(depth=2)

        @read_pipe.stage(m, o=[('idx', self.valid_width - 2), ('resp', 2)])
        def _(idx):
            return dict(idx=idx[:self.valid_width - 2],
                        resp=Mux((idx >> (self.valid_width - 2)) == self.prefix, 0, 3))

        @read_pipe.stage(m, o=[('data', self.data_width)])
        def _(idx, resp):
            res = Signal(self.data_width)
            with m.If((idx == 0x1f) & ~resp[0]):
                csr.dbg_result_consumed.count(m)
                m.d.av_comb += res.eq(self.fifos.result_fifo.read(m))
            with m.Else():
                m.d.av_comb += res.eq(xvalue(m, self.data_width))
            return dict(data=res)

        @read_pipe.stage(m)
        def _():
            pass

        read_regs = {
                0x00: ttl_hi_reg(0),
                0x01: ttl_lo_reg(0),
                0x02: csr_shadow.timing_status,
                0x03: csr_shadow.timing_ctrl,
                0x04: ttl_out_reg(0),
                0x05: C(0, self.data_width) | csr_shadow.clockout_div,
                0x06: MAJOR_VERSION,
                0x07: MINOR_VERSION,
                0x10: ttl_hi_reg(1),
                0x11: ttl_lo_reg(1),
                0x12: ttl_hi_reg(2),
                0x13: ttl_lo_reg(2),
                0x14: ttl_hi_reg(3),
                0x15: ttl_lo_reg(3),
                0x16: ttl_hi_reg(4),
                0x17: ttl_lo_reg(4),
                0x18: ttl_hi_reg(5),
                0x19: ttl_lo_reg(5),
                0x1a: ttl_hi_reg(6),
                0x1b: ttl_lo_reg(6),
                0x1c: ttl_hi_reg(7),
                0x1d: ttl_lo_reg(7),
                0x1e: csr.loopback,
                0x20: csr_shadow.dbg_inst_word_count,
                0x21: csr_shadow.dbg_inst_count,
                0x22: csr_shadow.dbg_ttl_count,
                0x23: csr_shadow.dbg_dds_count,
                0x24: csr_shadow.dbg_wait_count,
                0x25: csr_shadow.dbg_clear_count,
                0x26: csr_shadow.dbg_loopback_count,
                0x27: csr_shadow.dbg_clock_count,
                0x28: csr_shadow.dbg_spi_count,
                0x29: csr_shadow.dbg_underflow_cycle,
                0x2a: csr_shadow.dbg_inst_cycle,
                # 0x2b: csr_shadow.dbg_ttl_cycle,
                # 0x2c: csr_shadow.dbg_wait_cycle,
                # 0x2d: csr_shadow.dbg_result_overflow_count,
                # 0x2e: csr_shadow.dbg_result_count,
                0x2f: csr_shadow.dbg_result_generated,
                0x30: csr_shadow.dbg_result_consumed,

                0x40: ttl_out_reg(1),
                0x41: ttl_out_reg(2),
                0x42: ttl_out_reg(3),
                0x43: ttl_out_reg(4),
                0x44: ttl_out_reg(5),
                0x45: ttl_out_reg(6),
                0x46: ttl_out_reg(7),

                0x50: csr_shadow.dds_timing1 | C(0, self.data_width),
                0x51: csr_shadow.dds_timing2 | C(0, self.data_width),
        }

        read_regs = list(read_regs.items())
        nread_regs = len(read_regs)
        batch_sz = 6

        for start_idx in range(0, nread_regs, batch_sz):
            end_idx = min(nread_regs, start_idx + batch_sz)
            assert start_idx != end_idx
            @read_pipe.stage(m, o=[('data', self.data_width)])
            def _(idx, data):
                res = Signal(self.data_width)
                with m.Switch(idx):
                    with m.Case(0x1f):
                        m.d.av_comb += res.eq(data)
                    for i in range(start_idx):
                        reg_idx, reg_val = read_regs[i]
                        with m.Case(reg_idx):
                            m.d.av_comb += res.eq(data)
                    for i in range(start_idx, end_idx):
                        reg_idx, reg_val = read_regs[i]
                        with m.Case(reg_idx):
                            m.d.av_comb += res.eq(reg_val)
                    with m.Default():
                        m.d.av_comb += res.eq(xvalue(m, self.data_width))
                return dict(data=res)

            @read_pipe.stage(m)
            def _():
                pass

        read_pipe.fifo(depth=2)

        @read_pipe.stage(m)
        def _():
            pass

        @read_pipe.stage(m)
        def _(data, resp, id, last):
            read_iface.done(m, data=data, resp=resp, id=id, last=last)

        with Transaction().body(m):
            req = read_iface.get(m)
            start_read(m, idx=req.addr >> 2, id=req.id, last=req.last)

        return m

if __name__ == '__main__':
    from amaranth.back import verilog
    from amaranth_axi.axibus import AXI4
    from transactron import TransactronContextElaboratable
    from .config import Config
    from .csr import Registers
    from .fifo import Fifos

    config = Config()

    m = TModule()
    m.submodules.regs = regs = Registers(config)
    m.submodules.fifos = fifos = Fifos(32)
    m.submodules.ctrl = ctrl = ControlInterface(AXI4(32, 10, 6, len_width=4).create(),
                                                regs, fifos)
    m = TransactronContextElaboratable(m)
    print(verilog.convert(m, ports=ctrl.axi.all_ports))
