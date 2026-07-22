#

from amaranth import *
from amaranth_axi.axitools import axi_write_reg, AXISlaveReadIFace, AXISlaveWriteIFace

from transactron import TModule, Transaction
from transactron.lib import PipelineBuilder

from types import SimpleNamespace

from .config import MAJOR_VERSION, MINOR_VERSION
from .csr import Registers
from .fifo import BufferedFifo
from .utils import xvalue, reg_chain

def relaxed_read_shadow(m, reg):
    r2 = Signal.like(reg)
    r2.attrs["molecube.vivado.false_path_to"] = "TRUE"
    m.d.sync += r2.eq(reg)
    return r2

class ReadData:
    def __init__(self, addr, size, bit):
        self.addr = addr
        self.size = size
        self.value = None
        self.expr = None
        self.bit = bit
        self.batch = -1

class ReadStates:
    def __init__(self):
        self.values = []

    def add_leaf(self, addr, src, sz=None):
        if isinstance(src, int):
            if src == 0:
                src = C(0, 0)
            else:
                src = C(src)
        if sz is None:
            assert not isinstance(src, str)
            sz = len(src)
        data = ReadData(addr, sz, -1)
        data.value = src
        self.values.append(data)

    def compute_exprs_bit(self, bit, in_set):
        out_set = dict()
        bit_mask = 1 << bit
        while in_set:
            addr = next(iter(in_set))
            value = in_set.pop(addr)
            addr2 = addr ^ bit_mask
            addr0 = addr & ~bit_mask
            if addr2 not in in_set:
                out_set[addr0] = value
                continue
            if addr == addr0:
                value0 = value
                value1 = in_set.pop(addr2)
            else:
                value1 = value
                value0 = in_set.pop(addr2)
            if value0.size == 0 and value1.size == 0:
                assert value0.expr is None
                assert value1.expr is None
                out_set[addr0] = value0
                continue
            data = ReadData(addr0, max(value0.size, value1.size), bit)
            data.value = f'data_{bit}_{addr0:x}'
            data.expr = (value0, value1)
            out_set[addr0] = data
            self.values.append(data)
        return out_set

    def compute_exprs(self):
        work_set = {data.addr: data for data in self.values}
        bit = 0
        while len(work_set) > 1:
            work_set = self.compute_exprs_bit(bit, work_set)
            bit += 1
        _, data = work_set.popitem()
        assert data.expr is not None
        data.value = 'data'

    def schedule_values(self, batch_size):
        batches = []
        cur_batch = []
        batch_id = 0
        for value in self.values:
            if value.expr is None:
                continue
            lhs, rhs = value.expr
            assert lhs.batch <= batch_id
            assert rhs.batch <= batch_id
            if lhs.batch == -1:
                assert lhs.expr is None
            if rhs.batch == -1:
                assert rhs.expr is None
            if (len(cur_batch) >= batch_size or
                lhs.batch == batch_id or rhs.batch == batch_id):
                assert len(cur_batch) > 0
                batches.append(cur_batch)
                cur_batch = []
                batch_id += 1
            cur_batch.append(value)
            value.batch = batch_id
        assert len(cur_batch) > 0
        batches.append(cur_batch)
        return batches

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

        for reg_name in ['ttl_hi_mask', 'ttl_lo_mask', 'dma_ttl_mask', 'timing_ctrl',
                         'dds_timing1', 'dds_timing2', 'loopback', 'dma_ctrl']:
            if (reg_name == 'ttl_hi_mask' or reg_name == 'ttl_lo_mask' or
                reg_name == 'dma_ttl_mask'):
                rd_real_reg = wr_real_reg = getattr(csr, reg_name)[:self.ioctrl.nttlout]
            elif reg_name == 'dds_timing1' or reg_name == 'dds_timing2':
                # Let the property getter return different padding registers for read/write
                rd_real_reg = getattr(csr, reg_name)
                wr_real_reg = getattr(csr, reg_name)
            else:
                rd_real_reg = wr_real_reg = getattr(csr, reg_name)
            wr_real_reg = Signal.cast(wr_real_reg)
            rd_real_reg = Signal.cast(rd_real_reg)
            if reg_name in ('ttl_hi_mask', 'ttl_lo_mask', 'dma_ttl_mask', 'dds_timing1',
                            'dds_timing2', 'loopback'):
                rd_reg = relaxed_read_shadow(m, rd_real_reg)
            else:
                rd_reg, _ = reg_chain(m, input=rd_real_reg, levels=2)
            _, wr_reg = reg_chain(m, output=wr_real_reg, levels=2)
            setattr(wr_shadow, reg_name, wr_reg)
            setattr(rd_shadow, reg_name, rd_reg)

        for reg_name in ['ttl_out', 'ttl_in', 'timing_status', 'clockout_div',
                         'dbg_result_count', 'dds0_reg', 'dds1_reg', 'dma_status']:
            real_reg = Signal.cast(getattr(csr, reg_name))
            if reg_name == 'ttl_out':
                real_reg = real_reg[:self.ioctrl.nttlout]
            elif reg_name == 'ttl_in':
                real_reg = real_reg[:self.ioctrl.nttlin]
            if reg_name in ('ttl_out', 'ttl_in', 'clockout_div',
                            'dbg_result_count', 'dds0_reg', 'dds1_reg'):
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

        def ttl_in_reg(idx):
            return rd_shadow.ttl_in[idx * 32:(idx + 1) * 32]

        def rd_dma_ttl(idx):
            return rd_shadow.dma_ttl_mask[idx * 32:(idx + 1) * 32]
        def wr_dma_ttl(idx):
            return wr_shadow.dma_ttl_mask[idx * 32:(idx + 1) * 32]

        with Transaction().body(m, ready=self.fifos.result_fifo.write.run):
            csr.dbg_result_generated.count(m)

        dma_enabled = Signal()
        m.d.comb += dma_enabled.eq(csr.dma_ctrl.enabled)
        dma_enabled.attrs["molecube.vivado.false_path_to"] = "TRUE"

        # Buffer for command fifo to simplify write combinational logic
        m.submodules.cmd_pre_fifo = cmd_pre_fifo = BufferedFifo([('data', self.data_width)], 3)
        with Transaction().body(m):
            cmd = cmd_pre_fifo.read(m)
            with m.If(dma_enabled):
                self.fifos.cmd2_fifo.write(m, cmd)
            with m.Else():
                self.fifos.cmd_fifo.write(m, cmd)

        m.submodules.write_pipe = write_pipe = PipelineBuilder()
        start_write = write_pipe.create_external(i=[('idx', self.valid_width - 2),
                                                    ('data', self.data_width),
                                                    ('strb', 4)], o=[])

        @write_pipe.stage(m)
        def _(idx, data):
            with m.If(idx == 0x1f):
                csr.dbg_inst_word_count.count(m)
                cmd_pre_fifo.write(m, data)
            with m.Elif(idx == 0x58):
                self.fifos.dma_cmd_fifo.write(m, addr=Cat(C(0, 12), data[12:]),
                                              blocks=data[1:11], first=data[0])

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
                with m.Case(0x05):
                    self.ioctrl.clockout.set(m, Cat(~C(0, self.ioctrl.clock_shift),
                                                    data[:8]))

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

        @write_pipe.stage(m)
        def _(idx, data, strb):
            with m.Switch(idx):
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
                    axi_write_reg(m, wr_dma_ttl(0), data, strb)
                with m.Case(0x49):
                    axi_write_reg(m, wr_dma_ttl(1), data, strb)
                with m.Case(0x4a):
                    axi_write_reg(m, wr_dma_ttl(2), data, strb)
                with m.Case(0x4b):
                    axi_write_reg(m, wr_dma_ttl(3), data, strb)
                with m.Case(0x4c):
                    axi_write_reg(m, wr_dma_ttl(4), data, strb)
                with m.Case(0x4d):
                    axi_write_reg(m, wr_dma_ttl(5), data, strb)
                with m.Case(0x4e):
                    axi_write_reg(m, wr_dma_ttl(6), data, strb)
                with m.Case(0x4f):
                    axi_write_reg(m, wr_dma_ttl(7), data, strb)

                with m.Case(0x50):
                    axi_write_reg(m, wr_shadow.dds_timing1, data, strb)
                with m.Case(0x51):
                    axi_write_reg(m, wr_shadow.dds_timing2, data, strb)
                with m.Case(0x52):
                    self.ioctrl.dds0.read_dds_cache(m, id=data[7:11], addr=data[1:7])
                with m.Case(0x53):
                    self.ioctrl.dds1.read_dds_cache(m, id=data[7:11], addr=data[1:7])
                with m.Case(0x59):
                    axi_write_reg(m, wr_shadow.dma_ctrl, data, strb)

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

        read_states = ReadStates()

        read_states.add_leaf(0x00, rd_ttl_hi(0))
        read_states.add_leaf(0x01, rd_ttl_lo(0))
        read_states.add_leaf(0x02, rd_shadow.timing_status)
        read_states.add_leaf(0x03, rd_shadow.timing_ctrl)
        read_states.add_leaf(0x04, ttl_out_reg(0))
        read_states.add_leaf(0x05, rd_shadow.clockout_div)
        read_states.add_leaf(0x06, MAJOR_VERSION)
        read_states.add_leaf(0x07, MINOR_VERSION)
        read_states.add_leaf(0x10, rd_ttl_hi(1))
        read_states.add_leaf(0x11, rd_ttl_lo(1))
        read_states.add_leaf(0x12, rd_ttl_hi(2))
        read_states.add_leaf(0x13, rd_ttl_lo(2))
        read_states.add_leaf(0x14, rd_ttl_hi(3))
        read_states.add_leaf(0x15, rd_ttl_lo(3))
        read_states.add_leaf(0x16, rd_ttl_hi(4))
        read_states.add_leaf(0x17, rd_ttl_lo(4))
        read_states.add_leaf(0x18, rd_ttl_hi(5))
        read_states.add_leaf(0x19, rd_ttl_lo(5))
        read_states.add_leaf(0x1a, rd_ttl_hi(6))
        read_states.add_leaf(0x1b, rd_ttl_lo(6))
        read_states.add_leaf(0x1c, rd_ttl_hi(7))
        read_states.add_leaf(0x1d, rd_ttl_lo(7))
        read_states.add_leaf(0x1e, rd_shadow.loopback)
        read_states.add_leaf(0x1f, 'fifo_data', 32)
        read_states.add_leaf(0x20, rd_shadow.dbg_inst_word_count)
        read_states.add_leaf(0x21, rd_shadow.dbg_inst_count)
        read_states.add_leaf(0x22, rd_shadow.dbg_ttl_count)
        read_states.add_leaf(0x23, rd_shadow.dbg_dds_count)
        read_states.add_leaf(0x24, rd_shadow.dbg_wait_count)
        read_states.add_leaf(0x25, rd_shadow.dbg_clear_count)
        read_states.add_leaf(0x26, rd_shadow.dbg_loopback_count)
        read_states.add_leaf(0x27, rd_shadow.dbg_clock_count)
        read_states.add_leaf(0x28, rd_shadow.dbg_spi_count)
        read_states.add_leaf(0x29, rd_shadow.dbg_underflow_cycle)
        read_states.add_leaf(0x2a, rd_shadow.dbg_inst_cycle)
        # read_states.add_leaf(0x2b, rd_shadow.dbg_ttl_cycle)
        # read_states.add_leaf(0x2c, rd_shadow.dbg_wait_cycle)
        # read_states.add_leaf(0x2d, rd_shadow.dbg_result_overflow_count)
        read_states.add_leaf(0x2e, rd_shadow.dbg_result_count)
        read_states.add_leaf(0x2f, rd_shadow.dbg_result_generated)
        read_states.add_leaf(0x30, rd_shadow.dbg_result_consumed)

        read_states.add_leaf(0x40, ttl_out_reg(1))
        read_states.add_leaf(0x41, ttl_out_reg(2))
        read_states.add_leaf(0x42, ttl_out_reg(3))
        read_states.add_leaf(0x43, ttl_out_reg(4))
        read_states.add_leaf(0x44, ttl_out_reg(5))
        read_states.add_leaf(0x45, ttl_out_reg(6))
        read_states.add_leaf(0x46, ttl_out_reg(7))

        read_states.add_leaf(0x48, rd_dma_ttl(0))
        read_states.add_leaf(0x49, rd_dma_ttl(1))
        read_states.add_leaf(0x4a, rd_dma_ttl(2))
        read_states.add_leaf(0x4b, rd_dma_ttl(3))
        read_states.add_leaf(0x4c, rd_dma_ttl(4))
        read_states.add_leaf(0x4d, rd_dma_ttl(5))
        read_states.add_leaf(0x4e, rd_dma_ttl(6))
        read_states.add_leaf(0x4f, rd_dma_ttl(7))

        read_states.add_leaf(0x50, rd_shadow.dds_timing1)
        read_states.add_leaf(0x51, rd_shadow.dds_timing2)
        read_states.add_leaf(0x52, rd_shadow.dds0_reg)
        read_states.add_leaf(0x53, rd_shadow.dds1_reg)

        read_states.add_leaf(0x58, rd_shadow.dma_status)
        read_states.add_leaf(0x59, rd_shadow.dma_ctrl)

        read_states.add_leaf(0x60, ttl_in_reg(0))
        read_states.add_leaf(0x61, ttl_in_reg(1))
        read_states.add_leaf(0x62, ttl_in_reg(2))
        read_states.add_leaf(0x63, ttl_in_reg(3))
        read_states.add_leaf(0x64, ttl_in_reg(4))
        read_states.add_leaf(0x65, ttl_in_reg(5))
        read_states.add_leaf(0x66, ttl_in_reg(6))
        read_states.add_leaf(0x67, ttl_in_reg(7))

        read_states.compute_exprs()

        all_idxs = set()
        for value in read_states.values:
            if value.expr is None:
                continue
            all_idxs.add(value.bit)
        all_idxs = sorted(all_idxs)

        @read_pipe.stage(m, o=[(f'idx{i}', 1) for i in all_idxs])
        def _(idx):
            return {f'idx{i}': idx[i] for i in all_idxs}

        max_batch_sz = 8
        batches = read_states.schedule_values(max_batch_sz)

        for (batch_id, batch) in enumerate(batches):
            idxs = set()
            layout_in = []
            layout_out = []
            for value in batch:
                assert value.expr is not None
                idxs.add(value.bit)
                assert isinstance(value.value, str)
                layout_out.append((value.value, value.size))
                for vin in value.expr:
                    if isinstance(vin.value, str):
                        layout_in.append((vin.value, vin.size))
            for idx in sorted(idxs):
                layout_in.append((f'idx{idx}', 1))

            @read_pipe.stage(m, i=layout_in, o=layout_out)
            def _(arg):
                res = {}
                for value in batch:
                    v0, v1 = value.expr
                    v0 = getattr(arg, v0.value) if isinstance(v0.value, str) else v0.value
                    v1 = getattr(arg, v1.value) if isinstance(v1.value, str) else v1.value
                    idx_bit = getattr(arg, f'idx{value.bit}')
                    res[value.value] = Mux(idx_bit, v1, v0)
                return res

        m.submodules.read_rep_fifo = read_rep_fifo = BufferedFifo(
            [('data', self.data_width), ('resp', 2), ('id', self.id_width),
             ('last', 1)], 3)

        read_pipe.call_method(read_rep_fifo.write)

        with Transaction().body(m):
            rep = read_rep_fifo.read(m)
            read_iface.done(m, data=rep.data, resp=rep.resp,
                            id=rep.id, last=rep.last)

        with Transaction().body(m):
            req = read_iface.get(m)
            start_read(m, idx=req.addr >> 2, id=req.id, last=req.last)

        return m
