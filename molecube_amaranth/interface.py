#

from amaranth import *
from amaranth_axi.axitools import axi_write_reg, AXISlaveReadIFace, AXISlaveWriteIFace

from transactron import TModule, Transaction
from transactron.lib import PipelineBuilder
from transactron.lib import BasicFifo

from types import SimpleNamespace

from .config import MAJOR_VERSION, MINOR_VERSION
from .csr import Registers
from .utils import xvalue, reg_chain

def relaxed_read_shadow(m, reg):
    r2 = Signal.like(reg)
    r2.attrs["molecube.vivado.false_path_to"] = "TRUE"
    m.d.sync += r2.eq(reg)
    return r2

class ControlInterface(Elaboratable):
    def __init__(self, axi, csr_regs, fifos, ioctrl, prefix=0, valid_width=None):
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
        self.ioctrl = ioctrl

    def elaborate(self, plat):
        m = TModule()

        m.submodules.write_iface = write_iface = AXISlaveWriteIFace(self.axi,
                                                                    buffered=True)
        m.submodules.read_iface = read_iface = AXISlaveReadIFace(self.axi,
                                                                 buffered=True)

        wr_shadow = SimpleNamespace()
        rd_shadow = SimpleNamespace()

        csr = self.csr_regs

        for reg_name in ['ttl_hi_mask', 'ttl_lo_mask', 'timing_ctrl',
                         'dds_timing1', 'dds_timing2', 'loopback']:
            real_reg = getattr(csr, reg_name)
            if reg_name == 'ttl_hi_mask' or reg_name == 'ttl_lo_mask':
                real_reg = real_reg[:self.ioctrl.nttlout]
            if reg_name in ('ttl_hi_mask', 'ttl_lo_mask', 'dds_timing1',
                            'dds_timing2', 'loopback'):
                rd_reg = relaxed_read_shadow(m, real_reg)
            else:
                rd_reg, _ = reg_chain(m, input=real_reg, levels=2)
            _, wr_reg = reg_chain(m, output=real_reg, levels=2)
            setattr(wr_shadow, reg_name, wr_reg)
            setattr(rd_shadow, reg_name, rd_reg)

        for reg_name in ['ttl_out', 'timing_status', 'clockout_div', 'dbg_result_count',
                         'dds0_reg', 'dds1_reg']:
            real_reg = getattr(csr, reg_name)
            if reg_name in ('ttl_out', 'clockout_div', 'dbg_result_count',
                            'dds0_reg', 'dds1_reg'):
                rd_reg = relaxed_read_shadow(m, real_reg)
            else:
                rd_reg, _ = reg_chain(m, input=real_reg, levels=2)
            setattr(rd_shadow, reg_name, rd_reg)

        for (k, c) in csr.all_counters.items():
            setattr(rd_shadow, k, relaxed_read_shadow(m, c.value))

        def rd_ttl_hi(idx):
            return rd_shadow.ttl_hi_mask[idx * 32:(idx + 1) * 32]
        def rd_ttl_lo(idx):
            return rd_shadow.ttl_lo_mask[idx * 32:(idx + 1) * 32]

        def wr_ttl_hi(idx):
            return wr_shadow.ttl_hi_mask[idx * 32:(idx + 1) * 32]
        def wr_ttl_lo(idx):
            return wr_shadow.ttl_lo_mask[idx * 32:(idx + 1) * 32]

        def ttl_out_reg(idx):
            return rd_shadow.ttl_out[idx * 32:(idx + 1) * 32]

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
                    axi_write_reg(m, wr_ttl_hi(0), data, strb)
                with m.Case(0x01):
                    axi_write_reg(m, wr_ttl_lo(0), data, strb)
                with m.Case(0x03):
                    axi_write_reg(m, wr_shadow.timing_ctrl, data, strb)
                with m.Case(0x04):
                    self.ioctrl.ttlout.set_bank_user0(m, hi=data[:8], lo=data[8:16],
                                                      byte=data[16:18])

                with m.Case(0x10):
                    axi_write_reg(m, wr_ttl_hi(1), data, strb)
                with m.Case(0x11):
                    axi_write_reg(m, wr_ttl_lo(1), data, strb)
                with m.Case(0x12):
                    axi_write_reg(m, wr_ttl_hi(2), data, strb)
                with m.Case(0x13):
                    axi_write_reg(m, wr_ttl_lo(2), data, strb)
                with m.Case(0x14):
                    axi_write_reg(m, wr_ttl_hi(3), data, strb)
                with m.Case(0x15):
                    axi_write_reg(m, wr_ttl_lo(3), data, strb)
                with m.Case(0x16):
                    axi_write_reg(m, wr_ttl_hi(4), data, strb)
                with m.Case(0x17):
                    axi_write_reg(m, wr_ttl_lo(4), data, strb)
                with m.Case(0x18):
                    axi_write_reg(m, wr_ttl_hi(5), data, strb)
                with m.Case(0x19):
                    axi_write_reg(m, wr_ttl_lo(5), data, strb)
                with m.Case(0x1a):
                    axi_write_reg(m, wr_ttl_hi(6), data, strb)
                with m.Case(0x1b):
                    axi_write_reg(m, wr_ttl_lo(6), data, strb)
                with m.Case(0x1c):
                    axi_write_reg(m, wr_ttl_hi(7), data, strb)
                with m.Case(0x1d):
                    axi_write_reg(m, wr_ttl_lo(7), data, strb)
                with m.Case(0x1e):
                    axi_write_reg(m, wr_shadow.loopback, data, strb)
                with m.Case(0x40):
                    self.ioctrl.ttlout.set_bank_user1(m, hi=data[:8], lo=data[8:16],
                                                      byte=data[16:18])
                with m.Case(0x41):
                    self.ioctrl.ttlout.set_bank_user2(m, hi=data[:8], lo=data[8:16],
                                                      byte=data[16:18])
                with m.Case(0x42):
                    self.ioctrl.ttlout.set_bank_user3(m, hi=data[:8], lo=data[8:16],
                                                      byte=data[16:18])
                with m.Case(0x43):
                    self.ioctrl.ttlout.set_bank_user4(m, hi=data[:8], lo=data[8:16],
                                                      byte=data[16:18])
                with m.Case(0x44):
                    self.ioctrl.ttlout.set_bank_user5(m, hi=data[:8], lo=data[8:16],
                                                      byte=data[16:18])
                with m.Case(0x45):
                    self.ioctrl.ttlout.set_bank_user6(m, hi=data[:8], lo=data[8:16],
                                                      byte=data[16:18])
                with m.Case(0x46):
                    self.ioctrl.ttlout.set_bank_user7(m, hi=data[:8], lo=data[8:16],
                                                      byte=data[16:18])

                with m.Case(0x48):
                    self.ioctrl.dds0.read_dds_cache(m, id=data[7:11], addr=data[1:7])
                with m.Case(0x49):
                    self.ioctrl.dds1.read_dds_cache(m, id=data[7:11], addr=data[1:7])

                with m.Case(0x50):
                    axi_write_reg(m, wr_shadow.dds_timing1, data, strb)
                with m.Case(0x51):
                    axi_write_reg(m, wr_shadow.dds_timing2, data, strb)

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

        @read_pipe.stage(m, o=[('fifo_data', self.data_width)])
        def _(idx, resp):
            res = Signal(self.data_width)
            with m.If((idx == 0x1f) & ~resp[0]):
                csr.dbg_result_consumed.count(m)
                m.d.av_comb += res.eq(self.fifos.result_fifo.read(m))
            with m.Else():
                m.d.av_comb += res.eq(xvalue(m, self.data_width))
            return dict(fifo_data=res)

        @read_pipe.stage(m, o=[(f'idx{i}', 1) for i in range(self.valid_width - 2)])
        def _(idx):
            return {f'idx{i}': idx[i] for i in range(self.valid_width - 2)}

        read_regs = {
                0x00: rd_ttl_hi(0),
                0x01: rd_ttl_lo(0),
                0x02: rd_shadow.timing_status,
                0x03: rd_shadow.timing_ctrl,
                0x04: ttl_out_reg(0),
                0x05: rd_shadow.clockout_div,
                0x06: MAJOR_VERSION,
                0x07: MINOR_VERSION,
                0x10: rd_ttl_hi(1),
                0x11: rd_ttl_lo(1),
                0x12: rd_ttl_hi(2),
                0x13: rd_ttl_lo(2),
                0x14: rd_ttl_hi(3),
                0x15: rd_ttl_lo(3),
                0x16: rd_ttl_hi(4),
                0x17: rd_ttl_lo(4),
                0x18: rd_ttl_hi(5),
                0x19: rd_ttl_lo(5),
                0x1a: rd_ttl_hi(6),
                0x1b: rd_ttl_lo(6),
                0x1c: rd_ttl_hi(7),
                0x1d: rd_ttl_lo(7),
                0x1e: rd_shadow.loopback,
                0x20: rd_shadow.dbg_inst_word_count,
                0x21: rd_shadow.dbg_inst_count,
                0x22: rd_shadow.dbg_ttl_count,
                0x23: rd_shadow.dbg_dds_count,
                0x24: rd_shadow.dbg_wait_count,
                0x25: rd_shadow.dbg_clear_count,
                0x26: rd_shadow.dbg_loopback_count,
                0x27: rd_shadow.dbg_clock_count,
                0x28: rd_shadow.dbg_spi_count,
                0x29: rd_shadow.dbg_underflow_cycle,
                0x2a: rd_shadow.dbg_inst_cycle,
                # 0x2b: rd_shadow.dbg_ttl_cycle,
                # 0x2c: rd_shadow.dbg_wait_cycle,
                # 0x2d: rd_shadow.dbg_result_overflow_count,
                0x2e: rd_shadow.dbg_result_count,
                0x2f: rd_shadow.dbg_result_generated,
                0x30: rd_shadow.dbg_result_consumed,

                0x40: ttl_out_reg(1),
                0x41: ttl_out_reg(2),
                0x42: ttl_out_reg(3),
                0x43: ttl_out_reg(4),
                0x44: ttl_out_reg(5),
                0x45: ttl_out_reg(6),
                0x46: ttl_out_reg(7),

                0x48: rd_shadow.dds0_reg,
                0x49: rd_shadow.dds1_reg,

                0x50: rd_shadow.dds_timing1,
                0x51: rd_shadow.dds_timing2,
        }

        stage_state = {k: lambda arg, v=v: v for k, v in read_regs.items()}
        stage_state[0x1f] = lambda arg: arg.fifo_data

        def get_stage(arg, i):
            if i in stage_state:
                return stage_state[i](arg)

        max_batch_sz = 8
        for bit in range(self.valid_width - 2):
            idx_out_width = self.valid_width - 2 - 1 - bit
            next_stage_state = {}
            for idx_out_val in range(1 << idx_out_width):
                if ((idx_out_val * 2) in stage_state or
                    (idx_out_val * 2 + 1) in stage_state):
                    fld = f'data_{bit}_{idx_out_val}'
                    next_stage_state[idx_out_val] = lambda arg, fld=fld: getattr(arg, fld)

            idx_outs = list(next_stage_state.keys())
            nidx_outs = len(idx_outs)
            nbatches = (nidx_outs + max_batch_sz - 1) // max_batch_sz
            batch_sz = (nidx_outs + nbatches - 1) // nbatches

            if bit == 2:
                read_pipe.fifo(depth=2)

            for start_idx in range(0, nidx_outs, batch_sz):
                end_idx = min(nidx_outs, start_idx + batch_sz)
                idxs = idx_outs[start_idx:end_idx]

                layout_in = [(f'idx{bit}', 1)]
                layout_out = []

                for idx_out_val in idxs:
                    if idx_out_width == 0:
                        layout_out.append(('data', self.data_width))
                    else:
                        layout_out.append((f'data_{bit}_{idx_out_val}', self.data_width))
                    if bit == 0:
                        if idx_out_val == 0x1f >> 1:
                            layout_in.append(('fifo_data', self.data_width))
                        continue
                    if (idx_out_val * 2) in stage_state:
                        layout_in.append((f'data_{bit - 1}_{idx_out_val * 2}', self.data_width))
                    if (idx_out_val * 2 + 1) in stage_state:
                        layout_in.append((f'data_{bit - 1}_{idx_out_val * 2 + 1}', self.data_width))

                @read_pipe.stage(m, i=layout_in, o=layout_out)
                def _(arg):
                    res = {}
                    idx_bit = getattr(arg, f'idx{bit}')
                    for idx_out_val in idxs:
                        v0 = get_stage(arg, idx_out_val * 2)
                        v1 = get_stage(arg, idx_out_val * 2 + 1)
                        if idx_out_width == 0:
                            fld = 'data'
                        else:
                            fld = f'data_{bit}_{idx_out_val}'
                        if v0 is None:
                            res[fld] = v1
                        elif v1 is None:
                            res[fld] = v0
                        else:
                            res[fld] = Mux(idx_bit, v1, v0)
                    return res

            stage_state = next_stage_state

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
